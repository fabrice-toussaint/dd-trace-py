#!/usr/bin/env python3
"""Advanced memory-domain profiling benchmark.

Designed to produce a rich, readable flame graph comparison between
DD_PROFILING_MEM_DOMAIN_ENABLED=false and =true, covering:

  • Multiple data-structure shapes that exercise PYMEM_DOMAIN_MEM
  • Deep, named call stacks so individual contributors are visible in the UI
  • Long-lived objects (survive full 60-second profiling windows)
  • Realistic workload patterns: caches, event queues, time-series buffers

Memory domain quick reference
──────────────────────────────
  PYMEM_DOMAIN_OBJ  (always hooked):
    bytes, int, float, str, tuple, custom class __new__
  PYMEM_DOMAIN_MEM  (hooked when DD_PROFILING_MEM_DOMAIN_ENABLED=true):
    list ob_item array       – the C pointer array backing every list
    dict hash table          – ma_table when capacity > 8
    array.array data buffer  – the raw C array of typed elements
    set hash table           – equivalent to dict
    collections.deque blocks – each block is a PyMem_Malloc slab
  PYMEM_DOMAIN_RAW  (never hooked by ddtrace today):
    bytearray backing buffer – raw malloc in cpython/Objects/bytesobject.c

Usage (two terminals, one MEM off, one MEM on):

    # Terminal A – MEM off (baseline)
    DD_SERVICE=mem-domain-bench \\
    DD_PROFILING_ENABLED=true \\
    DD_PROFILING_MEM_DOMAIN_ENABLED=false \\
    DD_RUN_HANDLER=run_mem_off \\
    DD_TRACE_AGENT_URL=unix:///var/run/workspaces/datadog/apm.socket \\
    DD_DOGSTATSD_URL=unix:///var/run/workspaces/datadog-agent/statsd.sock \\
      .venv/bin/ddtrace-run .venv/bin/python \\
        scripts/memory/advanced_domain_workload.py

    # Terminal B – MEM on
    DD_SERVICE=mem-domain-bench \\
    DD_PROFILING_ENABLED=true \\
    DD_PROFILING_MEM_DOMAIN_ENABLED=true \\
    DD_RUN_HANDLER=run_mem_on \\
    DD_TRACE_AGENT_URL=unix:///var/run/workspaces/datadog/apm.socket \\
    DD_DOGSTATSD_URL=unix:///var/run/workspaces/datadog-agent/statsd.sock \\
      .venv/bin/ddtrace-run .venv/bin/python \\
        scripts/memory/advanced_domain_workload.py

    Then compare in Datadog:
      Profile type : Heap Live Size
      Query A      : service:mem-domain-bench run:run_mem_off
      Query B      : service:mem-domain-bench run:run_mem_on
"""

from __future__ import annotations

import array
import collections
import os
import time
from dataclasses import dataclass, field
from typing import Deque, Dict, List, Optional, Set

# ── Knobs ─────────────────────────────────────────────────────────────────────
HOLD_SECONDS = 90.0   # keep pool alive for at least one 60-s profile window
BURST_SLEEP  = 0.15   # seconds between allocation bursts
PRINT_EVERY  = 10     # print status every N cycles


# ─────────────────────────────────────────────────────────────────────────────
# Data-structure definitions (interesting class hierarchy in the flame graph)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class MetricPoint:
    """Single time-series data point.  Exercises OBJ domain (float/int objects)."""
    timestamp: float
    value: float
    tags: tuple  # immutable → OBJ domain

    @staticmethod
    def make(ts: float, v: float, tag_count: int = 4) -> "MetricPoint":
        return MetricPoint(
            timestamp=ts,
            value=v,
            tags=tuple(f"tag{i}:val{i}" for i in range(tag_count)),
        )


