#!/usr/bin/env python3
"""Measure dd-trace-py profiler heap / RSS coverage gap.

Establishes the empirical baseline for the Q2 success metric:
    "Workflow explains >= X% of RSS in >= Y% of profiling windows"

The profiler today only hooks PYMEM_DOMAIN_OBJ. This script measures how much
of a process's RSS is currently attributed ("heap-space" samples in the profile)
vs. invisible. Adding PYMEM_DOMAIN_MEM is expected to close 15-35pp of the gap.

USAGE
-----

Mode 1 — local pprof files (fastest, self-contained):
    # Run your workload with local profile output enabled:
    DD_PROFILING_OUTPUT_PPROF=/tmp/myapp ddtrace-run python your_workload.py

    # Then analyse the captured profiles:
    python scripts/measure_profiler_rss_coverage.py local /tmp/myapp

    # If you also know the RSS (e.g. from `ps` or another tool):
    python scripts/measure_profiler_rss_coverage.py local /tmp/myapp --rss-mb 512

Mode 2 — wrap a command (captures RSS automatically while it runs):
    python scripts/measure_profiler_rss_coverage.py wrap \\
        -- ddtrace-run python your_workload.py

Mode 3 — Datadog metrics API (fleet-wide RSS; requires DD_API_KEY + DD_APP_KEY):

    # Single service:
    DD_API_KEY=... DD_APP_KEY=... \\
    python scripts/measure_profiler_rss_coverage.py fleet \\
        --service my-python-svc --env prod --period 1d

    # Multiple explicit services (produces a ranked table):
    python scripts/measure_profiler_rss_coverage.py fleet \\
        --services svc-a svc-b svc-c --env prod --period 4h

    # Auto-discover all Python services from the metrics API, top 20 by RSS:
    python scripts/measure_profiler_rss_coverage.py fleet \\
        --services all --env prod --period 1h --top 20

NOTE ON MODE 3: heap is queried via POST /api/v2/query/timeseries with
    data_source=profiles (@prof_python_lifetime_heap_bytes). Requires profiling
    to be enabled on the target service (DD_PROFILING_ENABLED=true).

    Max practical lookback: ~30 days (profiles retention). Metrics API supports
    up to 15 months, but the heap side returns nothing after profiles are gone.
"""

from __future__ import annotations

import argparse
import dataclasses
import glob
import os
import pathlib
import re
import statistics
import subprocess
import sys
import threading
import time
from typing import Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# pprof_utils lives in the test tree; add it to path.
# ---------------------------------------------------------------------------
_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT / "tests" / "profiling" / "collector"))
sys.path.insert(0, str(_REPO_ROOT))


# ---------------------------------------------------------------------------
# .env loading  (no external dependency; handles both KEY=val and export KEY=val)
# ---------------------------------------------------------------------------

def _parse_env_file(path: pathlib.Path) -> Dict[str, str]:
    """Parse a .env file and return key/value pairs.

    Handles:
      KEY=value
      export KEY=value
      KEY="quoted value"
      # comments and blank lines
    """
    result: Dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return result
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        line = re.sub(r"^export\s+", "", line)
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip("'\"")
        if key:
            result[key] = val
    return result


def load_dotenv(*extra_paths: pathlib.Path) -> None:
    """Load .env files into os.environ (existing env vars take precedence).

    Search order (first match wins per key):
      1. Any paths passed explicitly (e.g. --env-file CLI flag)
      2. .env in the repo root
      3. ~/.env
    """
    candidates = list(extra_paths) + [
        _REPO_ROOT / ".env.prod",
        _REPO_ROOT / ".env",
        pathlib.Path.home() / ".env.prod",
        pathlib.Path.home() / ".env",
    ]
    for path in candidates:
        for key, val in _parse_env_file(path).items():
            os.environ.setdefault(key, val)  # env var already set wins


def _import_pprof_utils():
    try:
        import pprof_utils  # noqa: PLC0415
        return pprof_utils
    except ImportError as exc:
        sys.exit(
            f"Cannot import pprof_utils: {exc}\n"
            "Run this script from the dd-trace-py repo root, "
            "and ensure zstandard + google-protobuf are installed:\n"
            "  pip install zstandard protobuf"
        )


# ---------------------------------------------------------------------------
# Profile parsing
# ---------------------------------------------------------------------------

# Sample type strings emitted by the dd-trace-py memalloc collector.
HEAP_SPACE_TYPE = "heap-space"    # live (inuse) heap bytes  ← primary
ALLOC_SPACE_TYPE = "alloc-space"  # cumulative allocated bytes (fallback)


def _sum_heap_bytes(profile, value_type: str) -> Optional[int]:
    """Return sum of all samples for *value_type*, or None if not present."""
    try:
        import pprof_utils  # noqa: PLC0415
        idx = pprof_utils.get_sample_type_index(profile, value_type)
    except StopIteration:
        return None
    return sum(s.value[idx] for s in profile.sample if idx < len(s.value))


def heap_bytes_from_pprof(path: str) -> Optional[Tuple[int, str]]:
    """Parse a single .pprof file and return (heap_live_bytes, sample_type_name).

    Returns the sum of heap-space (live in-use bytes) samples. Profiles that
    only contain alloc-space (cumulative bytes since last upload) are skipped
    with a warning, because alloc-space is not comparable to RSS.
    """
    pu = _import_pprof_utils()
    try:
        profile = pu.parse_profile(path)
    except Exception as exc:
        print(f"  skip {os.path.basename(path)}: {exc}", file=sys.stderr)
        return None

    total = _sum_heap_bytes(profile, HEAP_SPACE_TYPE)
    if total is not None:
        return total, HEAP_SPACE_TYPE

    types = [profile.string_table[st.type] for st in profile.sample_type]
    print(
        f"  WARN skip {os.path.basename(path)}: no '{HEAP_SPACE_TYPE}' sample type "
        f"(available: {types}). alloc-space is cumulative-since-last-upload and "
        "is not comparable to RSS, so it is not used as a fallback.",
        file=sys.stderr,
    )
    return None


