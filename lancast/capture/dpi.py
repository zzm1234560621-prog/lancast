"""Per-monitor DPI awareness for the capture process.

Windows lies about screen geometry to DPI-unaware processes (a 2880x1800
display at 200% is reported as 1440x900).  Capturing and cursor coordinates
must use physical pixels, so the process has to opt into per-monitor v2
awareness before any display, DXGI or MSS object is created.

:func:`ensure_dpi_awareness` is idempotent and is called when
``lancast.capture`` is imported.  On non-Windows it is a no-op.
"""

from __future__ import annotations

import ctypes
import logging
import os
import threading

logger = logging.getLogger("lancast.capture")

# DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 is ((DPI_AWARENESS_CONTEXT)-4).
_DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = ctypes.c_void_p(-4)
_PROCESS_PER_MONITOR_DPI_AWARE = 2

_lock = threading.Lock()
_done = False


def _try_context_v2() -> bool:
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        func = getattr(user32, "SetProcessDpiAwarenessContext", None)
        if func is None:  # before Windows 10 1703
            return False
        func.argtypes = [ctypes.c_void_p]
        func.restype = ctypes.c_bool
        return bool(
            func(_DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2)
        )
    except Exception:  # pragma: no cover - depends on Windows version
        logger.debug("SetProcessDpiAwarenessContext(-4) failed.", exc_info=True)
        return False


def _try_shcore() -> bool:
    try:
        shcore = ctypes.WinDLL("shcore", use_last_error=True)
        func = shcore.SetProcessDpiAwareness
        func.argtypes = [ctypes.c_int]
        func.restype = ctypes.c_long
        hresult = int(func(_PROCESS_PER_MONITOR_DPI_AWARE))
        # S_OK, or S_FALSE when awareness was already set to the same value.
        return hresult in (0, 1)
    except Exception:  # pragma: no cover - depends on Windows version
        logger.debug("SetProcessDpiAwareness(2) failed.", exc_info=True)
        return False


def _try_legacy() -> bool:
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        func = user32.SetProcessDPIAware
        func.argtypes = []
        func.restype = ctypes.c_bool
        return bool(func())
    except Exception:  # pragma: no cover - depends on Windows version
        logger.debug("SetProcessDPIAware() failed.", exc_info=True)
        return False


def ensure_dpi_awareness() -> None:
    """Make this process per-monitor DPI aware (idempotent).

    Tries, in order: ``SetProcessDpiAwarenessContext(-4)`` (per-monitor v2),
    ``shcore.SetProcessDpiAwareness(2)`` and finally
    ``SetProcessDPIAware()``.  Existing awareness settings win; failures are
    only logged because a process manifest may already have set the mode.
    """

    global _done
    if _done:
        return
    with _lock:
        if _done:
            return
        if os.name != "nt":
            _done = True
            return

        method = None
        if _try_context_v2():
            method = "SetProcessDpiAwarenessContext(PER_MONITOR_AWARE_V2)"
        elif _try_shcore():
            method = "SetProcessDpiAwareness(PER_MONITOR_AWARE)"
        elif _try_legacy():
            method = "SetProcessDPIAware()"

        if method:
            logger.debug("DPI awareness enabled via %s.", method)
        else:
            logger.warning(
                "Could not enable DPI awareness; screen and cursor geometry "
                "may be scaled by Windows."
            )
        _done = True
