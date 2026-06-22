#!/usr/bin/env bash
#
# End-to-end smoke test for the eBPF off-CPU sidecar.
#
# Runs the real daemon against a tiny Python sleeper and asserts the off-CPU
# profile attributes the sleep to clock_nanosleep. This exercises the whole
# live path: BPF load/attach, the sched_switch hook, the ring buffer, native
# symbolization and the CPython frame walk.
#
# It needs a live kernel with BTF and cap_bpf/cap_perfmon (or root), so it
# SKIPS (exit 77, CTest SKIP_RETURN_CODE) rather than fails when those are
# unavailable. Inside the kernel-matrix VMs (separate PR) these preconditions
# always hold, so the skip becomes a real assertion.
#
# Usage: smoke_offcpu.sh <path-to-dd_offcpu>
set -u

SKIP_RC=77

skip() {
    echo "SKIP: $1" >&2
    exit $SKIP_RC
}

fail() {
    echo "FAIL: $1" >&2
    exit 1
}

DDOFFCPU="${1:-}"
PYTHON="${PYTHON:-python3}"

[ -n "$DDOFFCPU" ] && [ -x "$DDOFFCPU" ] || fail "dd_offcpu binary not found/executable: '$DDOFFCPU'"
command -v "$PYTHON" >/dev/null 2>&1 || skip "no python3 interpreter"

# --- capability / kernel gate ------------------------------------------------
[ -r /sys/kernel/btf/vmlinux ] || skip "no kernel BTF at /sys/kernel/btf/vmlinux"
if [ "$(id -u)" != "0" ]; then
    if command -v capsh >/dev/null 2>&1; then
        capsh --has-p=cap_bpf >/dev/null 2>&1 && capsh --has-p=cap_perfmon >/dev/null 2>&1 ||
            skip "not root and missing cap_bpf/cap_perfmon"
    else
        skip "not root and capsh unavailable to verify cap_bpf/cap_perfmon"
    fi
fi

# --- run ---------------------------------------------------------------------
OUT="$(mktemp "${TMPDIR:-/tmp}/offcpu_smoke.XXXXXX.pb.gz")"
LOG="$(mktemp "${TMPDIR:-/tmp}/offcpu_smoke.XXXXXX.log")"
WPID=""
DPID=""
cleanup() {
    [ -n "$DPID" ] && kill "$DPID" 2>/dev/null
    [ -n "$WPID" ] && kill "$WPID" 2>/dev/null
    rm -f "$OUT" "$LOG"
}
trap cleanup EXIT

# Sleeper: repeatedly blocks ~50 ms in clock_nanosleep.
"$PYTHON" -c 'import time
while True:
    time.sleep(0.05)' &
WPID=$!
sleep 0.3
kill -0 "$WPID" 2>/dev/null || fail "sleeper workload failed to start"

# 1 ms floor keeps the ~50 ms sleeps but drops scheduling noise.
"$DDOFFCPU" --pid "$WPID" --min-block-us 1000 --output "$OUT" >"$LOG" 2>&1 &
DPID=$!

# Wait for the readiness banner (or early exit).
ready=""
for _ in $(seq 1 50); do
    if grep -q "profiling pid" "$LOG" 2>/dev/null; then
        ready=1
        break
    fi
    kill -0 "$DPID" 2>/dev/null || break
    sleep 0.1
done
if [ -z "$ready" ]; then
    # Daemon never reached readiness. Distinguish "can't load BPF here" (skip)
    # from an unexpected failure.
    if grep -qiE "failed to (load|attach)|operation not permitted|permission denied" "$LOG"; then
        echo "----- dd_offcpu log -----" >&2
        cat "$LOG" >&2
        skip "kernel/permissions cannot load the BPF program here"
    fi
    echo "----- dd_offcpu log -----" >&2
    cat "$LOG" >&2
    fail "daemon did not reach readiness"
fi

# Let it collect a few off-CPU intervals, then stop and flush.
sleep 3
kill -INT "$DPID" 2>/dev/null
wait "$DPID" 2>/dev/null

[ -s "$OUT" ] || {
    echo "----- dd_offcpu log -----" >&2
    cat "$LOG" >&2
    fail "no profile written to $OUT"
}

# The string table of the gzipped pprof carries the symbol names; grep -a keeps
# this dependency-free (no go/protobuf/strings needed).
if gunzip -c "$OUT" | LC_ALL=C grep -aq "clock_nanosleep"; then
    echo "PASS: off-CPU profile attributes the sleeper to clock_nanosleep"
    exit 0
fi

echo "FAIL: clock_nanosleep not present in off-CPU profile" >&2
echo "names seen (sleep/futex/nanosleep):" >&2
gunzip -c "$OUT" | LC_ALL=C grep -aoE "[a-z_]*nanosleep|futex[a-z_]*|[a-z_]*sleep" | sort -u >&2 || true
exit 1
