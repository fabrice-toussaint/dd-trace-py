# eBPF Off-CPU Profiler — Design Decision

## Problem

The current off-CPU implementation in dd-trace-py (`vlad/profiling-offcpu-approximation`)
is a userspace approximation: `off_cpu ≈ max(0, wall_time − cpu_time)`, sampled at
10ms intervals. It is imprecise:
- Misses off-CPU bursts shorter than the sampling interval
- Accumulates clock skew between the wall and CPU clocks
- Cannot attribute exact off-CPU duration to the frame that caused it
- On platforms without CPU time measurement, emits nothing at all

The goal: replace the approximation with exact kernel-measured off-CPU times, correlated
with Python frame names, delivered to all ddtrace customers.

---

## Candidates Considered

### Option A: Extend the approximation (already shipped)

What exists: `wall_time − cpu_time` per sample, `off cpu cause` label from leaf frame name.

**Pros:** Zero new dependencies, works on macOS and Linux, works today.  
**Cons:** Imprecise by definition. Cannot detect sub-interval bursts. Cause label is
a substring heuristic, not a measurement.  
**Status:** Shipped in `vlad/profiling-offcpu-approximation` + `vlad/profiling-offcpu-cause-label`.
Good enough for an initial signal. Not good enough for a production-quality off-CPU profiler.

---

### Option B: Extend ddprof

**What it is:** `github.com/DataDog/ddprof` — Datadog's Native Profiler for Linux.
C++, `perf_event_open`-based, profiles individual processes. Has a `PerfWatcher`
plugin architecture with tracepoint support, DWARF-based stack unwinder, libdatadog
pprof export. Not actively developed (beta, in maintenance).

**Why it doesn't fit:**
- Uses `perf_event_open` with no BPF programs. For off-CPU, this means receiving raw
  `sched_switch` events in userspace and correlating start/end there — extremely high
  volume on busy systems, no kernel-side accumulation.
- No Python frame walking — shows `_PyEval_EvalFrameDefault`, not `time.sleep`.
- Requires manual invocation (`ddprof ./my_program`); not auto-launched by ddtrace;
  most Python developers don't know it exists.
- Not actively worked on; landing new subsystems would face high inertia.

**Verdict: Ruled out.** Wrong tool for BPF-based off-CPU; not the right long-term home.

---

### Option C: Full Host Profiler (dd-otel-host-profiler)

**What it is:** `github.com/DataDog/dd-otel-host-profiler` — Datadog's wrapper around
the OpenTelemetry eBPF profiler (originally donated by Elastic). Written in Go +
cilium/ebpf. Profiles ALL processes on a host continuously. Actively developed; the
profiling team's main focus.

**What it already has** (in the upstream OTel project):
- `support/ebpf/off_cpu.ebpf.c` — `sched_switch` + `kprobe/finish_task_switch`,
  per-TID duration accumulation in kernel, ring buffer output. Exactly what we'd build.
- `interpreter/python/python.go` — Python frame walking from outside the process:
  reads `_PyRuntime` from `/proc/<pid>/mem`, walks `PyInterpreterState → frame` chain.
  Handles Python 3.6–3.14 with version-keyed struct offsets.
- `support/ebpf/python_tracer.ebpf.c` — BPF-side Python tracing.
- `runner.go` already calls `trc.AttachSchedMonitor()` — the scheduler BPF hook is live.

**What it lacks for our use case:**
1. **Reach.** Requires: manual binary install, `sudo`, `tracefs` mounted, Datadog Agent
   running separately. Not bundled with dd-trace-py. Not deployed by default on any
   customer host. A feature requiring FHP has near-zero customer reach today.
2. **Trace correlation.** FHP knows "thread X was off-CPU for 40ms." It does not know
   which ddtrace *span* was executing at that moment — no service name, no trace ID,
   no endpoint, no resource attribution. That correlation is the entire product value.
3. **Python profiling context.** asyncio task tracking, greenlet support, GIL
   contention attribution — FHP's Python support gives function names; ddtrace gives
   full application context.

