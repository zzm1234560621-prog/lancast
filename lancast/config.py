"""Runtime settings for LanCast."""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field
from pathlib import Path


def default_data_dir() -> Path:
    """Per-user, non-roaming data directory (certificates, paired devices)."""
    base = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), ".local", "share")
    return Path(base) / "LanCast"


# Settings the owner may change at runtime from the admin page: name -> (type, min, max).
LIVE_SETTINGS: dict[str, tuple[type, int, int]] = {
    "fps": (int, 1, 120),
    "max_width": (int, 320, 7680),
    "max_height": (int, 240, 4320),
    "bitrate_kbps": (int, 500, 100_000),
    "jpeg_quality": (int, 20, 95),
    "jpeg_fps": (int, 1, 60),
    "approval": (bool, 0, 1),
    "control_enabled": (bool, 0, 1),
    "viewer_quality": (bool, 0, 1),
}


@dataclass
class Settings:
    port: int = 8443                 # LAN listener (HTTPS + WSS)
    admin_port: int = 8442           # loopback listener (HTTP, admin page)
    bind: str = "0.0.0.0"
    fps: int = 60
    max_width: int = 1920
    max_height: int = 1080
    bitrate_kbps: int = 10_000
    jpeg_quality: int = 70
    jpeg_fps: int = 15
    encoder: str = "auto"            # auto | h264_qsv | libx264 | ...
    capture: str = "auto"            # auto | dxgi | mss | synthetic
    approval: bool = True            # new devices need the owner's click
    control_enabled: bool = True     # master switch for remote control (per-device grants still required)
    viewer_quality: bool = True      # viewers may choose their own quality preset
    fake_input: bool = False         # record input events instead of injecting them (tests / demos)
    max_viewers: int = 8
    allow_cidrs: list[str] = field(default_factory=list)
    pin_ttl_s: int = 600
    data_dir: Path = field(default_factory=default_data_dir)
    open_admin: bool = True

    def public_dict(self) -> dict:
        """The subset shown in / editable from the admin page."""
        d = asdict(self)
        return {k: d[k] for k in (*LIVE_SETTINGS.keys(), "max_viewers")}

    def apply_live(self, patch: dict) -> dict:
        """Validate and apply a partial update from the admin API. Returns applied keys.

        Raises ValueError with a human-readable message on any invalid key or value;
        nothing is applied in that case.
        """
        if not isinstance(patch, dict):
            raise ValueError("settings must be a JSON object")
        clean: dict = {}
        for key, value in patch.items():
            spec = LIVE_SETTINGS.get(key)
            if spec is None:
                raise ValueError(f"unknown or read-only setting: {key}")
            typ, lo, hi = spec
            if typ is bool:
                if not isinstance(value, bool):
                    raise ValueError(f"{key} must be true or false")
                clean[key] = value
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{key} must be a number")
            if isinstance(value, float) and not value.is_integer():
                raise ValueError(f"{key} must be an integer")
            ivalue = int(value)
            if not lo <= ivalue <= hi:
                raise ValueError(f"{key} must be between {lo} and {hi}")
            clean[key] = ivalue
        for key, value in clean.items():
            setattr(self, key, value)
        return clean
