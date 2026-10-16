"""Atomic file replacement that tolerates transient Windows file locks.

``os.replace`` (``MoveFileEx`` with ``REPLACE_EXISTING``) fails with
``ERROR_ACCESS_DENIED`` (WinError 5) while another process holds a short-lived
handle on the freshly written source or the destination. On Windows this is
common for files under an indexed or watched directory: Windows Search Indexer,
cloud-sync clients (OneDrive) and IDE workspace watchers open a new file right
after it is written, so an immediately following rename can lose the race.

The lock is transient, so a bounded retry is enough. A genuine permission
problem (read-only destination, wrong ACL) still raises after the attempts are
exhausted, so real errors are never hidden.
"""
from __future__ import annotations

import os
import time

# A short, bounded retry. The observed window is a few milliseconds; 20 attempts
# spaced 10 ms cover ~200 ms without making a real permission error hang.
_DEFAULT_ATTEMPTS = 20
_DEFAULT_DELAY_SECONDS = 0.01

# Windows sharing/access errors that are transient when another process is
# scanning or watching the file: ERROR_ACCESS_DENIED, ERROR_SHARING_VIOLATION,
# ERROR_LOCK_VIOLATION.
_RETRYABLE_WINERRORS = frozenset({5, 32, 33})


def _is_transient_lock(exc: OSError) -> bool:
    if isinstance(exc, PermissionError):
        return True
    return getattr(exc, "winerror", None) in _RETRYABLE_WINERRORS


def replace_with_retry(source, destination, *, attempts: int = _DEFAULT_ATTEMPTS,
                       delay: float = _DEFAULT_DELAY_SECONDS) -> None:
    """``os.replace(source, destination)`` retried past transient locks.

    Accepts :class:`os.PathLike` or ``str`` paths, matching ``os.replace``.
    """
    for attempt in range(1, attempts + 1):
        try:
            os.replace(source, destination)
            return
        except OSError as exc:
            if attempt >= attempts or not _is_transient_lock(exc):
                raise
            time.sleep(delay)