def find_pprof_files(prefix_or_file: str) -> List[str]:
    if os.path.isfile(prefix_or_file):
        return [prefix_or_file]
    # Glob: <prefix>.<pid>.<seq>.pprof
    files = sorted(glob.glob(f"{prefix_or_file}*.pprof"),
                   key=lambda f: int(f.rsplit(".", 2)[-2]))
    return files


# ---------------------------------------------------------------------------
# RSS helpers
# ---------------------------------------------------------------------------

def rss_bytes_proc(pid: int) -> Optional[int]:
    """Read RSS for the given PID. Returns bytes. Works on Linux and macOS."""
    # Linux: /proc/<pid>/status (VmRSS in kB)
    try:
        with open(f"/proc/{pid}/status") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) * 1024
    except OSError:
        pass
    # psutil if available (any platform)
    try:
        import psutil  # noqa: PLC0415
        return psutil.Process(pid).memory_info().rss
    except Exception:
        pass
    # macOS / BSD fallback: ps -o rss= reports KB
    try:
        out = subprocess.check_output(
            ["ps", "-p", str(pid), "-o", "rss="],
            stderr=subprocess.DEVNULL,
        )
        kb = int(out.strip())
        return kb * 1024
    except Exception:
        return None


def rss_bytes_current_process() -> Optional[int]:
    return rss_bytes_proc(os.getpid())


# ---------------------------------------------------------------------------
# Statistics output
# ---------------------------------------------------------------------------

def _percentile(sorted_vals: List[float], q: float) -> float:
    """Inclusive-method percentile.

    Accepts a sorted list (the historical signature) and uses
    statistics.quantiles for proper interpolation. Falls back to the single
    value when the list has length <= 1.
    """
    n = len(sorted_vals)
    if n == 0:
        raise ValueError("_percentile called with empty list")
    if n == 1:
        return sorted_vals[0]
    # statistics.quantiles with n=100 produces 99 cut points: index 0 is p1.
    # For q=0.50 we want cuts[49]; for q in (0,1) generally cuts[round(q*100)-1].
    q_int = max(1, min(99, int(round(q * 100))))
    cuts = statistics.quantiles(sorted_vals, n=100, method="inclusive")
    return cuts[q_int - 1]


def print_stats(
    ratios: List[float],
    label: str = "",
    sample_type: str = HEAP_SPACE_TYPE,
    coverage_threshold_pct: float = 50.0,
) -> None:
    if not ratios:
        print("  (no data - check that profiles contain heap samples)")
        return

    s = sorted(ratios)
    p = lambda q: _percentile(s, q)  # noqa: E731
    threshold = coverage_threshold_pct / 100.0
    n_above = sum(1 for x in s if x >= threshold)
    survival_pct = 100.0 * n_above / len(s)

    print(f"\n{'=' * 64}")
    print(f"  {label or 'Profiler heap / RSS  (PYMEM_DOMAIN_OBJ only)'}")
    print(f"  sample type : {sample_type}")
    print(f"  windows     : {len(ratios)}")
    print(f"  p25  = {p(0.25)*100:5.1f}%")
    print(f"  p50  = {p(0.50)*100:5.1f}%  <- baseline X for Q2 goal")
    print(f"  p75  = {p(0.75)*100:5.1f}%")
    print(f"  p90  = {p(0.90)*100:5.1f}%")
    print(f"  mean = {statistics.mean(ratios)*100:5.1f}%")
    print(f"  min  = {min(ratios)*100:5.1f}%   max = {max(ratios)*100:5.1f}%")
    print(
        f"  P(ratio >= {coverage_threshold_pct:.0f}%) = "
        f"{survival_pct:.1f}% of windows"
    )
    print(f"{'=' * 64}")
    print("\n  Projected after PYMEM_DOMAIN_MEM (Q2):")
    print(
        "    Conservative commitment: +3 to +8pp at p50 "
        "(under-promise / over-deliver)."
    )
    print(
        "    Stretch (services dominated by list/array/dict-of-primitives use): "
        "+10 to +20pp."
    )
    print(
        "    Validate by re-running on a canary with "
        "DD_PROFILING_MEM_DOMAIN_ENABLED=true before committing a final number."
    )


# ---------------------------------------------------------------------------
# Mode 1: local pprof files
# ---------------------------------------------------------------------------

def run_local(args: argparse.Namespace) -> None:
    files = find_pprof_files(args.prefix)
    if not files:
        sys.exit(f"No .pprof files found matching '{args.prefix}*.pprof'")

    print(f"Found {len(files)} pprof file(s) at '{args.prefix}'")

    rss_override_bytes = int(args.rss_mb * 1024 * 1024) if args.rss_mb else None
    rss_samples: List[Tuple[float, int]] = []
    if rss_override_bytes is None:
        rss_samples = _read_rss_csv(args.prefix)
        if rss_samples:
            print(f"  Loaded {len(rss_samples)} RSS samples from {_rss_csv_path(args.prefix)}")

    ratios: List[float] = []

    for f in files:
        result = heap_bytes_from_pprof(f)
        if result is None:
            continue
        heap_b, stype = result

        rss_b: Optional[int] = rss_override_bytes
        if rss_b is None and rss_samples:
            rss_b = _rss_for_profile(f, rss_samples)
        if rss_b is None:
            print(
                f"  {os.path.basename(f)}: heap={heap_b/1e6:.1f}MB  RSS=? "
                f"(pass --rss-mb or generate {_rss_csv_path(args.prefix)} via wrap mode)"
            )
            continue

        ratio = heap_b / rss_b
        ratios.append(ratio)
        print(
            f"  {os.path.basename(f):40s}  "
            f"heap={heap_b/1e6:6.1f}MB / RSS={rss_b/1e6:6.1f}MB = "
            f"{ratio*100:5.1f}%  [{stype}]"
        )

    print_stats(ratios)


