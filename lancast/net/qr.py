"""QR code rendering (SVG and a PIL-free terminal renderer)."""

from __future__ import annotations

import base64

import qrcode

_QUIET_ZONE = 4
_ERROR_CORRECTION = qrcode.constants.ERROR_CORRECT_M


def _matrix(text: str, border: int) -> list[list[bool]]:
    code = qrcode.QRCode(
        error_correction=_ERROR_CORRECTION,
        border=border,
        box_size=1,
    )
    code.add_data(text)
    code.make(fit=True)
    return code.get_matrix()


def qr_svg(text: str) -> str:
    """Return a standalone SVG document with one path for the dark modules."""

    modules = _matrix(text, border=0)
    module_count = len(modules)
    size = module_count + 2 * _QUIET_ZONE

    commands: list[str] = []
    for y, row in enumerate(modules):
        x = 0
        while x < module_count:
            if row[x]:
                start = x
                while x < module_count and row[x]:
                    x += 1
                run = x - start
                commands.append(
                    f"M{start + _QUIET_ZONE} {y + _QUIET_ZONE}h{run}v1h-{run}z"
                )
            else:
                x += 1

    # "width"/"height" only give the standalone document a sensible intrinsic
    # size; the viewBox itself stays in module units.
    pixel_size = size * 8
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {size} {size}" '
        f'width="{pixel_size}" height="{pixel_size}" shape-rendering="crispEdges">'
        f'<rect width="{size}" height="{size}" fill="#ffffff"/>'
        f'<path fill="#000000" shape-rendering="crispEdges" d="{"".join(commands)}"/>'
        "</svg>"
    )


def qr_svg_data_uri(text: str) -> str:
    """Return the SVG as a ``data:image/svg+xml;base64,`` URI."""

    encoded = base64.b64encode(qr_svg(text).encode("utf-8")).decode("ascii")
    return "data:image/svg+xml;base64," + encoded


def qr_terminal(text: str) -> str:
    """Render a QR code with Unicode half blocks for a dark console.

    Light modules become white cells (``█``/``▀``/``▄``) and dark modules
    become spaces, so the code keeps the correct polarity on a black terminal.
    Two module rows are packed into one text line; the 4-module quiet zone is
    part of the matrix.
    """

    modules = _matrix(text, border=_QUIET_ZONE)
    width = len(modules)
    lines: list[str] = []

    for y in range(0, width, 2):
        top = modules[y]
        bottom = modules[y + 1] if y + 1 < width else None
        cells: list[str] = []
        for x in range(width):
            top_light = not top[x]
            # An odd trailing row is padded with light cells so the bottom
            # quiet zone stays fully white.
            bottom_light = True if bottom is None else not bottom[x]
            if top_light and bottom_light:
                cells.append("█")
            elif top_light:
                cells.append("▀")
            elif bottom_light:
                cells.append("▄")
            else:
                cells.append(" ")
        lines.append("".join(cells))

    return "\n".join(lines)
