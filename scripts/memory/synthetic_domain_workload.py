"""
Synthetic workload that deliberately allocates ~100 MB in each of the three
CPython allocator domains, then sleeps so ddtrace's memory profiler can collect
samples.

Domain mapping:
- _pool_obj  (PYMEM_DOMAIN_OBJ)  — PyObject_Malloc, ≤512-byte Python objects.
  Simulated by creating many small Python objects (bytes of size 64).
- _pool_mem  (PYMEM_DOMAIN_MEM)  — PyMem_Malloc, internal VM memory such as
  list/dict backing arrays and buffer objects.
  Simulated by building large lists (repeated .append triggers backing-array
  reallocation through the MEM domain) and array.array objects.
- _pool_raw  (PYMEM_DOMAIN_RAW)  — malloc, allocations >512 bytes or explicitly
  routed to the system allocator.
  Simulated with large bytearray chunks (each 1 MB).

Intended use: run under `measure_profiler_rss_coverage.py wrap` mode, which sets
DD_PROFILING_OUTPUT_PPROF and compares heap-space bytes in the resulting pprof
against process RSS.
"""

import array
import os
import resource
import time

TARGET_BYTES = 100 * 1024 * 1024

_pool_obj = []
_pool_mem = []
_pool_raw = []


def _allocate_obj(target_bytes):
    chunk_size = 64
    count = target_bytes // chunk_size
    for _ in range(count):
        _pool_obj.append(bytes(chunk_size))
    return count, count * chunk_size


def _allocate_mem(target_bytes):
    array_item_size = array.array("B").itemsize
    items_per_array = 65536
    bytes_per_array = items_per_array * array_item_size
    num_arrays = target_bytes // bytes_per_array
    for _ in range(num_arrays):
        a = array.array("B", bytes(items_per_array))
        _pool_mem.append(a)
    large_list = []
    remaining = target_bytes - num_arrays * bytes_per_array
    list_items = remaining // 8
    for i in range(list_items):
        large_list.append(i)
    _pool_mem.append(large_list)
    total_bytes = num_arrays * bytes_per_array + list_items * 8
    return len(_pool_mem), total_bytes


def _allocate_raw(target_bytes):
    chunk_size = 1024 * 1024
    count = target_bytes // chunk_size
    for _ in range(count):
        _pool_raw.append(bytearray(chunk_size))
    return count, count * chunk_size


def main():
    print("Allocating PYMEM_DOMAIN_OBJ pool (~100 MB, small Python objects)...")
    obj_count, obj_bytes = _allocate_obj(TARGET_BYTES)
    print(f"  _pool_obj: {obj_count} objects, ~{obj_bytes / 1024 / 1024:.1f} MB")

    print("Allocating PYMEM_DOMAIN_MEM pool (~100 MB, array.array + large list)...")
    mem_count, mem_bytes = _allocate_mem(TARGET_BYTES)
    print(f"  _pool_mem: {mem_count} items, ~{mem_bytes / 1024 / 1024:.1f} MB")

    print("Allocating PYMEM_DOMAIN_RAW pool (~100 MB, large bytearray chunks)...")
    raw_count, raw_bytes = _allocate_raw(TARGET_BYTES)
    print(f"  _pool_raw: {raw_count} chunks, ~{raw_bytes / 1024 / 1024:.1f} MB")

    # ru_maxrss is in kilobytes on Linux, bytes on macOS/BSD
    ru = resource.getrusage(resource.RUSAGE_SELF)
    rss_raw = ru.ru_maxrss
    rss_mb = rss_raw / 1024 if os.uname().sysname == "Linux" else rss_raw / (1024 * 1024)
    print(f"\nCurrent RSS (max): {rss_mb:.1f} MB")
    print(f"Total estimated allocated: {(obj_bytes + mem_bytes + raw_bytes) / 1024 / 1024:.1f} MB")

    print("\nSleeping 30 seconds for profiler collection...")
    time.sleep(30)
    print("Done.")


if __name__ == "__main__":
    main()