# ---------------------------------------------------------------------------
# Mode 2: wrap a subprocess, poll RSS, then analyse profiles
# ---------------------------------------------------------------------------

def _rss_csv_path(prefix: str) -> str:
    return f"{prefix}.rss.csv"


def _write_rss_csv(prefix: str, samples: List[Tuple[float, int]]) -> str:
    """Write [(unix_ts_seconds, rss_bytes), ...] to <prefix>.rss.csv."""
    path = _rss_csv_path(prefix)
    with open(path, "w") as fh:
        fh.write("ts_seconds,rss_bytes\n")
        for ts, rss in samples:
            fh.write(f"{ts:.3f},{rss}\n")
    return path


def _read_rss_csv(prefix: str) -> List[Tuple[float, int]]:
    path = _rss_csv_path(prefix)
    if not os.path.exists(path):
        return []
    out: List[Tuple[float, int]] = []
    with open(path) as fh:
        next(fh, None)  # header
        for line in fh:
            parts = line.strip().split(",")
            if len(parts) != 2:
                continue
            try:
                out.append((float(parts[0]), int(parts[1])))
            except ValueError:
                continue
    return out


def _rss_for_profile(
    profile_path: str, rss_samples: List[Tuple[float, int]], window_secs: float = 60.0
) -> Optional[int]:
    """Median RSS within ±window_secs of the profile file's mtime."""
    if not rss_samples:
        return None
    try:
        mtime = os.path.getmtime(profile_path)
    except OSError:
        return None
    matches = [rss for ts, rss in rss_samples if abs(ts - mtime) <= window_secs]
    if not matches:
        return None
    return int(statistics.median(matches))


def run_wrap(args: argparse.Namespace) -> None:
    import tempfile  # noqa: PLC0415

    out_dir = tempfile.mkdtemp(prefix="dd_profiler_coverage_")
    prefix = os.path.join(out_dir, "profile")
    print(f"Writing profiles to: {prefix}.<pid>.<seq>.pprof")
    print(f"RSS samples will be written to: {_rss_csv_path(prefix)}")
    print(f"Command: {' '.join(args.cmd)}\n")

    env = os.environ.copy()
    env["DD_PROFILING_OUTPUT_PPROF"] = prefix
    env.setdefault("DD_PROFILING_ENABLED", "true")

    rss_samples: List[Tuple[float, int]] = []
    stop_event = threading.Event()

    proc = subprocess.Popen(args.cmd, env=env)

    def _poll_rss() -> None:
        while not stop_event.wait(timeout=5.0):
            r = rss_bytes_proc(proc.pid)
            if r is not None:
                rss_samples.append((time.time(), r))

    poller = threading.Thread(target=_poll_rss, daemon=True)
    poller.start()
    proc.wait()
    stop_event.set()
    poller.join(timeout=2)

    if not rss_samples:
        sys.exit("Process exited before any RSS samples were collected.")

    csv_path = _write_rss_csv(prefix, rss_samples)
    rss_only = [r for _, r in rss_samples]
    rss_median = statistics.median(rss_only)
    print(
        f"\nRSS samples: n={len(rss_samples)}  median={rss_median/1e6:.0f}MB  "
        f"max={max(rss_only)/1e6:.0f}MB  csv={csv_path}"
    )

    files = find_pprof_files(prefix)
    if not files:
        sys.exit(
            f"No .pprof files found in {out_dir}. "
            "Check that dd-trace-py profiler started (DD_PROFILING_ENABLED=true)."
        )

    print(f"Found {len(files)} profile(s). Analysing per-window…")
    ratios: List[float] = []

    for f in files:
        result = heap_bytes_from_pprof(f)
        if result is None:
            continue
        heap_b, stype = result
        rss_b = _rss_for_profile(f, rss_samples)
        if rss_b is None:
            print(
                f"  {os.path.basename(f)}: heap={heap_b/1e6:.1f}MB  "
                "RSS=? (no samples within window)"
            )
            continue
        ratio = heap_b / rss_b
        ratios.append(ratio)
        print(
            f"  {os.path.basename(f):40s}  "
            f"heap={heap_b/1e6:6.1f}MB / RSS={rss_b/1e6:6.1f}MB = "
            f"{ratio*100:5.1f}%  [{stype}]"
        )

    print_stats(ratios)


# ---------------------------------------------------------------------------
# Mode 3: Datadog APIs — RSS (metrics) + heap (profiles timeseries)
# ---------------------------------------------------------------------------

# The profiler heap lives in the Profiles backend.
#
# Datadog exposes two byte-valued profile fields:
#   @prof_python_inuse_heap_bytes     - currently-alive bytes  (corresponds to
#                                       pprof 'heap-space'). This is the field
#                                       that should be compared to RSS.
#   @prof_python_lifetime_heap_bytes  - cumulative bytes allocated over the
#                                       process lifetime, including freed
#                                       allocations. Not comparable to RSS.
#
# Queried via POST /api/v2/query/timeseries with data_source=profiles - a
# public, versioned Datadog API that accepts standard API+APP key auth.
_HEAP_PROFILE_FIELD_INUSE = "@prof_python_inuse_heap_bytes"
_HEAP_PROFILE_FIELD_LIFETIME = "@prof_python_lifetime_heap_bytes"
# @prof_python_lifetime_heap_bytes is the correct field despite its name.
# It stores the current live-heap snapshot (pprof 'heap-space'), not a
# cumulative sum. Validated against the Profile Explorer 'Heap Live Size'
# view on 2026-05-08: median=20.5 MB matched the UI's 19-20 MiB.
# @prof_python_inuse_heap_bytes does not exist in the Datadog backend.
_HEAP_PROFILE_FIELD_DEFAULT = _HEAP_PROFILE_FIELD_LIFETIME
# Backward-compat alias used by existing print sites.
_HEAP_PROFILE_FIELD = _HEAP_PROFILE_FIELD_DEFAULT

