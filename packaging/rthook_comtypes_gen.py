"""PyInstaller runtime hook: give comtypes a writable code-generation cache.

``comtypes`` creates ``comtypes.gen`` at runtime and writes generated type-library
wrappers into it.  Inside a one-file PyInstaller executable the package lives in
the temporary ``_MEIPASS`` directory; that directory is removed when the process
exits, so generated modules would be recreated on every launch (and a read-only
install location would make ``comtypes.client`` fail outright).  Pre-registering
the package with a per-user cache directory keeps generation working and
persistent.
"""

from __future__ import annotations

import os
import sys
import tempfile
import types
from pathlib import Path


def _cache_dir() -> str:
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
    candidates = []
    if base:
        candidates.append(Path(base) / "LanCast" / "comtypes_gen")
    candidates.append(Path(tempfile.gettempdir()) / "LanCast" / "comtypes_gen")
    for path in candidates:
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError:
            continue
        if os.access(path, os.W_OK):
            return str(path)
    # Last resort: a fresh temporary directory (always writable).
    return tempfile.mkdtemp(prefix="lancast-comtypes-")


if "comtypes.gen" not in sys.modules:
    module = types.ModuleType("comtypes.gen")
    module.__path__ = [_cache_dir()]
    module.__file__ = None
    sys.modules["comtypes.gen"] = module