**Verdict: Right long-term home, wrong solution today.**
Building off-CPU into FHP (or contributing to it) is the right eventual state.
But FHP's deployment story makes it useless for the "reach all ddtrace customers" goal
until either (a) FHP is bundled with the Datadog Agent by default, or (b) ddtrace
auto-provisions it. Neither is true today.

---

### Option D: Custom sidecar (recommended for spike)

**What it is:** A new, minimal, focused binary that does exactly one thing: measure
off-CPU time for a specific Python process and emit a pprof profile with Python frames.
Bundled with dd-trace-py; auto-launched by ddtrace when off-CPU is enabled.

**Why custom instead of leveraging FHP code directly:**
FHP's Python frame walker is in Go and cannot be linked into a C binary. However, the
*algorithm* is well-documented in FHP and py-spy and can be re-implemented in C in a
few hundred lines. The BPF program can be implemented in C with libbpf — the same
approach FHP uses internally (cilium/ebpf just loads precompiled BPF C objects; the
BPF side is the same language regardless).

**Why this solves the problems FHP doesn't:**
- Bundled in the ddtrace package → reaches 100% of ddtrace customers, not 1%
- ddtrace spawns it pointed at its own PID → no manual setup
- ddtrace can pass its current span context → enables trace correlation
- Privilege solved via `setcap cap_bpf,cap_perfmon=ep` at install time (Option B
  privilege model), same as `dumpcap`/`ping` — capability on the binary, not the
  launcher

**What we are NOT doing:** reinventing FHP. The BPF program and Python frame walker
are well-understood; the spike proves they work. Once proven, the right conversation
with the FHP team is "can we make FHP deployable alongside ddtrace and add trace
correlation?" — the spike exists to inform that conversation.

---

## Decision: Build the custom sidecar spike

We are building it. Rationale:
1. FHP already has everything technically — but we still need to build it ourselves to
   solve the reach and trace correlation problems.
2. The spike is fast (days, not weeks) because the BPF approach is proven and the
   Python frame walking algorithm exists in FHP/py-spy to reference.
3. If the spike succeeds, it becomes either a standalone ddtrace feature OR the
   concrete proposal we bring to the FHP team for integration. Either way it's not
   wasted.

---

## Architecture

```
┌──────────────────────────────────────┐
│  Python app + dd-trace-py            │
│  (no privilege change)               │
│  - spawns sidecar at startup         │
│  - merges sidecar pprof into upload  │
│  - passes span context (future)      │
└──────────────────────────────────────┘
           ↕ pprof file (spike) / socket (production)
┌──────────────────────────────────────────────────────┐
│  dd_offcpu sidecar                                   │
│  setcap cap_bpf,cap_perfmon=ep at install time       │
│                                                      │
│  BPF program (C, clang -target bpf)                  │
│    sched_switch: per-TID duration accumulation       │
│    BPF_MAP_TYPE_STACK_TRACE: user stacks             │
│    BPF_MAP_TYPE_RINGBUF: completed events            │
│                                                      │
│  Daemon (C + libbpf)                                 │
│    ring buffer reader                                │
│    ELF symbolizer (/proc/<pid>/maps)                 │
│    Python frame walker (/proc/<pid>/mem)             │
│    pprof builder (libdatadog C API)                  │
└──────────────────────────────────────────────────────┘
```

**Language:** C (daemon + BPF program). libbpf for BPF loading. libdatadog for pprof
(already a dd-trace-py dependency). No Go.

**Privilege model:** `setcap cap_bpf,cap_perfmon=ep /path/to/dd_offcpu` once at install
time. After that ddtrace spawns it as a plain subprocess — no sudo at runtime.

---

## Spike Scope

**BPF program** (`bpf/offcpu.bpf.c`): `tracepoint/sched/sched_switch` +
`kprobe/finish_task_switch`, per-TID hash map, ring buffer output. **Kernel ≥ 5.8**,
CO-RE for portability. See "Kernel requirements & customer reach" for the version
breakdown and graceful-fallback behavior.

**C daemon** (`src/`): libbpf skeleton loader, ring buffer poll, ELF symbolizer,
Python frame walker, pprof emit via libdatadog.