# Human-friendly period aliases → seconds.
# Max practical lookback: 30d (profiles retention). Metrics API supports 15 months
# but the heap side returns nothing after profiles age out.
_PERIOD_PRESETS: Dict[str, int] = {
    "1h": 3_600,
    "4h": 4 * 3_600,
    "12h": 12 * 3_600,
    "1d": 86_400,
    "3d": 3 * 86_400,
    "7d": 7 * 86_400,
    "30d": 30 * 86_400,
}


def _parse_period(s: str) -> int:
    """Parse a period string into seconds. Accepts presets (1h, 4h, 12h, 1d, 3d, 7d, 30d)
    or any Nh / Nd pattern."""
    if s in _PERIOD_PRESETS:
        return _PERIOD_PRESETS[s]
    m: Optional[re.Match[str]] = re.match(r"^(\d+)([hd])$", s)
    if m:
        n: int = int(m.group(1))
        unit: str = m.group(2)
        return n * (3_600 if unit == "h" else 86_400)
    raise argparse.ArgumentTypeError(
        f"Invalid period '{s}'. "
        f"Supported presets: {', '.join(_PERIOD_PRESETS)}. "
        "Or use Nh/Nd format, e.g. 6h, 2d."
    )


@dataclasses.dataclass
class ServiceResult:
    """Query result for a single service.

    `ratios` is the per-window distribution: one ratio per matched
    (runtime-id, time-bucket) pair. `ratio_pct` is the p50 of `ratios`,
    kept for backward-compat with the existing table.
    """

    service: str
    rss_p50_mb: Optional[float] = None
    heap_avg_mb: Optional[float] = None
    ratio_pct: Optional[float] = None
    rss_windows: int = 0
    ratios: List[float] = dataclasses.field(default_factory=list)
    n_runtime_ids: int = 0


def _query_rss(session, site: str, service: str, env: str, start: int, end: int) -> List[float]:
    """Return all runtime.python.mem.rss point values for the window (flat).

    Kept for the single-service summary header. For per-runtime-id alignment,
    use _query_rss_by_runtime_id instead.
    """
    query = f"avg:runtime.python.mem.rss{{service:{service},env:{env}}}"
    resp = session.get(
        f"https://api.{site}/api/v1/query",
        params={"from": start, "to": end, "query": query},
        timeout=30,
    )
    resp.raise_for_status()
    return [
        val
        for series in resp.json().get("series", [])
        for _, val in series.get("pointlist", [])
        if val is not None
    ]


def _extract_tag(tag_set: List[str], key: str) -> Optional[str]:
    prefix = f"{key}:"
    for tag in tag_set:
        if tag.startswith(prefix):
            return tag[len(prefix):]
    return None


def _query_rss_by_runtime_id(
    session, site: str, service: str, env: str, start: int, end: int
) -> Dict[str, List[Tuple[float, float]]]:
    """Return RSS points grouped by runtime-id (one series per process).

    Each value is (timestamp_ms, rss_bytes). The per-process resolution is
    what makes per-window ratio comparisons meaningful: RSS aggregated across
    all workers on a host cannot be compared to a single profile's heap.

    Falls back to grouping by host when runtime-id has no values; an empty
    dict means the metric isn't tagged at the requested resolution.
    """
    out: Dict[str, List[Tuple[float, float]]] = {}
    for group_key in ("runtime-id", "host"):
        query = (
            f"avg:runtime.python.mem.rss"
            f"{{service:{service},env:{env}}}by{{{group_key}}}"
        )
        resp = session.get(
            f"https://api.{site}/api/v1/query",
            params={"from": start, "to": end, "query": query},
            timeout=30,
        )
        resp.raise_for_status()
        series_list = resp.json().get("series", [])
        for series in series_list:
            tag_set = series.get("tag_set", [])
            grp = _extract_tag(tag_set, group_key)
            if grp is None:
                continue
            pts = [
                (float(ts), float(v))
                for ts, v in series.get("pointlist", [])
                if v is not None
            ]
            if not pts:
                continue
            out.setdefault(grp, []).extend(pts)
        if out:
            return out
    return out


def _heap_timeseries(
    session,
    site: str,
    service: str,
    env: str,
    start: int,
    end: int,
    field: str,
    interval_ms: int = 60000,
) -> List[float]:
    """Return raw per-bucket heap values for *field*. Empty list on no data."""
    url: str = f"https://api.{site}/api/v2/query/timeseries"
    search_query: str = f"service:{service} env:{env} source:python"
    body: Dict = {
        "data": {
            "type": "timeseries_request",
            "attributes": {
                "formulas": [{"formula": "query1"}],
                "from": start * 1000,
                "to": end * 1000,
                "interval": interval_ms,
                "queries": [{
                    "name": "query1",
                    "data_source": "profiles",
                    "search": {"query": search_query},
                    "compute": {"aggregation": "avg", "metric": field},
                    "indexes": ["*"],
                }],
            },
        }
    }
    resp = session.post(url, json=body, timeout=30)
    if resp.status_code == 400:
        return []
    resp.raise_for_status()
    return [
        v
        for series in resp.json().get("data", {}).get("attributes", {}).get("values", [])
        for v in series
        if v is not None
    ]


def _query_heap(
    session,
    site: str,
    service: str,
    env: str,
    start: int,
    end: int,
    field: str = _HEAP_PROFILE_FIELD_DEFAULT,
) -> Optional[float]:
    """Return median heap field value over the window, or None.

    *field* defaults to @prof_python_inuse_heap_bytes (live heap, comparable
    to RSS). Pass _HEAP_PROFILE_FIELD_LIFETIME for the cumulative variant.

    Uses POST /api/v2/query/timeseries with data_source=profiles - a public,
    versioned Datadog API that accepts standard API+APP key authentication.
    Returns None when the service has no profiling data in this window.
    """
    values = _heap_timeseries(session, site, service, env, start, end, field)
    return statistics.median(values) if values else None


