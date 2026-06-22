#!/usr/bin/env python3
"""
Infinite-loop workload that keeps ~100 MB live in each CPython allocator domain.

No profiling awareness — run it under ddtrace-run to collect profiles:

  DD_SERVICE=mem-domain-test DD_PROFILING_ENABLED=true \\
  DD_PROFILING_MEM_DOMAIN_ENABLED=true \\
  .venv/bin/ddtrace-run python scripts/mem_domain_workload.py

Ctrl-C to stop.
"""

import array
import time

TARGET_BYTES: int = 100 * 1024 * 1024  # 100 MB per domain
SLEEP_SECONDS: int = 60  # matches profiler default upload interval

# Module-level pools keep allocations live at every profiler upload window.
_pool_obj: list = []
_pool_mem: list = []
_pool_raw: list = []


def _alloc_obj(target_bytes: int) -> int:
    """PYMEM_DOMAIN_OBJ: small bytes objects via PyObject_Malloc."""
    chunk: int = 64
    count: int = target_bytes // chunk
    for _ in range(count):
        _pool_obj.append(bytes(chunk))
    return count * chunk


def _alloc_mem(target_bytes: int) -> int:
    """PYMEM_DOMAIN_MEM: array.array buffers + list ob_item via PyMem_Calloc.

    Uses [None]*N for the list (single PyMem_Calloc for ob_item, no per-element
    OBJ allocation) to keep this domain pure.
    """
    item_count: int = 65536  # 64 KB per array
    n_arrays: int = (target_bytes // 2) // item_count
    for _ in range(n_arrays):
        _pool_mem.append(array.array("B", bytes(item_count)))
    n_ptrs: int = (target_bytes // 2) // 8
    _pool_mem.append([None] * n_ptrs)
    return n_arrays * item_count + n_ptrs * 8


def _alloc_raw(target_bytes: int) -> int:
    """PYMEM_DOMAIN_RAW: large bytearray chunks via malloc."""
    chunk_size: int = 1024 * 1024
    count: int = target_bytes // chunk_size
    for _ in range(count):
        _pool_raw.append(bytearray(chunk_size))
    return count * chunk_size


def main() -> None:
    print("Allocating ~100 MB per CPython domain...")

    obj_bytes = _alloc_obj(TARGET_BYTES)
    print(f"  OBJ  (PyObject_Malloc): {obj_bytes / 1024 / 1024:.1f} MB"
          f" — {len(_pool_obj)} × 64-byte bytes objects")

    mem_bytes = _alloc_mem(TARGET_BYTES)
    n_arrays = sum(1 for x in _pool_mem if isinstance(x, array.array))
    print(f"  MEM  (PyMem_Calloc):    {mem_bytes / 1024 / 1024:.1f} MB"
          f" — {n_arrays} × 64 KB array.array + {TARGET_BYTES // 2 // 8:,}-element list")

    raw_bytes = _alloc_raw(TARGET_BYTES)
    print(f"  RAW  (malloc):          {raw_bytes / 1024 / 1024:.1f} MB"
          f" — {len(_pool_raw)} × 1 MB bytearray chunks")

    total_mb = (obj_bytes + mem_bytes + raw_bytes) / 1024 / 1024
    print(f"\nTotal live: {total_mb:.0f} MB  |  upload window: {SLEEP_SECONDS}s  |  Ctrl-C to stop\n")

    cycle: int = 0
    while True:
        cycle += 1
        print(f"[cycle {cycle}] sleeping {SLEEP_SECONDS}s...")
        time.sleep(SLEEP_SECONDS)


if __name__ == "__main__":
    main()
