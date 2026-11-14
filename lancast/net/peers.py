"""Peer admission checks and CIDR parsing."""

from __future__ import annotations

import ipaddress

_Network = ipaddress.IPv4Network | ipaddress.IPv6Network

# Mirrors PROTOCOL.md §1: loopback, RFC 1918, link-local, IPv6 ULA/link-local.
_ALLOWED_NETWORKS: tuple[_Network, ...] = tuple(
    ipaddress.ip_network(cidr)
    for cidr in (
        "127.0.0.0/8",
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "169.254.0.0/16",
        "::1/128",
        "fc00::/7",
        "fe80::/10",
    )
)


def parse_cidrs(values) -> list[_Network]:
    """Parse ``None``, a comma-separated string, or a list of CIDR strings.

    Host bits are accepted and masked (``strict=False``).  Raises ``ValueError``
    mentioning the offending original value.
    """

    if values is None:
        return []
    if isinstance(values, (ipaddress.IPv4Network, ipaddress.IPv6Network)):
        return [values]
    if isinstance(values, str):
        items: list = values.split(",")
    else:
        try:
            items = list(values)
        except TypeError:
            raise ValueError(f"无效的 CIDR 值: {values!r}") from None

    networks: list[_Network] = []
    for item in items:
        if isinstance(item, (ipaddress.IPv4Network, ipaddress.IPv6Network)):
            networks.append(item)
            continue
        if not isinstance(item, str):
            raise ValueError(f"无效的 CIDR 值: {item!r}")
        text = item.strip()
        if not text:
            continue
        try:
            networks.append(ipaddress.ip_network(text, strict=False))
        except ValueError as exc:
            raise ValueError(f"无效的 CIDR 值: {item!r}") from exc
    return networks


def _parse_peer(ip: object) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """Parse a peer address, handling IPv6 zones and IPv4-mapped IPv6."""

    if not isinstance(ip, str):
        return None
    candidate = ip.strip()
    if not candidate:
        return None
    # A scoped IPv6 literal such as "fe80::1%12" is still the link-local
    # address for admission purposes; the zone id is interface-local.
    if "%" in candidate and ":" in candidate:
        candidate = candidate.split("%", 1)[0]

    try:
        address = ipaddress.ip_address(candidate)
    except ValueError:
        return None

    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return address


def is_allowed_peer(ip: str, extra_cidrs=()) -> bool:
    """Whether a remote peer may connect to the LAN listener.

    ``extra_cidrs`` accepts the same forms as :func:`parse_cidrs`, plus a
    single :class:`ipaddress.IPv4Network`/:class:`ipaddress.IPv6Network`.
    Invalid peer addresses return ``False``.
    """

    address = _parse_peer(ip)
    if address is None:
        return False

    for network in _ALLOWED_NETWORKS:
        if address.version == network.version and address in network:
            return True

    for network in parse_cidrs(extra_cidrs):
        if address.version == network.version and address in network:
            return True
    return False