def _heap_timeseries_by_runtime_id(
    session,
    site: str,
    service: str,
    env: str,
    start: int,
    end: int,
    field: str,
    interval_ms: int = 60000,
) -> Dict[str, List[Tuple[float, float]]]:
    """Return per-runtime-id heap timeseries.

    Uses v2 timeseries with `group_by` on @runtime-id. If the API rejects
    the grouped query (400) or returns ungrouped data, returns an empty dict
    so callers can fall back. Each value is (bucket_start_ms, bytes).
    """
    url: str = f"https://api.{site}/api/v2/query/timeseries"
    search_query: str = f"service:{service} env:{env} source:python"
    body: Dict = {
        "data": {
            "type": "timeseries_request",
            "attributes": {
                "formulas": [{"formula": "query1"}],
                "from": start * 1000,
                "to": end * 1000,
                "interval": interval_ms,
                "queries": [{
                    "name": "query1",
                    "data_source": "profiles",
                    "search": {"query": search_query},
                    "compute": {"aggregation": "avg", "metric": field},
                    "group_by": [{"facet": "@runtime-id", "limit": 200}],
                    "indexes": ["*"],
                }],
            },
        }
    }
    resp = session.post(url, json=body, timeout=60)
    if resp.status_code == 400:
        return {}
    resp.raise_for_status()
    payload = resp.json().get("data", {}).get("attributes", {})
    times: List[int] = payload.get("times", []) or []
    series_list: List[Dict] = payload.get("series", []) or []
    values_block: List[List[Optional[float]]] = payload.get("values", []) or []
    out: Dict[str, List[Tuple[float, float]]] = {}
    for idx, series in enumerate(series_list):
        runtime_id: Optional[str] = None
        group_tags = series.get("group_tags") or series.get("tags") or []
        for t in group_tags:
            if isinstance(t, str) and t.startswith("@runtime-id:"):
                runtime_id = t[len("@runtime-id:"):]
                break
            if isinstance(t, str) and t.startswith("runtime-id:"):
                runtime_id = t[len("runtime-id:"):]
                break
        if runtime_id is None:
            continue
        if idx >= len(values_block):
            continue
        vals = values_block[idx]
        pts = [
            (float(times[i]), float(v))
            for i, v in enumerate(vals)
            if v is not None and i < len(times)
        ]
        if pts:
            out.setdefault(runtime_id, []).extend(pts)
    return out