**Python frame walker** (`src/pysym.c`): `/proc/<pid>/maps` → `_PyRuntime` →
`PyInterpreterState` → per-TID `tstate` → frame chain → `co_qualname`/`co_name`.
Struct offsets keyed by `(major, minor)`. Target: Python 3.10–3.12.
Reference: FHP's `interpreter/python/python.go` and py-spy's `src/python_spy.rs`.

**Spike milestones:**

| # | Done when | Status |
|---|-----------|--------|
| 1 | `./dd_offcpu --pid <pid>` loads BPF without error (setcap set) | ✅ done |
| 2 | Ring buffer prints TID + off-CPU duration (ns) | ✅ done |
| 3 | Native function names in output | ✅ done |
| 4 | `time.sleep`, `lock.acquire` visible as Python frames | ✅ done |
| 5 | `go tool pprof -http :8080 offcpu.pb.gz` shows off-CPU flame graph | ✅ done |
| 6 | Side-by-side comparison with `demo_offcpu_approximation.py` output | ✅ done |
| 7 | Tests: symbolizer unit tests + gated end-to-end smoke test [^tests] | ⬜ next |

**Milestone 1–2 notes:** libbpf is vendored and statically linked (`v1.5.1`)
because distro libbpf (Ubuntu 22.04 ships 0.5.0) predates `BTF_KIND_ENUM64` and
cannot load against kernels ≥ 6.0. Target matching is PID-namespace-aware: the
loader stats `/proc/<pid>/ns/pid` for the namespace inode and the BPF program
matches on `(namespace-local pid, ns inode)`, so a container-local `--pid`
resolves correctly. Emitted tids are namespace-local.

**Milestone 3 notes:** native frames are resolved by a from-scratch ELF
symbolizer (`src/symbolize.c`): it parses `/proc/<pid>/maps`, builds per-object
symbol tables from `.symtab`/`.dynsym`, and resolves purely by **file offset**
(converting each symbol's vaddr via its `PT_LOAD` header), which is uniform
across PIE/shared `ET_DYN` and fixed `ET_EXEC` and sidesteps ASLR/load-bias.
The off-CPU stack is captured **when the thread leaves the CPU** (in the
`sched_switch` handler `current == prev`, so the stack reflects where it
blocked) and carried in the `start` map until it is scheduled back in — the
earlier on-CPU capture sampled the wrong thread.

Verified: `time.sleep` threads resolve to `clock_nanosleep`, blocking
`os.read` to `__read`. Note the kernel's frame-pointer unwinder only yields the
reliable leaf frame for CPython (built `-fomit-frame-pointer`); deeper frames
walk into non-code memory and are dropped (`symbolizer_resolve` returns -1 once
an address is outside any executable mapping). Recovering the full native call
chain would need DWARF/ORC unwinding; the **Python** call chain comes from
milestone 4's out-of-process frame walk instead.

**Milestone 4 notes:** Python frames are walked **out-of-process** (`src/pysym.c`)
by reading the target's memory with `process_vm_readv`:
`_PyRuntime` → `interpreters.head` → `threads.head` (matched by
`native_thread_id`, which is namespace-local — same numbering as the BPF-emitted
tid) → `cframe.current_frame` → the `_PyInterpreterFrame` chain → `PyCodeObject`
(`co_qualname`, `co_filename`, `co_firstlineno`). `_PyRuntime` is found via the
ELF symbol table of the object that defines it (the python binary for a static
build, libpython for a shared one) and translated through the load bias.

Struct offsets are CPython-version-specific; this spike targets **3.12** and the
offsets were generated from the interpreter's own internal headers (`offsetof`
against `pycore_runtime.h`/`pycore_interp.h`/`pycore_frame.h`). Other versions
are detected from the binary path and declined rather than misread — a
production build needs a per-`(major,minor)` offset table like py-spy/echion.

