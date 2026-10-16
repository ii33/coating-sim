"""Allocation-free advisory resource estimates for disposable workers.

Estimates are advisory project conventions, not hardware guarantees or grid caps.
Available RAM is sampled at use time; other processes and device drivers can race
that observation. Process isolation reduces failure scope, not the OS OOM risk.
"""
from __future__ import annotations

import ctypes
import os
from pathlib import Path


def available_memory_bytes():
    """Best available host/container physical-memory headroom, or unknown."""
    candidates = []
    if os.name == 'nt':
        class MemoryStatus(ctypes.Structure):
            _fields_ = [('length', ctypes.c_ulong), ('load', ctypes.c_ulong)] + [
                (name, ctypes.c_ulonglong) for name in ('total', 'available', 'page_total',
                    'page_available', 'virtual_total', 'virtual_available', 'extended')]
        try:
            value = MemoryStatus(); value.length = ctypes.sizeof(value)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(value)):
                candidates.append(int(value.available))
        except (AttributeError, OSError, ValueError):
            pass
    else:
        try:
            rows = dict(line.split(':', 1) for line in Path('/proc/meminfo').read_text().splitlines())
            candidates.append(int(rows['MemAvailable'].split()[0]) * 1024)
        except (OSError, KeyError, ValueError):
            pass
        # cgroup v2 and v1: host MemAvailable may greatly exceed container RAM.
        for limit_path, used_path in (
            ('/sys/fs/cgroup/memory.max', '/sys/fs/cgroup/memory.current'),
            ('/sys/fs/cgroup/memory/memory.limit_in_bytes', '/sys/fs/cgroup/memory/memory.usage_in_bytes'),
        ):
            try:
                limit = int(Path(limit_path).read_text().strip())
                used = int(Path(used_path).read_text().strip())
                candidates.append(max(0, limit - used))
            except (OSError, ValueError):
                pass
    return min(candidates) if candidates else None


def format_bytes(value):
    if value is None:
        return '未知'
    for suffix, divisor in (('TiB', 1024**4), ('GiB', 1024**3), ('MiB', 1024**2), ('KiB', 1024)):
        if value >= divisor:
            whole, fraction = divmod(value, divisor)
            return f'{whole}.{fraction * 10 // divisor} {suffix}'
    return f'{value} B'


def estimate_memory(settings, *, layer_count=1, available_bytes=None):
    """Conservative RAM working-set estimate without creating a grid.

    Project convention: 64 MiB runtime + n²*(256+16*layers)+16*n bytes.
    Counts coordinate/geometry, backend scratch, retained fields, IPC and views;
    device memory and backend libraries can require more. It never refuses work.
    """
    n = settings.grid_size
    if type(n) is not int or n < 1:
        raise ValueError('网格边长必须为正整数')
    layers = max(1, int(layer_count))
    peak = 64 * 1024**2 + n * n * (256 + 16 * layers) + 16 * n
    available = available_memory_bytes() if available_bytes is None else available_bytes
    warning = (available is not None and peak > available // 2)
    message = f'预计峰值内存约 {format_bytes(peak)}'
    if available is not None:
        message += f'；当前可用约 {format_bytes(available)}'
    else:
        message += '；当前可用内存未知'
    message += '（粗估，实际可能更高）'
    if warning:
        message += '。内存风险较高，可能失败；可减小网格或关闭其他程序'
    return {'estimated_peak_memory_bytes': peak, 'available_memory_bytes': available,
            'memory_warning': warning, 'memory_estimate_label': message,
            'memory_estimate_basis': 'project_convention_grid_working_set_v1'}


def ensure_result_memory(byte_count, *, copies=4, available_bytes=None):
    """Compatibility no-op: RAM estimates must never reject a valid result.

    Actual allocation/transport failures are reported by the isolated worker or
    result reader. A sampled headroom value is not an allocation guarantee.
    """
    return None


def _array_bytes(value, seen=None):
    seen = set() if seen is None else seen
    if id(value) in seen:
        return 0
    seen.add(id(value))
    if hasattr(value, 'nbytes'):
        return int(value.nbytes)
    if isinstance(value, dict):
        return sum(_array_bytes(item, seen) for item in value.values())
    if isinstance(value, (tuple, list)):
        return sum(_array_bytes(item, seen) for item in value)
    if isinstance(value, (bytes, bytearray)):
        return len(value)
    return 0


def constrain_worker_memory():
    """Deprecated compatibility no-op; the product sets no memory hard cap.

    Keep the callable for older integrations, without setting Windows Job limits,
    Linux RLIMIT_AS, or rejecting unknown/zero measured memory headroom. Existing
    OS/container limits are outside the product's control and remain unchanged.
    """
    return False
