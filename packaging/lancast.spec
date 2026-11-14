# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for LanCast on Windows. Use packaging\\build.ps1, or from the repo root:

    .venv\\Scripts\\python.exe -m PyInstaller packaging\\lancast.spec ^
        --noconfirm --workpath packaging\\build --distpath packaging\\dist

Folder build by default (dist\\LanCast\\LanCast.exe); set LANCAST_ONEFILE=1 for a
single dist\\LanCast.exe. The browser assets are bundled under
``lancast/web/{viewer,admin}`` so that ``lancast.server.WEB_ROOT``
(``Path(__file__).parent / "web"``) resolves inside the bundle.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

# --------------------------------------------------------------------------- paths
# ``SPECPATH`` is the directory that contains this spec file (packaging/).
PACKAGING_DIR = Path(SPECPATH).resolve()
REPO_ROOT = PACKAGING_DIR.parent

# collect_submodules() imports the package to discover its path, so make the
# repository importable before anything is collected.
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


# --------------------------------------------------------------------------- assets
WEB_EXCLUDED = {"selftest.mjs", "mock_server.py"}


def _web_assets(subdir: str) -> list[tuple[str, str]]:
    """Files of one flat web directory, excluding dev/test-only helpers."""
    source_dir = REPO_ROOT / "lancast" / "web" / subdir
    destination = f"lancast/web/{subdir}"
    assets = []
    for path in sorted(source_dir.iterdir()):
        if path.is_file() and path.name not in WEB_EXCLUDED and not path.name.endswith((".pyc", ".pyo")):
            assets.append((str(path), destination))
    return assets


datas = _web_assets("viewer") + _web_assets("admin")


# --------------------------------------------------------------------------- hidden imports
def _not_tests_or_bench(name: str) -> bool:
    parts = name.split(".")
    return "tests" not in parts and parts[-1] != "bench"


hiddenimports: list[str] = []

# Every runtime-selected backend under these packages, minus tests/benchmarks.
for package in ("lancast.capture", "lancast.encode", "lancast.net", "lancast.input"):
    hiddenimports += collect_submodules(package, filter=_not_tests_or_bench)

# comtypes generates comtypes.gen at runtime (see rthook_comtypes_gen.py).
hiddenimports += collect_submodules(
    "comtypes", filter=lambda name: not name.startswith("comtypes.test")
)
hiddenimports += [
    # dxcam imports its numpy helper through importlib; the module graph cannot
    # see that, and the WinRT backend is selected through a lazy factory.
    "dxcam",
    "dxcam.processor._numpy_kernels",
    "dxcam.core.winrt_duplicator",
    # Top-level third-party packages used by LanCast.  PyInstaller's bundled
    # hooks take care of their native libraries and dynamic submodules.
    "av",
    "cv2",
    "numpy",
    "aiohttp",
    "cryptography",
    "qrcode",
    "psutil",
    "mss",
]

hiddenimports = sorted(set(hiddenimports))


# --------------------------------------------------------------------------- analysis
excludes = [
    # GUI/toolkit and scientific stacks that LanCast never imports.
    "tkinter",
    "_tkinter",
    "matplotlib",
    "PIL",
    "Pillow",
    "scipy",
    "pandas",
    "IPython",
    "jupyter",
    "notebook",
    "PyQt5",
    "PyQt6",
    "PySide2",
    "PySide6",
    "wx",
    # Development-only tooling.
    "pytest",
    "setuptools",
    "pip",
]


a = Analysis(
    [str(PACKAGING_DIR / "entry.py")],
    pathex=[str(REPO_ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[str(PACKAGING_DIR / "rthook_comtypes_gen.py")],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

# Default: a folder build (dist/LanCast/LanCast.exe + _internal/) that starts at
# once. LANCAST_ONEFILE=1 (build.ps1 -OneFile) makes a single dist/LanCast.exe,
# which is easier to copy but unpacks ~100 MB to %TEMP% on every start.
ONEFILE = os.environ.get("LANCAST_ONEFILE") == "1"

EXE_OPTIONS = dict(
    name="LanCast",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

if ONEFILE:
    exe = EXE(pyz, a.scripts, a.binaries, a.datas, [], upx_exclude=[], runtime_tmpdir=None, **EXE_OPTIONS)
else:
    exe = EXE(pyz, a.scripts, [], exclude_binaries=True, **EXE_OPTIONS)
    coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, upx_exclude=[], name="LanCast")
