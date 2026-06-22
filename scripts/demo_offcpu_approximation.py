#!/usr/bin/env python3
"""Off-CPU demo workload + side-by-side comparison with the eBPF sidecar.

This drives milestone 6 of the eBPF off-CPU profiler spike
(``ebpf-offcpu-profiler-design.md``): validate the kernel-measured off-CPU time
from ``dd_offcpu`` against the userspace ``wall_time - cpu_time`` approximation
that the in-process profiler uses (PR #18623).

It runs a fixed-duration workload of threads with *known* blocking behaviour
(sleep, lock contention, blocking I/O) plus CPU-bound threads (which should show
~0 off-CPU). Each worker measures its own wall and CPU time, so the script can
report the ``wall - cpu`` approximation directly -- no profiler required.

In ``--compare`` mode it additionally launches ``dd_offcpu`` against its own pid
for the same window, aggregates the kernel-measured off-CPU per thread, and
prints a side-by-side table keyed by native thread id (``threading.get_native_id``
matches the namespace-local tid the sidecar emits).

Usage:
    # Just the approximation (no eBPF, no privileges needed):
    python3 scripts/demo_offcpu_approximation.py --duration 5

    # Side-by-side vs the eBPF sidecar (dd_offcpu must be built + setcap'd):
    python3 scripts/demo_offcpu_approximation.py --compare --duration 5
"""

from __future__ import annotations

import argparse
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from dataclasses import field


DEFAULT_OFFCPU_BIN = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "..",
    "ddtrace",
    "internal",
    "datadog",
    "profiling",
    "dd_offcpu",
    "build",
    "dd_offcpu",
)


@dataclass
class Worker:
    """A demo thread and the timing it self-reports."""

    name: str
    cause: str  # expected off-CPU cause: sleep | lock | io | (none for CPU-bound)
    fn: object
    native_id: int = 0
    wall_s: float = 0.0
    cpu_s: float = 0.0
    thread: threading.Thread = field(default=None, repr=False)

    @property
    def approx_offcpu_s(self) -> float:
        # The in-process approximation: time the thread existed minus the CPU
        # time it actually consumed. Clamp tiny negatives from measurement skew.
        return max(0.0, self.wall_s - self.cpu_s)


def _run_timed(worker: Worker, stop: threading.Event) -> None:
    """Wrap a worker body with native-id capture and wall/CPU accounting."""
    worker.native_id = threading.get_native_id()
    wall0 = time.perf_counter()
    cpu0 = time.thread_time()
    try:
        worker.fn(stop)
    finally:
        worker.cpu_s = time.thread_time() - cpu0
        worker.wall_s = time.perf_counter() - wall0


# --------------------------------------------------------------------- workers


def make_sleeper():
    def body(stop):
        while not stop.is_set():
            time.sleep(0.02)

    return body


def make_lock_pair():
    """A holder that keeps a lock most of the time and a waiter that blocks on it."""
    lock = threading.Lock()

    def holder(stop):
        while not stop.is_set():
            with lock:
                time.sleep(0.03)
            time.sleep(0.001)

    def waiter(stop):
        while not stop.is_set():
            with lock:
                pass

    return holder, waiter


def make_io_pair():
    """A feeder that writes to a socket slowly and a reader that blocks on recv."""
    rsock, wsock = socket.socketpair()

    def feeder(stop):
        try:
            while not stop.is_set():
                time.sleep(0.02)
                try:
                    wsock.send(b"x")
                except OSError:
                    break
        finally:
            wsock.close()

    def reader(stop):
        try:
            while not stop.is_set():
                try:
                    if not rsock.recv(1):
                        break
                except OSError:
                    break
        finally:
            rsock.close()

    return feeder, reader


def make_spinner():
    def body(stop):
        x = 0
        while not stop.is_set():
            x = (x * 1103515245 + 12345) & 0x7FFFFFFF

    return body


def make_fibber():
    def fib(n):
        return n if n < 2 else fib(n - 1) + fib(n - 2)

    def body(stop):
        while not stop.is_set():
            fib(28)

    return body


def build_workers() -> list[Worker]:
    holder, waiter = make_lock_pair()
    feeder, reader = make_io_pair()
    return [
        Worker("sleeper", "sleep", make_sleeper()),
        Worker("lock_holder", "sleep", holder),
        Worker("lock_waiter", "lock", waiter),
        Worker("io_feeder", "sleep", feeder),
        Worker("io_reader", "io", reader),
        Worker("cpu_spinner", "-", make_spinner()),
        Worker("cpu_fibber", "-", make_fibber()),
    ]


# ------------------------------------------------------------------ sidecar I/O


def aggregate_offcpu_by_tid(stdout_lines: list[str]) -> dict[int, float]:
    """Sum off-CPU seconds per tid from dd_offcpu stdout (``tid=N ... off_cpu=X ms``)."""
    totals: dict[int, float] = {}
    for line in stdout_lines:
        if not line.startswith("tid="):
            continue
        tid = None
        ms = None
        for tok in line.split():
            if tok.startswith("tid="):
                try:
                    tid = int(tok[4:])
                except ValueError:
                    tid = None
            elif tok.startswith("off_cpu="):
                try:
                    ms = float(tok[len("off_cpu="):])
                except ValueError:
                    ms = None
        if tid is not None and ms is not None:
            totals[tid] = totals.get(tid, 0.0) + ms / 1000.0
    return totals


