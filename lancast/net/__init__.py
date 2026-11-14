"""LAN address discovery, peer allow-listing, QR rendering and Windows diagnostics.

The public surface is documented in ``docs/ARCHITECTURE.md`` (section
``lancast.net``).  Everything here is read-only with respect to the operating
system: no firewall rule is ever created, and no network packet is sent.
"""

from __future__ import annotations

from .addresses import LanAddress, lan_addresses, primary_address
from .peers import is_allowed_peer, parse_cidrs
from .qr import qr_svg, qr_svg_data_uri, qr_terminal
from .windows import windows_network_report

__all__ = [
    "LanAddress",
    "lan_addresses",
    "primary_address",
    "is_allowed_peer",
    "parse_cidrs",
    "qr_svg",
    "qr_svg_data_uri",
    "qr_terminal",
    "windows_network_report",
]
