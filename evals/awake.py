"""Keep the machine awake during an eval run.

A run takes minutes. When a laptop slept part-way through one (2026-11-25), a conversation took
over ten hours of wall time, connections dropped, and the run had to be thrown away. On Windows
this asks the OS not to sleep until the run ends; elsewhere it does nothing (use ``caffeinate``
on macOS or ``systemd-inhibit`` on Linux).
"""

import sys
from collections.abc import Iterator
from contextlib import contextmanager

_ES_CONTINUOUS = 0x80000000
_ES_SYSTEM_REQUIRED = 0x00000001


@contextmanager
def keep_awake() -> Iterator[bool]:
    """Yields whether the request was made. The display may still turn off; the system won't
    sleep."""
    if sys.platform != "win32":
        yield False
        return
    import ctypes

    kernel32 = ctypes.windll.kernel32
    granted = bool(kernel32.SetThreadExecutionState(_ES_CONTINUOUS | _ES_SYSTEM_REQUIRED))
    try:
        yield granted
    finally:
        kernel32.SetThreadExecutionState(_ES_CONTINUOUS)
