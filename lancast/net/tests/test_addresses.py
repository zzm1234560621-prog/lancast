"""LAN address discovery tests."""

from __future__ import annotations

import socket
from types import SimpleNamespace

import pytest

import lancast.net.addresses as addresses_module
from lancast.net import LanAddress, lan_addresses, primary_address


def _entry(ip: str) -> SimpleNamespace:
    return SimpleNamespace(
        family=socket.AF_INET,
        address=ip,
        netmask="255.255.255.0",
        broadcast=None,
        ptp=None,
    )


def _install_fake_network(monkeypatch, interfaces, descriptions=None, default=None):
    addresses = {
        name: [_entry(ip) for ip in ips] for name, ips in interfaces.items()
    }
    stats = {
        name: SimpleNamespace(isup=not name.startswith("Down"))
        for name in interfaces
    }
    monkeypatch.setattr(
        addresses_module.psutil, "net_if_addrs", lambda: addresses
    )
    monkeypatch.setattr(
        addresses_module.psutil, "net_if_stats", lambda: stats
    )
    monkeypatch.setattr(
        addresses_module,
        "_adapter_descriptions",
        lambda: descriptions or {},
    )
    monkeypatch.setattr(
        addresses_module, "_default_route_ip", lambda: default
    )


def test_classification_and_ordering(monkeypatch) -> None:
    _install_fake_network(
        monkeypatch,
        {
            "Wi-Fi": ["192.168.1.20"],
            "ZeroTier One": ["10.147.17.5"],
            "Tailscale": ["100.101.102.103"],
            "VMware Network Adapter VMnet8": ["192.168.88.1"],
            "vEthernet (WSL (Hyper-V firewall))": ["172.20.16.1"],
            "Ethernet 6": ["192.168.56.2"],
            "Ethernet 4": ["192.168.137.1"],
            "Ethernet 5": ["192.168.50.2"],
            "Loopback Pseudo-Interface 1": ["127.0.0.1"],
            "Local Area Connection* 8": ["169.254.66.153"],
            "Down": ["192.168.9.9"],
        },
        default="192.168.1.20",
    )

    result = lan_addresses()
    assert [address.ip for address in result] == [
        "192.168.1.20",     # lan + default route
        "192.168.50.2",     # lan
        "192.168.137.1",    # lan (hotspot), and virtual-by-IP is not applicable
        "10.147.17.5",   # vpn (ZeroTier name)
        "100.101.102.103",    # vpn (Tailscale name and CGNAT range)
        "172.20.16.1",      # virtual (vEthernet)
        "192.168.56.2",     # virtual (VirtualBox host-only range)
        "192.168.88.1",     # virtual (VMnet)
        "127.0.0.1",        # loopback comes last
    ]

    by_ip = {address.ip: address for address in result}
    assert by_ip["192.168.1.20"].kind == "lan"
    assert by_ip["192.168.1.20"].is_default_route is True
    assert by_ip["192.168.50.2"].is_default_route is False
    assert by_ip["10.147.17.5"].kind == "vpn"
    assert by_ip["100.101.102.103"].kind == "vpn"
    assert by_ip["172.20.16.1"].kind == "virtual"
    assert by_ip["192.168.56.2"].kind == "virtual"
    assert by_ip["192.168.88.1"].kind == "virtual"
    assert by_ip["127.0.0.1"].kind == "loopback"

    assert "169.254.66.153" not in by_ip
    assert "192.168.9.9" not in by_ip


def test_adapter_description_marks_vpn_and_virtual(monkeypatch) -> None:
    _install_fake_network(
        monkeypatch,
        {
            "Ethernet 7": ["192.168.66.9"],
            "Ethernet 8": ["192.168.77.9"],
        },
        descriptions={
            "Ethernet 7": "TAP-Windows Adapter V9",
            "Ethernet 8": "Hyper-V Virtual Ethernet Adapter",
        },
    )

    by_ip = {address.ip: address for address in lan_addresses()}
    assert by_ip["192.168.66.9"].kind == "vpn"
    assert by_ip["192.168.77.9"].kind == "virtual"


def test_primary_prefers_lan_then_non_loopback(monkeypatch) -> None:
    monkeypatch.setattr(
        addresses_module,
        "lan_addresses",
        lambda: [
            LanAddress("127.0.0.1", "lo", "loopback", False),
            LanAddress("100.101.102.103", "Tailscale", "vpn", False),
        ],
    )
    assert primary_address() == "100.101.102.103"

    monkeypatch.setattr(
        addresses_module,
        "lan_addresses",
        lambda: [
            LanAddress("100.101.102.103", "Tailscale", "vpn", False),
            LanAddress("192.168.1.20", "Wi-Fi", "lan", True),
        ],
    )
    assert primary_address() == "192.168.1.20"

    monkeypatch.setattr(
        addresses_module,
        "lan_addresses",
        lambda: [LanAddress("127.0.0.1", "lo", "loopback", False)],
    )
    assert primary_address() is None


def test_real_addresses_are_sane() -> None:
    result = lan_addresses()
    assert isinstance(result, list)
    for address in result:
        assert isinstance(address, LanAddress)
        assert address.kind in {"lan", "virtual", "vpn", "loopback"}
        assert len(address.ip.split(".")) == 4
        assert not address.ip.startswith("169.254.")

    ranks = []
    for address in result:
        if address.kind == "lan" and address.is_default_route:
            ranks.append(0)
        else:
            ranks.append({"lan": 1, "vpn": 2, "virtual": 3, "loopback": 4}[address.kind])
    assert ranks == sorted(ranks)


_DEFAULT_LAN = next(
    (address for address in lan_addresses() if address.kind == "lan" and address.is_default_route),
    None,
)


@pytest.mark.skipif(
    _DEFAULT_LAN is None,
    reason="this machine has no LAN address carrying the default route",
)
def test_default_route_lan_address_is_first_and_primary() -> None:
    result = lan_addresses()
    assert result[0].ip == _DEFAULT_LAN.ip
    assert result[0].kind == "lan"
    assert result[0].is_default_route is True
    assert primary_address() == _DEFAULT_LAN.ip