def _per_window_ratios(
    rss_by_id: Dict[str, List[Tuple[float, float]]],
    heap_by_id: Dict[str, List[Tuple[float, float]]],
    bucket_ms: int = 60_000,
) -> List[float]:
    """Match RSS and heap series by runtime-id then by time bucket.

    RSS is converted to seconds when comparing to heap (which is in ms).
    Returns one ratio per matched (runtime-id, bucket) pair. RSS samples
    are bucketized to `bucket_ms`; for each bucket we use the median RSS
    sample from that bucket and divide the heap value by it.
    """
    ratios: List[float] = []
    for rid, heap_pts in heap_by_id.items():
        rss_pts = rss_by_id.get(rid)
        if not rss_pts:
            continue
        # Bucketize RSS by floor(ts / bucket_ms). RSS series can be ms or s
        # depending on API path; v1 metrics returns seconds * 1000.
        rss_buckets: Dict[int, List[float]] = {}
        for ts, val in rss_pts:
            b = int(ts // bucket_ms)
            rss_buckets.setdefault(b, []).append(val)
        rss_med: Dict[int, float] = {
            b: statistics.median(vs) for b, vs in rss_buckets.items() if vs
        }
        for ts, heap_val in heap_pts:
            b = int(ts // bucket_ms)
            rss = rss_med.get(b)
            if rss is None or rss <= 0:
                continue
            ratios.append(heap_val / rss)
    return ratios


def _validate_heap_field_choice(
    session, site: str, service: str, env: str, start: int, end: int
) -> None:
    """Print both inuse and lifetime medians side-by-side once for sanity check.

    Operators eyeball this against the Datadog Profile Explorer 'Heap Live Size'
    figure for the same window. The matching field is the one to use; if they
    differ by orders of magnitude, lifetime is cumulative-since-process-start
    and must NOT be used to compute RSS coverage.
    """
    print(
        "Field validation (compare against Profile Explorer 'Heap Live Size'):",
        flush=True,
    )
    for field in (_HEAP_PROFILE_FIELD_INUSE, _HEAP_PROFILE_FIELD_LIFETIME):
        try:
            vals = _heap_timeseries(session, site, service, env, start, end, field)
        except Exception as exc:
            print(f"  {field}: error: {exc}")
            continue
        if not vals:
            print(f"  {field}: (no data)")
            continue
        med = statistics.median(vals)
        print(
            f"  {field}: median={med/1e6:.1f}MB  min={min(vals)/1e6:.1f}MB  "
            f"max={max(vals)/1e6:.1f}MB  n={len(vals)}"
        )
    print()


def _discover_python_services(session, site: str, env: str,
                               start: int, end: int) -> List[str]:
    """Return all service names emitting runtime.python.mem.rss, sorted by RSS descending."""
    query: str = f"avg:runtime.python.mem.rss{{env:{env}}}by{{service}}"
    resp = session.get(
        f"https://api.{site}/api/v1/query",
        params={"from": start, "to": end, "query": query},
        timeout=60,
    )
    resp.raise_for_status()

    service_rss: List[Tuple[str, float]] = []
    for series in resp.json().get("series", []):
        name: Optional[str] = None
        for tag in series.get("tag_set", []):
            if tag.startswith("service:"):
                name = tag[len("service:"):]
                break
        if name is None:
            continue
        vals: List[float] = [v for _, v in series.get("pointlist", []) if v is not None]
        med: float = statistics.median(vals) if vals else 0.0
        service_rss.append((name, med))

    service_rss.sort(key=lambda x: x[1], reverse=True)
    return [svc for svc, _ in service_rss]


def _query_single_service(
    session,
    site: str,
    service: str,
    env: str,
    start: int,
    end: int,
    heap_field: str = _HEAP_PROFILE_FIELD_DEFAULT,
) -> ServiceResult:
    """Query RSS and heap per runtime-id; compute per-window ratios.

    The returned `ratios` list contains one entry per (runtime-id, bucket)
    pair where both RSS and heap data exist. Aggregate statistics
    (`ratio_pct`, `rss_p50_mb`, `heap_avg_mb`) are derived from the matched
    pairs only, so a process with RSS but no profiles is not counted.
    """
    result: ServiceResult = ServiceResult(service=service)

    rss_by_id: Dict[str, List[Tuple[float, float]]] = {}
    try:
        rss_by_id = _query_rss_by_runtime_id(session, site, service, env, start, end)
    except Exception as exc:
        print(f"  [{service}] RSS query error: {exc}", file=sys.stderr)

    heap_by_id: Dict[str, List[Tuple[float, float]]] = {}
    try:
        heap_by_id = _heap_timeseries_by_runtime_id(
            session, site, service, env, start, end, heap_field
        )
    except Exception as exc:
        print(f"  [{service}] profiles API error: {exc}", file=sys.stderr)

    if rss_by_id:
        all_rss = [v for pts in rss_by_id.values() for _, v in pts]
        if all_rss:
            result.rss_p50_mb = _percentile(sorted(all_rss), 0.50) / 1e6
            result.rss_windows = len(all_rss)

    if heap_by_id:
        all_heap = [v for pts in heap_by_id.values() for _, v in pts]
        if all_heap:
            result.heap_avg_mb = statistics.median(all_heap) / 1e6

    if rss_by_id and heap_by_id:
        ratios = _per_window_ratios(rss_by_id, heap_by_id)
        result.ratios = ratios
        matched_ids = set(rss_by_id) & set(heap_by_id)
        result.n_runtime_ids = len(matched_ids)
        if ratios:
            result.ratio_pct = _percentile(sorted(ratios), 0.50) * 100

    return result


def _print_multi_table(
    results: List[ServiceResult],
    window_label: str,
    coverage_threshold_pct: float = 50.0,
) -> None:
    """Print a ranked table of ServiceResults plus fleet-wide survival stats.

    For each service we report the p50 of its per-window ratios. The fleet
    summary also shows P(ratio >= coverage_threshold_pct) computed across
    every per-window data point in the fleet, which is the right denominator
    for the Q2 statement "X% of RSS in Y% of profiling windows".
    """
    with_ratio: List[ServiceResult] = sorted(
        [r for r in results if r.ratio_pct is not None],
        key=lambda r: r.ratio_pct,  # type: ignore[return-value]
        reverse=True,
    )
    without_ratio: List[ServiceResult] = [r for r in results if r.ratio_pct is None]
    ordered: List[ServiceResult] = with_ratio + without_ratio

    svc_w: int = max(len("service"), max(len(r.service) for r in results))
    sep: str = "-" * (svc_w + 2 + 9 + 2 + 9 + 2 + 8 + 2 + 8 + 2 + 8)

    print(f"\n{sep}")
    print(f"Fleet: {window_label}  ({len(results)} services queried)")
    print(sep)
    print(
        f"{'service':<{svc_w}}  {'RSS p50':>9}  {'Heap p50':>9}  "
        f"{'Ratio p50':>8}  {'Windows':>8}  {'Procs':>8}"
    )
    print(sep)

    for r in ordered:
        rss_s: str = f"{r.rss_p50_mb:.0f} MB" if r.rss_p50_mb is not None else "--"
        heap_s: str = f"{r.heap_avg_mb:.0f} MB" if r.heap_avg_mb is not None else "--"
        ratio_s: str = f"{r.ratio_pct:.1f}%" if r.ratio_pct is not None else "--"
        n_windows = len(r.ratios)
        print(
            f"{r.service:<{svc_w}}  {rss_s:>9}  {heap_s:>9}  "
            f"{ratio_s:>8}  {n_windows:>8}  {r.n_runtime_ids:>8}"
        )

    print(sep)
    all_ratios: List[float] = [x for r in results for x in r.ratios]
    per_service_p50: List[float] = [
        r.ratio_pct for r in results if r.ratio_pct is not None  # type: ignore[misc]
    ]
    if all_ratios:
        srt = sorted(all_ratios)
        med_window = _percentile(srt, 0.50) * 100
        p25_window = _percentile(srt, 0.25) * 100
        p75_window = _percentile(srt, 0.75) * 100
        threshold = coverage_threshold_pct / 100.0
        n_above = sum(1 for x in srt if x >= threshold)
        survival_pct = 100.0 * n_above / len(srt)
        print(
            f"  Per-window ratios across fleet: "
            f"p25={p25_window:.1f}%  p50={med_window:.1f}%  p75={p75_window:.1f}%  "
            f"(n={len(srt)})"
        )
        print(
            f"  P(ratio >= {coverage_threshold_pct:.0f}%) = "
            f"{survival_pct:.1f}% of profiling windows"
        )
        if per_service_p50:
            cross = statistics.median(per_service_p50)
            print(f"  Cross-service p50 of per-service p50: {cross:.1f}%")
        print(sep)
        print("\nProjected after PYMEM_DOMAIN_MEM (Q2):")
        print(
            f"  Conservative commitment: +3 to +8pp at p50 across the fleet "
            f"(under-promise / over-deliver)."
        )
        print(
            "  Stretch (services with heavy list/array/dict-of-primitives use): "
            "+10 to +20pp."
        )
        print(
            "  Validate by re-running this script on a canary with "
            "DD_PROFILING_MEM_DOMAIN_ENABLED=true."
        )
    else:
        print(sep)


def run_fleet(args: argparse.Namespace) -> None:
    try:
        import requests  # noqa: PLC0415
    except ImportError:
        sys.exit("pip install requests  (required for fleet mode)")

    env_file: Optional[pathlib.Path] = (
        pathlib.Path(args.env_file) if getattr(args, "env_file", None) else None
    )
    load_dotenv(*([env_file] if env_file else []))

    api_key: str = os.environ.get("DD_API_KEY", "")
    app_key: str = os.environ.get("DD_APP_KEY", "")
    missing: List[str] = [k for k, v in [("DD_API_KEY", api_key), ("DD_APP_KEY", app_key)] if not v]
    if missing:
        sys.exit(
            f"Missing credentials: {', '.join(missing)}\n"
            "Add them to .env in the repo root, ~/.env, or pass --env-file:\n"
            f"  {chr(10).join(f'  {k}=<your-key>' for k in missing)}"
        )

    # Resolve time window: --period takes priority over legacy --hours.
    if getattr(args, "period", None) and getattr(args, "hours", None) != 24:
        sys.exit("Specify either --period or --hours, not both.")
    if getattr(args, "period", None):
        window_secs: int = _parse_period(args.period)
        window_label: str = args.period
    else:
        window_secs = (args.hours or 24) * 3600
        h: int = args.hours or 24
        window_label = f"{h}h" if h % 24 != 0 else f"{h // 24}d"

    # Resolve service list.
    services_arg: Optional[List[str]] = getattr(args, "services", None)
    service_arg: Optional[str] = getattr(args, "service", None)
    if services_arg:
        raw_services: List[str] = services_arg
    elif service_arg:
        raw_services = [service_arg]
    else:
        sys.exit("Provide --service SERVICE or --services svc1 svc2 ... (or 'all')")

    site: str = os.environ.get("DD_SITE", "datadoghq.com")
    session = requests.Session()
    session.headers.update({"DD-API-KEY": api_key, "DD-APPLICATION-KEY": app_key})

    now: int = int(time.time())
    start: int = now - window_secs

    heap_field: str = getattr(args, "heap_field", _HEAP_PROFILE_FIELD_DEFAULT)
    if getattr(args, "validate_fields", False):
        # Use the first explicit service for validation (don't burn the
        # auto-discovery call just for this).
        validate_svc: Optional[str] = None
        if getattr(args, "service", None):
            validate_svc = args.service
        elif getattr(args, "services", None) and args.services != ["all"]:
            validate_svc = args.services[0]
        if validate_svc:
            _validate_heap_field_choice(session, site, validate_svc, args.env, start, now)

    # Auto-discover all Python services when the special value 'all' is given.
    if raw_services == ["all"]:
        print(f"Discovering Python services in env={args.env} over last {window_label}…")
        all_services: List[str] = _discover_python_services(session, site, args.env, start, now)
        if not all_services:
            sys.exit(
                f"No runtime.python.mem.rss data found for env={args.env} "
                f"over the last {window_label}."
            )
        print(f"  Found {len(all_services)} service(s) with Python runtime metrics.")
        top: int = getattr(args, "top", 0) or 0
        services: List[str] = all_services[:top] if top else all_services
        if top and len(all_services) > top:
            print(f"  Limiting to top {top} by median RSS.")
    else:
        services = raw_services

    single: bool = len(services) == 1

    if single:
        # Single-service: detailed output with per-window distribution.
        svc: str = services[0]
        print(f"Querying RSS by runtime-id (runtime.python.mem.rss) for {svc}…")
        result = _query_single_service(
            session, site, svc, args.env, start, now, heap_field=heap_field
        )

        if not result.ratios:
            if result.rss_p50_mb is None:
                sys.exit(
                    f"No runtime.python.mem.rss data for service={svc}, env={args.env}.\n"
                    "Check DD_RUNTIME_METRICS_ENABLED=true is set on the service."
                )
            if result.heap_avg_mb is None:
                print(
                    f"  No profiling data found for service={svc}, env={args.env}.\n"
                    "  Check that DD_PROFILING_ENABLED=true is set and profiles are "
                    "reaching the backend."
                )
                print(
                    f"\n  RSS-only result: p50={result.rss_p50_mb:.0f}MB over last "
                    f"{window_label}"
                )
                return
            sys.exit(
                "RSS and heap timeseries did not share any (runtime-id, bucket) pair.\n"
                "This usually means the metric is not tagged by runtime-id. "
                "Re-run after enabling runtime-id tagging on the agent."
            )

        srt = sorted(result.ratios)
        rp = lambda q: _percentile(srt, q)  # noqa: E731
        print(f"\n{'=' * 72}")
        print(f"  Profiler heap / RSS  -  service={svc}, env={args.env}")
        print(f"  heap field    : {heap_field}")
        print(
            f"  RSS p50       : {result.rss_p50_mb:.0f}MB  (n={result.rss_windows} samples)"
        )
        print(f"  Heap p50      : {result.heap_avg_mb:.0f}MB")
        print(f"  Matched procs : {result.n_runtime_ids}")
        print(f"  Per-window ratio distribution (n={len(srt)}):")
        print(
            f"    p25={rp(0.25)*100:5.1f}%  p50={rp(0.50)*100:5.1f}%  "
            f"p75={rp(0.75)*100:5.1f}%  p90={rp(0.90)*100:5.1f}%"
        )
        threshold = 0.50
        n_above = sum(1 for x in srt if x >= threshold)
        print(
            f"  P(ratio >= {threshold*100:.0f}%) = "
            f"{100.0 * n_above / len(srt):.1f}% of windows"
        )
        print(f"{'=' * 72}")
        print("\n  Projected after PYMEM_DOMAIN_MEM (Q2):")
        print(
            "    Conservative commitment: +3 to +8pp at p50 (under-promise / over-deliver)."
        )
        print(
            "    Stretch (heavy list/array/dict-of-primitives use): +10 to +20pp."
        )
        print(
            "    Validate on a canary with DD_PROFILING_MEM_DOMAIN_ENABLED=true."
        )

    else:
        # Multi-service: query all, then print ranked table.
        results: List[ServiceResult] = []
        for i, svc in enumerate(services, 1):
            print(f"  [{i}/{len(services)}] {svc}", end="", flush=True)
            r: ServiceResult = _query_single_service(
                session, site, svc, args.env, start, now, heap_field=heap_field
            )
            if r.ratio_pct is not None:
                status = (
                    f"RSS={r.rss_p50_mb:.0f}MB  heap={r.heap_avg_mb:.0f}MB  "
                    f"ratio_p50={r.ratio_pct:.1f}%  windows={len(r.ratios)}"
                )
            elif r.rss_p50_mb is not None:
                status = f"RSS={r.rss_p50_mb:.0f}MB  (no matched profiling data)"
            else:
                status = "(no runtime metrics)"
            print(f"  {status}")
            results.append(r)

        label: str = f"env={args.env}, last {window_label}"
        _print_multi_table(results, label)


# ---------------------------------------------------------------------------
# Argument parsing & entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="mode", required=True)

    # -- local ----------------------------------------------------------------
    p_local = sub.add_parser(
        "local",
        help="Analyse pprof files captured via DD_PROFILING_OUTPUT_PPROF",
    )
    p_local.add_argument(
        "prefix",
        metavar="PREFIX_OR_FILE",
        help="DD_PROFILING_OUTPUT_PPROF prefix, or a single .pprof file",
    )
    p_local.add_argument(
        "--rss-mb",
        type=float,
        default=None,
        metavar="MB",
        help=(
            "Override RSS in MB for all files (e.g. from `ps` or Datadog). "
            "If omitted, the script loads <prefix>.rss.csv (written by wrap "
            "mode) and matches each profile's mtime to the closest RSS sample."
        ),
    )

    # -- wrap -----------------------------------------------------------------
    p_wrap = sub.add_parser(
        "wrap",
        help="Run a command, capture profiles and poll RSS automatically",
    )
    p_wrap.add_argument(
        "cmd",
        nargs=argparse.REMAINDER,
        help="Command to run (after --), e.g.: -- ddtrace-run python workload.py",
    )

    # -- fleet ----------------------------------------------------------------
    p_fleet = sub.add_parser(
        "fleet",
        help="Query Datadog metrics API for fleet-wide RSS (needs DD_API_KEY + DD_APP_KEY)",
    )
    svc_group = p_fleet.add_mutually_exclusive_group(required=True)
    svc_group.add_argument(
        "--service",
        metavar="SVC",
        help="Single Datadog service tag (backward-compat alias for --services SVC)",
    )
    svc_group.add_argument(
        "--services",
        nargs="+",
        metavar="SVC",
        help=(
            "One or more Datadog service tags. "
            "Pass 'all' to auto-discover every Python service from the metrics API."
        ),
    )
    p_fleet.add_argument("--env", default="prod", help="Datadog env tag (default: prod)")

    time_group = p_fleet.add_mutually_exclusive_group()
    time_group.add_argument(
        "--period",
        metavar="PERIOD",
        help=(
            f"Lookback window as a human-friendly string. "
            f"Presets: {', '.join(_PERIOD_PRESETS)}. "
            "Or any Nh/Nd value, e.g. 6h, 2d. "
            "Max practical: 30d (profiles retention). "
            "Mutually exclusive with --hours."
        ),
    )
    time_group.add_argument(
        "--hours",
        type=int,
        default=24,
        metavar="N",
        help="Lookback window in hours (default: 24). Mutually exclusive with --period.",
    )
    p_fleet.add_argument(
        "--top",
        type=int,
        default=0,
        metavar="N",
        help=(
            "When using --services all, cap results to the top N services by RSS "
            "(default: 0 = no cap, query all discovered services)."
        ),
    )
    p_fleet.add_argument(
        "--env-file",
        metavar="PATH",
        default=None,
        help="Path to a .env file (default: .env in repo root, then ~/.env)",
    )
    p_fleet.add_argument(
        "--heap-field",
        choices=[_HEAP_PROFILE_FIELD_INUSE, _HEAP_PROFILE_FIELD_LIFETIME],
        default=_HEAP_PROFILE_FIELD_DEFAULT,
        help=(
            f"Profile heap field to compare against RSS. Default: "
            f"{_HEAP_PROFILE_FIELD_LIFETIME} (live heap snapshot despite the "
            f"'lifetime' name; validated to match Profile Explorer 'Heap Live "
            f"Size'). {_HEAP_PROFILE_FIELD_INUSE} does not exist in the "
            "Datadog backend as of 2026-05-08."
        ),
    )
    p_fleet.add_argument(
        "--validate-fields",
        action="store_true",
        help=(
            "Before running, print median values for both inuse and lifetime "
            "heap fields side-by-side so you can verify which one matches the "
            "Profile Explorer 'Heap Live Size' figure."
        ),
    )

    args = parser.parse_args()

    if args.mode == "local":
        run_local(args)
    elif args.mode == "wrap":
        if not args.cmd or args.cmd[0] == "--":
            args.cmd = args.cmd[1:] if args.cmd else []
        if not args.cmd:
            p_wrap.error("Provide a command after --")
        run_wrap(args)
    elif args.mode == "fleet":
        run_fleet(args)


if __name__ == "__main__":
    main()
