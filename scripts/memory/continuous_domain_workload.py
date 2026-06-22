#!/usr/bin/env python3
"""Continuous allocation workload across all 3 CPython memory domains.

Unlike mem_domain_workload.py (allocate-once-then-sleep), this script keeps a
growing persistent pool so the profiler captures both allocation events (alloc-
space) AND live heap at profile-export time (heap-space / "Heap Live Size").

KEY FIX from v1: objects now live for at least one full profiling window (60 s)
before the pool drains.  v1 freed everything each 50 ms cycle, so heap-live-
size only showed background Python memory (~20 MiB), not workload memory.

Usage:
    DD_SERVICE=mem-domain-test DD_PROFILING_ENABLED=true \\
    DD_PROFILING_MEM_DOMAIN_ENABLED=true \\
    DD_TRACE_AGENT_URL=unix:///var/run/workspaces/datadog/apm.socket \\
    DD_DOGSTATSD_URL=unix:///var/run/workspaces/datadog-agent/statsd.sock \\
      .venv/bin/ddtrace-run .venv/bin/python scripts/memory/continuous_domain_workload.py

Ctrl-C to stop.  Three upload cycles (~3 min) captures fill, hold, and drain.

Pool lifecycle (illustrates heap-live-size profile):
  0–60 s   : pool grows   → heap tracker sees ever-more live samples
  60–90 s  : pool holds   → heap-live-size plateau captured in first profile
  90 s     : pool drains  → heap-live-size drops in next profile
  repeat

Two export cycles is enough to see the before/after MEM domain comparison in
Datadog's Comparison view.
"""

import array
import os
import time
from typing import List

# ── Workload knobs ────────────────────────────────────────────────────────────
CHUNK_OBJ = 64          # bytes per OBJ-domain object
OBJ_PER_BURST = 200_000 # smaller burst; we accumulate instead of free
ITEMS_PER_ARRAY = 65536 # bytes per array.array buffer  (MEM domain)
LIST_SIZE = 4096        # items per list ob_item array   (MEM domain)
ALLOCS_PER_BURST = 200  # n passed to alloc_mem_burst / alloc_raw_burst
CHUNK_RAW = 512 * 1024  # bytes per bytearray chunk      (RAW domain)
BURST_SLEEP = 0.10      # seconds between cycles
HOLD_SECONDS = 90.0     # how long to hold the pool before draining


def alloc_obj_burst(n: int) -> List[bytes]:
    """PYMEM_DOMAIN_OBJ: many small bytes objects via PyObject_Malloc."""
    return [bytes(CHUNK_OBJ) for _ in range(OBJ_PER_BURST)]


def alloc_mem_burst(n: int) -> list:
    """PYMEM_DOMAIN_MEM: array.array buffers + list backing arrays via PyMem_Calloc.

    What exercises the MEM domain:
      - array.array internal C buffer  → PyMem_Malloc (PYMEM_DOMAIN_MEM)
      - list ob_item pointer array     → PyMem_New    (PYMEM_DOMAIN_MEM)
    The Python wrapper objects themselves go through PYMEM_DOMAIN_OBJ.
    """
    out: list = []
    for _ in range(n // 2):
        out.append(array.array("B", bytes(ITEMS_PER_ARRAY)))
    for _ in range(n // 2):
        out.append([None] * LIST_SIZE)
    return out


def alloc_raw_burst(n: int) -> List[bytearray]:
    """PYMEM_DOMAIN_RAW: large bytearray chunks via malloc."""
    return [bytearray(CHUNK_RAW) for _ in range(max(n // 10, 1))]


def _mem_mb(mem_pool: list) -> float:
    return sum(
        len(x) * x.itemsize if isinstance(x, array.array) else len(x) * 8
        for x in mem_pool
    ) / 1024 / 1024


def _ensure_pprof_dir() -> None:
    pprof = os.environ.get("DD_PROFILING_OUTPUT_PPROF", "")
    if pprof:
        os.makedirs(os.path.dirname(pprof) or ".", exist_ok=True)


def main() -> None:
    _ensure_pprof_dir()

    # ── Persistent pools — objects live until drained ─────────────────────────
    obj_pool: List[bytes] = []
    mem_pool: list = []
    raw_pool: List[bytearray] = []

    cycle = 0
    pool_start = time.monotonic()
    phase = "fill"

    print(f"Pool lifecycle: fill {HOLD_SECONDS:.0f}s → hold → drain → repeat")
    print(f"OBJ burst={OBJ_PER_BURST} × {CHUNK_OBJ}B  "
          f"MEM burst={ALLOCS_PER_BURST//2} arrays + {ALLOCS_PER_BURST//2} lists  "
          f"sleep={BURST_SLEEP}s")
    print("Ctrl-C to stop\n")

    while True:
        cycle += 1
        elapsed = time.monotonic() - pool_start

        if elapsed >= HOLD_SECONDS:
            # Drain and restart the clock
            obj_pool.clear()
            mem_pool.clear()
            raw_pool.clear()
            pool_start = time.monotonic()
            phase = "fill"
            print(f"[cycle {cycle}] ── POOL DRAINED ──")

        # Accumulate into persistent pools (objects survive to next profile export)
        obj_pool.extend(alloc_obj_burst(ALLOCS_PER_BURST))
        mem_pool.extend(alloc_mem_burst(ALLOCS_PER_BURST))
        raw_pool.extend(alloc_raw_burst(ALLOCS_PER_BURST))

        if cycle % 10 == 1:
            obj_mb = len(obj_pool) * CHUNK_OBJ / 1024 / 1024
            mem_mb_val = _mem_mb(mem_pool)
            raw_mb = len(raw_pool) * CHUNK_RAW / 1024 / 1024
            print(
                f"[cycle {cycle:4d} | {elapsed:5.1f}s | {phase}] "
                f"OBJ={obj_mb:.0f}MB  MEM={mem_mb_val:.0f}MB  RAW={raw_mb:.0f}MB"
            )

        time.sleep(BURST_SLEEP)


if __name__ == "__main__":
    main()
