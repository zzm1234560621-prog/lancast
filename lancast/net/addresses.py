"""IPv4 interface discovery and classification."""

from __future__ import annotations

import ipaddress
import json
import os
import socket
import subprocess
import threading
from dataclasses import dataclass

import psutil

_IPV4_LOOPBACK = ipaddress.ip_network("127.0.0.0/8")
_IPV4_LINK_LOCAL = ipaddress.ip_network("169.254.0.0/16")
_IPV4_CGNAT = ipaddress.ip_network("100.64.0.0/10")
_IPV4_VBOX_HOST_ONLY = ipaddress.ip_network("192.168.56.0/24")

# Matched case-insensitively against "<interface name> <adapter description>".
_VPN_MARKERS = (
    "tailscale",
    "zerotier",
    "wireguard",
    "openvpn",
    "tap-windows",
    "wintun",
    "tun",
    "warp",
)
_VIRTUAL_MARKERS = (
    "vmware",
    "virtualbox",
    "vethernet",
    "hyper-v",
    "wsl",
    "docker",
    "vmnet",
    "npcap",
    "loopback",
    "bluetooth",
    "microsoft wi-fi direct",
)

# Lower rank sorts first.  A LAN address that carries the default route ranks 0.
_KIND_RANK = {"lan": 1, "vpn": 2, "virtual": 3, "loopback": 4}

_ADAPTER_SCRIPT = (
    "[Console]::OutputEncoding=[Text.Encoding]::UTF8; "
    "Get-NetAdapter -IncludeHidden | "
    "Select-Object Name,InterfaceDescription,Virtual | ConvertTo-Json"
)
_ADAPTER_TIMEOUT = 4.0
_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# The adapter-description lookup costs a process launch, so it is cached for
# the lifetime of the process (including a failed lookup, which yields {}).
_adapter_cache: dict[str, str] | None = None
_adapter_lock = threading.Lock()


@dataclass(frozen=True)
class LanAddress:
    """One IPv4 address of an up network interface."""

    ip: str
    interface: str
    kind: str  # "lan" | "virtual" | "vpn" | "loopback"
    is_default_route: bool


def _adapter_descriptions() -> dict[str, str]:
    """Return ``{interface name: description}`` from PowerShell, cached.

    Any failure (non-Windows, PowerShell missing, timeout, access denied,
    unparsable JSON) degrades to an empty mapping so callers can classify by
    interface name alone.
    """

    global _adapter_cache
    with _adapter_lock:
        if _adapter_cache is not None:
            return _adapter_cache

        descriptions: dict[str, str] = {}
        if os.name == "nt":
            try:
                completed = subprocess.run(
                    [
                        "powershell",
                        "-NoProfile",
                        "-NonInteractive",
                        "-Command",
                        _ADAPTER_SCRIPT,
                    ],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=_ADAPTER_TIMEOUT,
                    creationflags=_CREATE_NO_WINDOW,
                    check=False,
                )
                if completed.returncode == 0:
                    raw = (completed.stdout or "").lstrip("\ufeff").strip()
                    if raw:
                        data = json.loads(raw)
                        if isinstance(data, dict):
                            data = [data]
                        if isinstance(data, list):
                            for item in data:
                                if not isinstance(item, dict):
                                    continue
                                name = item.get("Name")
                                if name is None:
                                    continue
                                description = item.get("InterfaceDescription")
                                descriptions[str(name)] = (
                                    "" if description is None else str(description)
                                )
            except (OSError, subprocess.SubprocessError, ValueError, TypeError):
                descriptions = {}

        _adapter_cache = descriptions
        return descriptions


def _default_route_ip() -> str | None:
    """Local IPv4 address the OS would use for an external destination.

    ``connect()`` on a UDP socket only selects a route; it sends no packet.
    """

    sock = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect(("8.8.8.8", 80))
        return str(sock.getsockname()[0])
    except OSError:
        return None
    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass


def _classify(address: ipaddress.IPv4Address, interface: str, description: str) -> str:
    if address in _IPV4_LOOPBACK:
        return "loopback"

    haystack = f"{interface} {description}".casefold()
    if any(marker in haystack for marker in _VPN_MARKERS):
        return "vpn"
    if address in _IPV4_CGNAT:
        return "vpn"

    if any(marker in haystack for marker in _VIRTUAL_MARKERS):
        return "virtual"
    if address in _IPV4_VBOX_HOST_ONLY:
        return "virtual"

    return "lan"


def _sort_key(address: LanAddress) -> tuple[int, ipaddress.IPv4Address]:
    rank = _KIND_RANK[address.kind]
    if address.kind == "lan" and address.is_default_route:
        rank = 0
    return rank, ipaddress.IPv4Address(address.ip)


def lan_addresses() -> list[LanAddress]:
    """All usable IPv4 addresses, best LAN candidate first."""

    try:
        interface_addresses = psutil.net_if_addrs()
        interface_stats = psutil.net_if_stats()
    except Exception:
        return []

    descriptions = {
        name.casefold(): description
        for name, description in _adapter_descriptions().items()
    }
    default_ip = _default_route_ip()

    addresses: list[LanAddress] = []
    for interface, entries in interface_addresses.items():
        stats = interface_stats.get(interface)
        if stats is None or not stats.isup:
            continue

        description = descriptions.get(interface.casefold(), "")
        for entry in entries:
            if entry.family != socket.AF_INET:
                continue
            try:
                parsed = ipaddress.ip_address(entry.address)
            except ValueError:
                continue
            if not isinstance(parsed, ipaddress.IPv4Address):
                continue
            if parsed in _IPV4_LINK_LOCAL:
                continue

            addresses.append(
                LanAddress(
                    ip=str(parsed),
                    interface=interface,
                    kind=_classify(parsed, interface, description),
                    is_default_route=default_ip is not None
                    and str(parsed) == default_ip,
                )
            )

    addresses.sort(key=_sort_key)
    return addresses


def primary_address() -> str | None:
    """Best IP to put in a pairing QR code, or ``None`` if nothing is usable."""

    addresses = lan_addresses()
    for address in addresses:
        if address.kind == "lan":
            return address.ip
    for address in addresses:
        if address.kind != "loopback":
            return address.ip
    return None