def print_table(workers: list[Worker], ebpf_by_tid: dict[int, float] | None, duration: float) -> None:
    has_ebpf = ebpf_by_tid is not None
    print()
    print(f"Off-CPU comparison over ~{duration:.1f}s (per thread)")
    print("-" * (78 if has_ebpf else 56))
    if has_ebpf:
        print(f"{'thread':<14}{'cause':<7}{'wall(s)':>9}{'cpu(s)':>9}{'approx(s)':>11}{'eBPF(s)':>10}{'Δ(s)':>9}")
    else:
        print(f"{'thread':<14}{'cause':<7}{'wall(s)':>9}{'cpu(s)':>9}{'approx(s)':>11}")
    print("-" * (78 if has_ebpf else 56))
    for w in workers:
        approx = w.approx_offcpu_s
        if has_ebpf:
            ebpf = ebpf_by_tid.get(w.native_id, 0.0)
            delta = ebpf - approx
            print(f"{w.name:<14}{w.cause:<7}{w.wall_s:>9.2f}{w.cpu_s:>9.2f}{approx:>11.2f}{ebpf:>10.2f}{delta:>+9.2f}")
        else:
            print(f"{w.name:<14}{w.cause:<7}{w.wall_s:>9.2f}{w.cpu_s:>9.2f}{approx:>11.2f}")
    print("-" * (78 if has_ebpf else 56))
    if has_ebpf:
        unmatched = set(ebpf_by_tid) - {w.native_id for w in workers}
        if unmatched:
            extra = sum(ebpf_by_tid[t] for t in unmatched)
            print(f"(+{extra:.2f}s eBPF off-CPU on {len(unmatched)} unmatched tids, e.g. the main thread)")


# ------------------------------------------------------------------------- main


def run(duration: float, compare: bool, offcpu_bin: str, min_block_us: int, offcpu_output: str) -> int:
    workers = build_workers()
    stop = threading.Event()

    proc = None
    stdout_lines: list[str] = []
    reader_thread = None
    if compare:
        if not os.path.exists(offcpu_bin):
            print(f"error: dd_offcpu not found at {offcpu_bin}; build it first", file=sys.stderr)
            return 2
        # Attach the sidecar to ourselves before the workers start blocking.
        # --output makes the eBPF off-CPU pprof land at a known path we can print.
        proc = subprocess.Popen(
            [
                offcpu_bin,
                "--pid", str(os.getpid()),
                "--min-block-us", str(min_block_us),
                "--output", offcpu_output,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        # Drain stdout in a background thread: dd_offcpu prints several lines per
        # event, so reading only at the end would fill the pipe and stall it.
        def drain(pipe, sink):
            for line in pipe:
                sink.append(line)

        reader_thread = threading.Thread(target=drain, args=(proc.stdout, stdout_lines), daemon=True)
        reader_thread.start()

        # Wait for the "profiling pid ..." banner so we know it has attached.
        deadline = time.time() + 5.0
        while time.time() < deadline:
            line = proc.stderr.readline()
            if not line:
                break
            if "profiling pid" in line:
                break
        time.sleep(0.3)

    for w in workers:
        w.thread = threading.Thread(target=_run_timed, args=(w, stop), name=w.name, daemon=True)
        w.thread.start()

    time.sleep(duration)
    stop.set()
    for w in workers:
        w.thread.join(timeout=2.0)

    ebpf_by_tid = None
    if proc is not None:
        proc.send_signal(signal.SIGINT)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        if reader_thread is not None:
            reader_thread.join(timeout=5.0)
        ebpf_by_tid = aggregate_offcpu_by_tid(stdout_lines)

    print_table(workers, ebpf_by_tid, duration)

    if proc is not None:
        ebpf_path = os.path.abspath(offcpu_output)
        approx_prefix = os.environ.get("DD_PROFILING_OUTPUT_PPROF")
        print()
        print("pprof artifacts (open side by side in go tool pprof)")
        print("-" * 56)
        print(f"  eBPF (kernel-exact):  {ebpf_path}")
        print(f"    go tool pprof -http :8081 {ebpf_path}")
        if approx_prefix:
            print(f"  approx (in-process):  {approx_prefix}.*  (on profiler flush)")
            print(f"    go tool pprof -http :8080 {approx_prefix}.*")
        else:
            print("  approx (in-process):  not captured this run — to get its pprof, run")
            print("    on vlad/profiling-offcpu-approximation under the profiler:")
            print("    DD_PROFILING_ENABLED=true _DD_PROFILING_STACK_OFFCPU_TIME_ENABLED=true \\")
            print("    DD_PROFILING_OUTPUT_PPROF=/tmp/approx ddtrace-run python3 <this script>")
    return 0


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--duration", type=float, default=5.0, help="workload duration in seconds")
    ap.add_argument("--compare", action="store_true", help="also run dd_offcpu and print a side-by-side table")
    ap.add_argument("--offcpu-bin", default=os.path.normpath(DEFAULT_OFFCPU_BIN), help="path to the dd_offcpu binary")
    ap.add_argument("--min-block-us", type=int, default=1000, help="dd_offcpu --min-block-us")
    ap.add_argument(
        "--offcpu-output",
        default="/tmp/offcpu_ebpf.pb.gz",
        help="path for the eBPF off-CPU pprof written by dd_offcpu (--compare only)",
    )
    args = ap.parse_args(argv)

    print(f"pid={os.getpid()} duration={args.duration}s compare={args.compare}", flush=True)
    return run(args.duration, args.compare, args.offcpu_bin, args.min_block_us, args.offcpu_output)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
