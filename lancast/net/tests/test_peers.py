"""Peer admission and CIDR parsing tests."""

from __future__ import annotations

import ipaddress

import pytest

from lancast.net import is_allowed_peer, parse_cidrs


@pytest.mark.parametrize(
    "ip",
    [
        "127.0.0.1",
        "10.0.0.0",
        "10.255.255.255",
        "172.16.0.0",
        "172.31.255.255",
        "192.168.0.0",
        "192.168.255.255",
        "169.254.1.1",
        "::1",
        "fd00::1",
        "fe80::1%3",
        "::ffff:192.168.1.2",
    ],
)
def test_allowed_peers(ip: str) -> None:
    assert is_allowed_peer(ip) is True


@pytest.mark.parametrize(
    "ip",
    [
        "172.15.255.255",
        "172.32.0.0",
        "192.169.0.1",
        "100.64.0.1",
        "100.127.255.255",
        "8.8.8.8",
        "2001:db8::1",
        "::ffff:8.8.8.8",
        "::ffff:100.64.0.1",
    ],
)
def test_denied_peers(ip: str) -> None:
    assert is_allowed_peer(ip) is False


@pytest.mark.parametrize(
    "ip",
    ["", "   ", "not-an-ip", "999.1.1.1", "192.168.1.1/24", None, 12345],
)
def test_invalid_peer_is_false(ip) -> None:
    assert is_allowed_peer(ip) is False


def test_cgnat_allowed_only_via_extra_cidr() -> None:
    assert is_allowed_peer("100.64.0.1") is False
    assert is_allowed_peer("100.64.0.1", extra_cidrs="100.64.0.0/10") is True
    assert is_allowed_peer("100.127.255.255", extra_cidrs=["100.64.0.0/10"]) is True
    assert is_allowed_peer("100.128.0.1", extra_cidrs="100.64.0.0/10") is False


def test_extra_cidr_accepts_network_object() -> None:
    extra = ipaddress.ip_network("203.0.113.0/24")
    assert is_allowed_peer("203.0.113.7", extra_cidrs=extra) is True
    assert is_allowed_peer("203.0.114.7", extra_cidrs=extra) is False


def test_extra_cidr_does_not_open_public_internet() -> None:
    assert is_allowed_peer("8.8.8.8", extra_cidrs="10.0.0.0/8") is False


def test_parse_cidrs_none() -> None:
    assert parse_cidrs(None) == []


def test_parse_cidrs_comma_separated_string() -> None:
    networks = parse_cidrs("10.0.0.0/8, 192.168.1.0/24,, ")
    assert networks == [
        ipaddress.ip_network("10.0.0.0/8"),
        ipaddress.ip_network("192.168.1.0/24"),
    ]


def test_parse_cidrs_list_and_host_bits() -> None:
    networks = parse_cidrs(["10.1.2.3/8", "2001:db8::1/32"])
    assert networks == [
        ipaddress.ip_network("10.0.0.0/8"),
        ipaddress.ip_network("2001:db8::/32"),
    ]
    assert isinstance(networks[0], ipaddress.IPv4Network)
    assert isinstance(networks[1], ipaddress.IPv6Network)


def test_parse_cidrs_single_network_object() -> None:
    network = ipaddress.ip_network("172.16.0.0/12")
    assert parse_cidrs(network) == [network]


def test_parse_cidrs_invalid_string_mentions_value() -> None:
    with pytest.raises(ValueError) as excinfo:
        parse_cidrs("10.0.0.0/33")
    assert "10.0.0.0/33" in str(excinfo.value)


def test_parse_cidrs_invalid_list_entry_mentions_value() -> None:
    with pytest.raises(ValueError) as excinfo:
        parse_cidrs(["10.0.0.0/8", "bogus"])
    assert "bogus" in str(excinfo.value)
