"""QR rendering tests (no PIL required)."""

from __future__ import annotations

import base64
import xml.etree.ElementTree as ET

from lancast.net import qr_svg, qr_svg_data_uri, qr_terminal

_TEXT = "https://192.168.1.20:8443/#pair=test-token-0123456789"


def _children(root: ET.Element, name: str) -> list[ET.Element]:
    return [element for element in root.iter() if element.tag.endswith(name)]


def test_qr_svg_is_parseable_document() -> None:
    svg = qr_svg(_TEXT)
    root = ET.fromstring(svg)
    assert root.tag.endswith("svg")
    assert root.tag.startswith("{http://www.w3.org/2000/svg}")

    view_box = root.get("viewBox")
    assert view_box is not None
    parts = view_box.split()
    assert len(parts) == 4
    assert parts[0] == parts[1] == "0"
    assert int(parts[2]) == int(parts[3])
    assert int(parts[2]) > 21  # QR payload plus the 4-module quiet zone


def test_qr_svg_has_white_background_and_single_dark_path() -> None:
    root = ET.fromstring(qr_svg(_TEXT))

    rects = _children(root, "rect")
    assert len(rects) == 1
    assert rects[0].get("width") == rects[0].get("height")
    assert rects[0].get("fill", "").lower() in {"#fff", "#ffffff", "white"}

    paths = _children(root, "path")
    assert len(paths) == 1
    path = paths[0]
    assert path.get("fill", "").lower() in {"#000", "#000000", "black"}
    assert path.get("shape-rendering") == "crispEdges"
    assert path.get("d")
    assert "base64" not in path.get("d", "")


def test_qr_svg_quiet_zone_matches_viewbox() -> None:
    root = ET.fromstring(qr_svg(_TEXT))
    view_size = int(root.get("viewBox").split()[2])
    rect = _children(root, "rect")[0]
    assert int(rect.get("width")) == view_size
    assert root.get("shape-rendering") == "crispEdges"


def test_qr_svg_data_uri_round_trips() -> None:
    uri = qr_svg_data_uri(_TEXT)
    assert uri.startswith("data:image/svg+xml;base64,")
    payload = uri.split(",", 1)[1]
    svg = base64.b64decode(payload).decode("utf-8")
    root = ET.fromstring(svg)
    assert root.tag.endswith("svg")
    assert root.get("viewBox")


def test_qr_terminal_shape_and_alphabet() -> None:
    rendered = qr_terminal(_TEXT)
    lines = rendered.splitlines()
    assert len(lines) > 10
    assert len({len(line) for line in lines}) == 1

    allowed = set("▀▄█ \n")
    assert set(rendered) <= allowed

    # The top and bottom 4-module quiet zones are light and therefore filled.
    assert set(lines[0]) == {"█"}
    assert set(lines[-1]) == {"█"}


def test_qr_terminal_differs_for_different_text() -> None:
    assert qr_terminal(_TEXT) != qr_terminal(_TEXT + "-other")