Verified end-to-end: a sleeping thread reports
`sleeper (offcpu_workload.py:22) → Thread.run → Thread._bootstrap_inner →
Thread._bootstrap` above `clock_nanosleep`, the lock waiter reports
`lock_waiter`, etc. Two caveats: (1) the line shown is `co_firstlineno` (the
function's `def` line), not the exact blocked line — computing that needs
decoding `co_linetable` against the frame's `prev_instr`; (2) the daemon needs
**`CAP_SYS_PTRACE`** in addition to `CAP_BPF`/`CAP_PERFMON`, otherwise
`process_vm_readv` is denied (non-parent + `yama.ptrace_scope ≥ 1`) and Python
frames silently come back empty while native timing keeps working.

**Milestone 5 notes:** each completed off-CPU interval is accumulated into a
pprof profile (`src/pprof.c`) and flushed to `--output` (default
`offcpu.pb.gz`) on exit. Sample type is `off-cpu`/`nanoseconds`, value is the
interval `delta_ns`, and each sample carries `thread id` (num) and
`thread name` (str) labels. The merged stack is **leaf-first**: the native
syscall frame the thread blocked in (e.g. `clock_nanosleep`) leads, then the
Python frames from innermost (`sleeper`) out to `Thread._bootstrap` — so
`go tool pprof` roots the flame graph at `_bootstrap` with syscalls as leaves.
Frames are interned (one pprof function/location per unique label) so identical
stacks aggregate. Verified: `go tool pprof -top`/`-traces`/`-http` open the file
and show the off-CPU flame graph with correct per-thread durations.

One non-obvious gotcha: `go tool pprof` runs C++ demangling on every function
where `name == system_name`, and `demangle.Filter` rewrites Python synthetic
names like `<module>`/`<listcomp>`/`<genexpr>` (the leading `<…>` is read as a
template fragment) into an **empty** string — so those frames rendered blank.
The writer therefore sets `Function.name` only and leaves `system_name` empty;
that makes `name != system_name`, which trips pprof's "already demangled" guard
and displays our names verbatim.

The pprof writer is a deliberately small, dependency-free protobuf encoder
(reusing the zlib we already link for gzip) — enough for the spike. Two
follow-ups: (1) frames are emitted name-only (the Python `file:line` lives in
the function *name* string rather than pprof's `filename`/`line` fields, which
is cosmetic for a flame graph but limits `go tool pprof list`); (2) a production
build emits through **libdatadog**'s profile exporter instead, for upload, auth,
and standardized labels (and to merge with the in-process profiler — see the
sidecar architecture above).

**Milestone 6 notes:** `scripts/demo_offcpu_approximation.py` runs a controlled,
fixed-duration workload (sleeper, lock holder/waiter, blocking-I/O feeder/reader,
CPU spinner, CPU fibber) where each thread self-measures wall vs CPU time, so it
reports the in-process **`wall − cpu` approximation** (PR #18623) directly — no
profiler or extra branch needed. `--compare` additionally attaches `dd_offcpu` to
the demo's own pid for the same window, aggregates kernel-measured off-CPU per
thread, and prints a side-by-side table keyed by `threading.get_native_id()`
(which equals the namespace-local tid the sidecar emits). A representative 6 s run:

```
thread        cause    wall(s)   cpu(s)  approx(s)   eBPF(s)     Δ(s)
sleeper       sleep       6.08     0.02       6.06      6.01    -0.05
lock_holder   sleep       6.10     0.03       6.07      6.02    -0.05
lock_waiter   lock        6.09     0.77       5.33      5.28    -0.05
io_feeder     sleep       6.08     0.03       6.06      6.02    -0.03
io_reader     io          6.08     0.01       6.07      6.05    -0.02
cpu_spinner   -           6.08     2.47       3.61      3.51    -0.10
cpu_fibber    -           6.11     2.58       3.54      3.45    -0.09
```

eBPF agrees with the approximation to within ~0.02–0.10 s per thread, always
slightly lower — expected, since the sidecar drops sub-`min-block-us` intervals
and the partial intervals straddling attach/detach. Two observations worth
recording: (1) the "CPU-bound" spinner/fibber show ~3.5 s of off-CPU under
*both* methods because they block on the **GIL futex** (only one runs Python
bytecode at a time) — the sidecar correctly attributes this to a `futex` leaf;
(2) the main thread (and a couple of runtime threads) sit ~fully off-CPU and are
reported as unmatched tids in the table footer rather than tied to a named
worker. This confirms the kernel-exact path tracks the cheap approximation while
adding the per-stack attribution the approximation cannot give.

[^tests]: Testing strategy for this spike — deliberately deferred to the last
    milestone rather than one suite per feature, because most milestones are not
    unit-testable in isolation:
    - **Not unit-testable (need a live kernel):** the BPF program (1–2) and the
      daemon load/attach/ringbuf loop require BTF, `cap_bpf`/`cap_perfmon`, and a
      live target process. These belong in a **gated end-to-end smoke test** —
      run the workload, run `dd_offcpu`, assert output contains `clock_nanosleep`
      for a sleeping thread with ~50 ms durations; skip when BPF/caps are
      unavailable. The Python frame walk (4) is best covered here too, since it
      is CPython-version-fragile rather than pure logic.
    - **Worth unit tests (pure, deterministic, easy to get subtly wrong):** the
      ELF symbolizer (`src/symbolize.c`) via CTest — maps-line parsing (exec vs
      non-exec, anonymous, `[heap]`, paths with spaces), `vaddr_to_foff` against a
      known `ET_DYN`/`ET_EXEC` (resolve a known function, assert name + offset),
      and the "address outside any mapping → -1" guard.

---

## Long-term path (post-spike)

If the spike succeeds, two options:

**A. Stay custom:** ddtrace bundles the sidecar, installs with setcap. Add span context
passing so off-CPU pprof includes trace/span IDs. This is a standalone ddtrace feature
requiring no other team's involvement.

**B. Integrate with FHP:** Bring the spike to the FHP team. Propose: (1) FHP deployable
alongside ddtrace without manual user action, (2) FHP receives span context from
ddtrace and adds it to off-CPU samples. This gives FHP's richer Python support and
removes duplicate code. Requires cross-team coordination.

The spike is valuable either way.

---

## Research Context (for handoff)

This section captures the research trail behind the decisions above, so the next agent
has full context without needing to re-investigate.

### What's already shipped

Two PRs on `DataDog/dd-trace-py` are open and functionally complete:

- **PR #18623** (`vlad/profiling-offcpu-approximation`): Adds `off-cpu-time` as a new
  pprof sample type using `wall_time − cpu_time` approximation. Fixed CI failures caused
  by `std::call_once` in `ProfilerState::start()` — tests now use `@pytest.mark.subprocess`
  for isolation. Suspended async tasks / greenlets get `off_cpu = wall_time` exactly.

- **PR #18668** (`vlad/profiling-offcpu-cause-label`): Adds `off cpu cause` pprof label
  (`sleep` / `lock` / `io` / `other`) by classifying the leaf frame name at sample time
  in `stack_renderer.cpp::render_stack_end()`. Key files: `stack_renderer.cpp`,
  `stack_renderer.hpp` (added `top_frame_name` to `ThreadState`).

### Demo script verified working

`scripts/demo_offcpu_approximation.py` runs 8 threads (sleeper, lock-waiter, event-waiter,
queue-waiter, io-waiter, spinner, cpu-fibonacci, cpu-hash) and produces a pprof profile.

Verified in `go tool pprof -http :8080`:
- `off-cpu-time` (41.66s): lock/io/sleep/event/queue threads dominate; spinner/fib/hash tiny
- `cpu-time` (5.02s): only spinner/fib/hash appear; blocking threads absent
- The split is clean and correct.

pprof files land in macOS temp dir (not `/tmp`):
```bash
ls -lt /var/folders/2p/q3nbvkk15g76v9pwn8hdqqjw0000gp/T/offcpu_demo*.pprof
# decompress for go tool pprof (files are zstd, not gzip):
zstd -d <file> -c | gzip > /tmp/offcpu.pb.gz
go tool pprof -http :8080 /tmp/offcpu.pb.gz
```

### Candidate investigation

**ddprof** (`github.com/DataDog/ddprof`)
- C++, `perf_event_open` only — zero BPF code (confirmed by grepping the repo)
- Has `PerfWatcher` plugin architecture and tracepoint support, but raw `sched_switch`
  events would flood userspace without BPF-side accumulation
- No Python frame walking — shows `_PyEval_EvalFrameDefault`, not function names
- Not actively developed (beta/maintenance). **Ruled out.**

**Full Host Profiler** (`github.com/DataDog/dd-otel-host-profiler`)
- Go + cilium/ebpf wrapper around `go.opentelemetry.io/ebpf-profiler` (OTel, ex-Elastic)
- The upstream OTel project **already has everything**: `support/ebpf/off_cpu.ebpf.c`
  (sched_switch + kprobe/finish_task_switch, ring buffer), `interpreter/python/python.go`
  (Python frame walking from `/proc/<pid>/mem`, Python 3.6–3.14), `python_tracer.ebpf.c`
- The Datadog fork (`runner.go`) already calls `trc.AttachSchedMonitor()`
- **Why it doesn't solve our problem:**
  1. Not deployed on customer hosts — requires manual `sudo` binary install + `tracefs`
     mount + separate Datadog Agent. Most ddtrace users have never heard of it.
  2. No trace correlation — FHP doesn't know which ddtrace *span* was active during an
     off-CPU interval. That's the core product value we need.
  3. No greenlet/asyncio tracking at ddtrace depth.
- **Conclusion:** Right long-term home, wrong deployment model today. The spike informs
  a future conversation with the FHP team.

**libdd-heap-sampler** (`DataDog/libdatadog`, branch `sgg/heap-prof-poc`)
- In-process USDT emission (`ddheap:alloc`, `ddheap:free`) + external eBPF consumer (TODO)
- Different trigger (USDT vs sched_switch) but identical daemon infrastructure needed
- See "Convergence signal" section below.

### Key technical facts

**Kernel requirements & customer reach:**
The implementation as written has three kernel-feature floors, the highest of which wins:

| Feature | Min kernel | Notes |
|---------|-----------|-------|
| Kernel BTF / CO-RE (`/sys/kernel/btf/vmlinux`) | 5.4 | Needs `CONFIG_DEBUG_INFO_BTF=y` |
| `tp_btf/sched_switch` (BTF raw tracepoint) | 5.5 | Used by the spike for clean CO-RE |
| `BPF_MAP_TYPE_RINGBUF` | **5.8** | **The binding constraint** |

So the effective minimum is **kernel 5.8** (released Aug 2020). If we ever need to reach
5.5–5.7, swapping the ring buffer for a `BPF_MAP_TYPE_PERF_EVENT_ARRAY` perf buffer
(available since 4.3) drops the floor to the `tp_btf` requirement; classic
`tracepoint/sched/sched_switch` + perf buffer + no CO-RE would go lower still but loses
portability. Not worth it for the spike.

**Version checks are unreliable — prefer runtime feature detection.** Distros backport
heavily, so the kernel version string lies in both directions:
- **RHEL 8** ships kernel `4.18` but Red Hat backported BTF, CO-RE, and the BPF ring
  buffer (RHEL 8.2+). It *works* despite reporting 4.18. A naive `>= 5.8` gate would
  wrongly exclude it.
- **Ubuntu 20.04 LTS** GA kernel is `5.4` — BTF/CO-RE but **no** ring buffer. Its HWE
  kernel (`5.15`) is fine. Same distro, two outcomes.

The robust gate is to probe at startup: check `/sys/kernel/btf/vmlinux` exists, then try
to create a tiny `BPF_MAP_TYPE_RINGBUF` and load a trivial `tp_btf` program. If any probe
fails, disable the sidecar and fall back (see below). libbpf already surfaces these as
load-time errors.

**How likely are customers on unsupported kernels?**
Low and shrinking, and never a hard failure because of the fallback:
- **Supported out of the box:** RHEL 8/9, Ubuntu 22.04/24.04 (and 20.04 HWE), Debian
  11/12, Amazon Linux 2023, and effectively all current managed k8s (EKS/GKE/AKS default
  to 5.10/5.15+). This is the large majority of the modern fleet.
- **The notable laggard:** Ubuntu 20.04 on its *GA* 5.4 kernel (ring buffer absent).
  Still widely deployed but on its way out (standard support ended Apr 2025).
- **Genuinely too old / EOL:** RHEL 7 (3.10), Ubuntu 18.04 (4.15), Debian 10 (4.19),
  Amazon Linux 2 default (4.14). These are past or near end-of-life.

Crucially, the eBPF sidecar is an *enhancement*, not a replacement: the userspace
approximation (Option A) already ships and runs everywhere, including macOS and any
pre-5.8 kernel. On an unsupported kernel the runtime probe fails, the sidecar is not
launched, and ddtrace keeps emitting the approximation. So "old kernel" means "no precise
off-CPU," not "broken profiler." That makes 5.8 an acceptable floor for the spike.

**Why BPF over perf_event_open for off-CPU:**
`sched_switch` fires millions of times/sec on a busy host. With raw `perf_event_open`
you'd receive every event in userspace and correlate start/end there — extremely
expensive. BPF accumulates per-TID off-CPU duration in a kernel hash map and only emits
completed events to a ring buffer. The userspace daemon sees only the finished intervals.

**Python frame walking from outside the process:**
Walk: `/proc/<pid>/maps` → ELF symbol table → `_PyRuntime` address →
`PyInterpreterState` → `tstate_head` (match by `thread_id == tid`) →
`_PyCFrame` / `frame` chain → `code->co_qualname` (3.11+) / `co_name` (≤3.10).
Struct layouts differ per Python minor version — use a version-keyed offset table.
Detect Python version from `/proc/<pid>/cmdline` or ELF `.rodata`.
References: FHP `interpreter/python/python.go`, py-spy `src/python_spy.rs`.

**Privilege model (setcap):**
```bash
sudo setcap cap_bpf,cap_perfmon=ep /path/to/dd_offcpu  # once at install
# ddtrace then spawns it as plain subprocess, no sudo at runtime
```
Same model as `dumpcap` (Wireshark) and `ping`. Capability on the binary, not the
launching process.

**Language choice: C (not Go)**
- libbpf is C — reference implementation, best CO-RE support
- ddprof is C++ — if we eventually merge, stay in the same language
- No GC jitter in a profiler daemon processing high-frequency events
- FHP uses Go + cilium/ebpf but cilium/ebpf just loads precompiled BPF C objects;
  the BPF side is C regardless

---

## Convergence signal: libdd-heap-sampler

The `libdatadog` heap profiling effort (`sgg/heap-prof-poc` branch) is building toward
the same architecture from a different direction. It emits USDTs (`ddheap:alloc`,
`ddheap:free`) from inside the process and explicitly calls out "the entire eBPF full
host profiler side of things" as a TODO — meaning it needs an external eBPF daemon to
consume those USDTs and capture stacks.

That daemon would need exactly what our off-CPU sidecar needs:
- BPF program loading (different program, same infrastructure)
- Python frame walking from `/proc/<pid>/mem`
- pprof builder via libdatadog
- IPC channel back to ddtrace

Rather than two separate binaries growing independently, the natural convergence is a
single **ddtrace eBPF companion daemon** that loads different BPF programs based on
what is enabled:

| Feature | BPF hook | Status |
|---------|----------|--------|
| Off-CPU time | `tracepoint/sched/sched_switch` | this spike |
| Heap allocation | `usdt:ddheap:alloc` / `ddheap:free` | libdd-heap-sampler TODO |
| CPU (native) | `perf_event_open`, HW/SW event auto-selected at runtime | future |

This is worth watching: if `libdd-heap-sampler` ships its eBPF consumer, it should
share the daemon infrastructure we build here rather than spawn a second sidecar.
The off-CPU spike is effectively building the foundation of that shared daemon.

> **CPU event auto-selection:** At startup, attempt `perf_event_open` with
> `PERF_TYPE_HARDWARE, PERF_COUNT_HW_CPU_CYCLES`. If it returns `ENOENT` or `EPERM`
> (VM / container with no PMU exposure), fall back to
> `PERF_TYPE_SOFTWARE, PERF_COUNT_SW_CPU_CLOCK`. HW cycles are lower overhead and more
> precise; SW clock works universally. The probe-and-fallback is a single syscall at
> init time with no runtime cost.