@dataclass
class EventRecord:
    """Event with a payload dict and a list of labels.

    The dict hash table and list ob_item array are PYMEM_DOMAIN_MEM.
    The string/int values are PYMEM_DOMAIN_OBJ.
    """
    event_id: int
    attributes: Dict[str, str]   # dict hash table → MEM domain
    labels: List[str]            # list ob_item     → MEM domain
    payload: bytes               # bytes object     → OBJ domain

    @staticmethod
    def make(event_id: int, n_attrs: int = 16, payload_size: int = 256) -> "EventRecord":
        return EventRecord(
            event_id=event_id,
            attributes={f"key_{i}": f"value_{i}" for i in range(n_attrs)},
            labels=[f"label_{i}" for i in range(n_attrs // 2)],
            payload=bytes(payload_size),
        )


@dataclass
class TimeSeriesBuffer:
    """Fixed-capacity ring buffer backed by array.array (MEM domain buffer).

    array.array uses PyMem_Malloc for its C data buffer → visible in MEM domain.
    """
    capacity: int
    _data: array.array = field(init=False)
    _count: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        # 'd' = C double (8 bytes each); entire buffer allocated via PyMem_Malloc
        self._data = array.array("d", [0.0] * self.capacity)

    def append(self, v: float) -> None:
        self._data[self._count % self.capacity] = v
        self._count += 1

    @property
    def nbytes(self) -> int:
        return len(self._data) * self._data.itemsize


@dataclass
class SlidingWindowSet:
    """Tracks unique string keys in a set (set hash table → MEM domain)."""
    max_size: int
    _seen: Set[str] = field(default_factory=set)

    def add(self, key: str) -> None:
        if len(self._seen) >= self.max_size:
            # Evict oldest half — in practice we'd use an OrderedDict;
            # the rebuild exercises MEM domain (new set hash table allocation)
            self._seen = set(list(self._seen)[self.max_size // 2 :])
        self._seen.add(key)

    @property
    def size(self) -> int:
        return len(self._seen)


# ─────────────────────────────────────────────────────────────────────────────
# Allocation layers — deep call stacks for the flame graph
# ─────────────────────────────────────────────────────────────────────────────

class MetricsIngestionLayer:
    """Simulates a metrics ingestion pipeline.

    Stack: MetricsIngestionLayer.ingest
             └─ _parse_batch
                  └─ _build_timeseries
                       └─ TimeSeriesBuffer (array.array, MEM domain)
    """

    def __init__(self, n_series: int = 50, capacity: int = 2048) -> None:
        self.series: List[TimeSeriesBuffer] = [
            TimeSeriesBuffer(capacity) for _ in range(n_series)
        ]

    def ingest(self, batch_size: int = 200) -> int:
        return self._parse_batch(batch_size)

    def _parse_batch(self, batch_size: int) -> int:
        ts = time.time()
        total = 0
        for i, buf in enumerate(self.series):
            total += self._build_timeseries(buf, ts, batch_size, i)
        return total

    def _build_timeseries(
        self, buf: TimeSeriesBuffer, ts: float, n: int, series_id: int
    ) -> int:
        for j in range(n):
            buf.append(ts + j * 0.001 + series_id)
        return n

    @property
    def nbytes(self) -> int:
        return sum(b.nbytes for b in self.series)


class EventQueueLayer:
    """Simulates an event processing queue.

    Stack: EventQueueLayer.enqueue
             └─ _create_events
                  └─ EventRecord.make   (dict hash table + list ob_item → MEM)
    The deque itself uses PyMem_Malloc for its internal block slab.
    """

    def __init__(self, max_depth: int = 5000) -> None:
        self.queue: Deque[EventRecord] = collections.deque(maxlen=max_depth)
        self._counter = 0

    def enqueue(self, batch: int = 100) -> None:
        for rec in self._create_events(batch):
            self.queue.append(rec)

    def _create_events(self, batch: int) -> List[EventRecord]:
        events = []
        for _ in range(batch):
            self._counter += 1
            events.append(self._build_event(self._counter))
        return events

    def _build_event(self, event_id: int) -> EventRecord:
        return EventRecord.make(event_id, n_attrs=20, payload_size=128)

    @property
    def nbytes(self) -> int:
        n_events = len(self.queue)
        # rough: each event has ~20 dict entries × ~50 bytes avg + list + payload
        return n_events * (20 * 50 + 20 * 8 + 128)


class DeduplicationLayer:
    """Tracks seen event-IDs in a sliding window set.

    Stack: DeduplicationLayer.process
             └─ _check_and_register
                  └─ SlidingWindowSet.add  (set hash table → MEM domain)
    """

    def __init__(self, window: int = 10_000) -> None:
        self.seen = SlidingWindowSet(window)

    def process(self, event_ids: List[int]) -> int:
        return sum(self._check_and_register(eid) for eid in event_ids)

    def _check_and_register(self, event_id: int) -> int:
        key = f"evt:{event_id}"
        already_seen = key in self.seen._seen
        self.seen.add(key)
        return 0 if already_seen else 1


class LargeCacheLayer:
    """Simulates an LRU-style cache with list-of-MetricPoints per key.

    Each cache entry is a list → ob_item array is MEM domain.
    The MetricPoint values are OBJ domain.
    """

    def __init__(self, n_keys: int = 200, points_per_key: int = 500) -> None:
        self.cache: Dict[str, List[MetricPoint]] = {}
        self.n_keys = n_keys
        self.points_per_key = points_per_key

    def fill(self) -> None:
        ts = time.time()
        for k in range(self.n_keys):
            self._populate_key(f"metric_{k}", ts)

    def _populate_key(self, key: str, ts: float) -> None:
        self.cache[key] = self._build_series(ts, self.points_per_key)

    def _build_series(self, ts: float, n: int) -> List[MetricPoint]:
        return [MetricPoint.make(ts + i, float(i)) for i in range(n)]

    @property
    def nbytes(self) -> int:
        # rough: each MetricPoint ~200 bytes (3 fields + tuple overhead)
        return sum(len(v) for v in self.cache.values()) * 200


# ─────────────────────────────────────────────────────────────────────────────
# Orchestrator
# ─────────────────────────────────────────────────────────────────────────────

def _ensure_pprof_dir() -> None:
    pprof = os.environ.get("DD_PROFILING_OUTPUT_PPROF", "")
    if pprof:
        os.makedirs(os.path.dirname(pprof) or ".", exist_ok=True)


def _mb(n: int) -> str:
    return f"{n / 1024 / 1024:.0f} MB"


def main() -> None:
    _ensure_pprof_dir()

    run = os.environ.get("DD_RUN_HANDLER", "default")
    mem_on = os.environ.get("DD_PROFILING_MEM_DOMAIN_ENABLED", "false").lower() in (
        "1", "true", "yes"
    )
    print(f"Advanced domain workload  run={run}  mem_domain={'ON' if mem_on else 'OFF'}")
    print(f"Pool lifecycle: fill/accumulate for {HOLD_SECONDS:.0f}s → drain → repeat")
    print("Ctrl-C to stop\n")

    # Instantiate layers (all long-lived; survive across profiling windows)
    metrics    = MetricsIngestionLayer(n_series=80,  capacity=1024)
    events     = EventQueueLayer(max_depth=8000)
    dedup      = DeduplicationLayer(window=15_000)
    cache      = LargeCacheLayer(n_keys=300, points_per_key=300)

    # Pre-fill cache (exercises deep stack immediately)
    cache.fill()

    cycle       = 0
    pool_start  = time.monotonic()
    # Secondary OBJ-domain accumulator so heap-live-size grows visibly
    obj_accum: List[bytes] = []
    mem_accum: list = []

    while True:
        cycle += 1
        elapsed = time.monotonic() - pool_start

        # ── Drain phase ───────────────────────────────────────────────────────
        if elapsed >= HOLD_SECONDS:
            obj_accum.clear()
            mem_accum.clear()
            events.queue.clear()
            dedup.seen._seen.clear()
            cache.fill()           # rebuild cache after drain
            pool_start = time.monotonic()
            print(f"[cycle {cycle:4d}] ── POOL DRAINED, rebuilding ──")

        # ── Allocation bursts ─────────────────────────────────────────────────
        metrics.ingest(batch_size=100)                        # MEM: array buffers
        events.enqueue(batch=80)                              # MEM: dict + list
        dedup.process(list(range(cycle * 80, cycle * 80 + 80)))  # MEM: set table
        # OBJ+MEM accumulator — grows until drain
        obj_accum.extend(bytes(64) for _ in range(5_000))    # OBJ: bytes objects
        mem_accum.extend([None] * 512 for _ in range(20))    # MEM: list ob_item

        # ── Status ────────────────────────────────────────────────────────────
        if cycle % PRINT_EVERY == 1:
            obj_mb    = len(obj_accum) * 64 / 1024 / 1024
            mem_lists = len(mem_accum) * 512 * 8 / 1024 / 1024
            q_depth   = len(events.queue)
            dedup_sz  = dedup.seen.size
            print(
                f"[cycle {cycle:4d} | {elapsed:5.1f}s] "
                f"obj_accum={obj_mb:.0f}MB  "
                f"mem_lists={mem_lists:.0f}MB  "
                f"ts_buffers={_mb(metrics.nbytes)}  "
                f"cache={_mb(cache.nbytes)}  "
                f"queue_depth={q_depth}  "
                f"dedup_keys={dedup_sz}"
            )

        time.sleep(BURST_SLEEP)


if __name__ == "__main__":
    main()
