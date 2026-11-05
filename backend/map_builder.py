"""Admin map builder: convert a shared-border planar graph into territory SVG paths.

The builder UI stores each border once (a straight segment between two joints);
neighbouring territories share it. This module validates the graph, traces its
faces, reattaches territory labels by anchor point, and emits one closed <path>
per labelled territory — matching the shape of the map SVGs in frontend/public
(``svg_borders`` layer, ``id`` + ``inkscape:label`` attributes, absolute path data).
"""

from __future__ import annotations

import math
import re
from typing import Any

MAX_DIM = 50_000.0
MIN_DIM = 100.0
MAX_JOINTS = 12_000
MAX_BORDERS = 24_000
MAX_LABELS = 2_000
MAX_WARNINGS = 50
MAX_NAME_LEN = 120

_KEY_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.\-]{0,79}$")
_TID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,63}$")

# Matches the invisible-outline style used by the existing map SVGs' territory paths.
PATH_STYLE = (
    "baseline-shift:baseline;display:inline;overflow:visible;opacity:1;"
    "fill:none;stroke:none;stroke-width:1.5;stroke-dasharray:none;"
    "stroke-opacity:1;enable-background:accumulate;stop-color:#000000;stop-opacity:1"
)

# Faces smaller than this (in px^2) are treated as degenerate and skipped.
_AREA_EPS = 1e-6


class MapBuilderError(ValueError):
    """Invalid builder payload (routes convert to HTTP 400)."""


def _as_float(value: Any, what: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise MapBuilderError(f"{what} must be a number")
    v = float(value)
    if not math.isfinite(v):
        raise MapBuilderError(f"{what} must be finite")
    return v


def _check_key(key: Any, what: str) -> str:
    if not isinstance(key, str) or not _KEY_RE.match(key):
        raise MapBuilderError(f"invalid {what} id: {key!r}")
    return key


def _parse(payload: Any) -> tuple[float, float, dict, dict, dict]:
    if not isinstance(payload, dict):
        raise MapBuilderError("payload must be a JSON object")

    width = _as_float(payload.get("width"), "width")
    height = _as_float(payload.get("height"), "height")
    if not (MIN_DIM <= width <= MAX_DIM):
        raise MapBuilderError(f"width must be between {MIN_DIM:g} and {MAX_DIM:g}")
    if not (MIN_DIM <= height <= MAX_DIM):
        raise MapBuilderError(f"height must be between {MIN_DIM:g} and {MAX_DIM:g}")

    joints_raw = payload.get("joints")
    if not isinstance(joints_raw, dict):
        raise MapBuilderError("joints must be an object keyed by joint id")
    if len(joints_raw) > MAX_JOINTS:
        raise MapBuilderError(f"too many joints (max {MAX_JOINTS})")
    joints: dict[str, tuple[float, float]] = {}
    for key, raw in joints_raw.items():
        jid = _check_key(key, "joint")
        if not isinstance(raw, dict):
            raise MapBuilderError(f"joint {jid!r} must be an object with x and y")
        x = _as_float(raw.get("x"), f"joint {jid} x")
        y = _as_float(raw.get("y"), f"joint {jid} y")
        if not (0.0 <= x <= width and 0.0 <= y <= height):
            raise MapBuilderError(f"joint {jid!r} is outside the map bounds")
        joints[jid] = (x, y)

    borders_raw = payload.get("borders")
    if borders_raw is None:
        borders_raw = {}
    if not isinstance(borders_raw, dict):
        raise MapBuilderError("borders must be an object keyed by border id")
    if len(borders_raw) > MAX_BORDERS:
        raise MapBuilderError(f"too many borders (max {MAX_BORDERS})")
    borders: dict[str, tuple[str, str]] = {}
    for key, raw in borders_raw.items():
        bid = _check_key(key, "border")
        if not isinstance(raw, dict):
            raise MapBuilderError(f"border {bid!r} must be an object with a and b")
        a = raw.get("a")
        b = raw.get("b")
        if a not in joints:
            raise MapBuilderError(f"border {bid!r} references unknown joint {a!r}")
        if b not in joints:
            raise MapBuilderError(f"border {bid!r} references unknown joint {b!r}")
        if a == b:
            raise MapBuilderError(f"border {bid!r} connects a joint to itself")
        borders[bid] = (a, b)

    labels_raw = payload.get("labels")
    if labels_raw is None:
        labels_raw = {}
    if isinstance(labels_raw, list):
        as_dict: dict[str, Any] = {}
        for entry in labels_raw:
            if not isinstance(entry, dict) or "territory_id" not in entry:
                raise MapBuilderError("each label must be an object with territory_id")
            as_dict[str(entry["territory_id"])] = entry
        labels_raw = as_dict
    if not isinstance(labels_raw, dict):
        raise MapBuilderError("labels must be an object keyed by territory id")
    if len(labels_raw) > MAX_LABELS:
        raise MapBuilderError(f"too many labels (max {MAX_LABELS})")
    labels: dict[str, dict[str, Any]] = {}
    for key, raw in labels_raw.items():
        if not isinstance(key, str) or not _TID_RE.match(key):
            raise MapBuilderError(f"invalid territory id: {key!r}")
        if not isinstance(raw, dict):
            raise MapBuilderError(f"label {key!r} must be an object")
        name = raw.get("display_name", key)
        if not isinstance(name, str) or not name.strip():
            raise MapBuilderError(f"label {key!r} needs a display_name")
        if len(name) > MAX_NAME_LEN:
            raise MapBuilderError(f"label {key!r} display_name is too long")
        x = _as_float(raw.get("x"), f"label {key} x")
        y = _as_float(raw.get("y"), f"label {key} y")
        if not (0.0 <= x <= width and 0.0 <= y <= height):
            raise MapBuilderError(f"label {key!r} anchor is outside the map bounds")
        labels[key] = {"display_name": name.strip(), "x": x, "y": y}

    return width, height, joints, borders, labels


def _prune_dangling(joints: dict, borders: dict) -> list[str]:
    """Drop borders that are not part of any cycle (repeatedly, degree <= 1)."""
    removed: list[str] = []
    changed = True
    while changed:
        changed = False
        degree = {jid: 0 for jid in joints}
        for a, b in borders.values():
            degree[a] += 1
            degree[b] += 1
        for bid in list(borders):
            a, b = borders[bid]
            if degree[a] <= 1 or degree[b] <= 1:
                del borders[bid]
                removed.append(bid)
                changed = True
    return removed


def _trace_rings(joints: dict, borders: dict) -> list[tuple[float, list[str]]]:
    """Trace every half-edge cycle; return (signed_area, joint_ring) per cycle.

    Positive area = interior (CCW) ring; negative = outer-boundary component (CW).
    The next-half-edge rule keeps the face on the left: from (u->v), take the
    half-edge immediately clockwise after (v->u) around v (previous in CCW order).
    """
    outgoing: dict[str, list[tuple[float, str]]] = {jid: [] for jid in joints}
    for a, b in borders.values():
        ax, ay = joints[a]
        bx, by = joints[b]
        ang_ab = math.atan2(by - ay, bx - ax) % (2.0 * math.pi)
        ang_ba = math.atan2(ay - by, ax - bx) % (2.0 * math.pi)
        outgoing[a].append((ang_ab, b))
        outgoing[b].append((ang_ba, a))
    for lst in outgoing.values():
        lst.sort(key=lambda t: t[0])

    pos: dict[tuple[str, str], int] = {}
    for frm, lst in outgoing.items():
        for i, (_, to) in enumerate(lst):
            pos[(frm, to)] = i

    used: set[tuple[str, str]] = set()
    rings: list[tuple[float, list[str]]] = []
    for frm in sorted(outgoing):
        lst = outgoing[frm]
        for i in range(len(lst)):
            start = (frm, lst[i][1])
            if start in used:
                continue
            ring: list[str] = []
            cur = start
            while cur not in used:
                used.add(cur)
                u, v = cur
                ring.append(u)
                vl = outgoing[v]
                j = pos[(v, u)]
                cur = (v, vl[(j - 1) % len(vl)][1])
            if len(ring) < 3:
                continue
            rings.append((_signed_area(ring, joints), ring))
    return rings


def _signed_area(ring: list[str], joints: dict) -> float:
    total = 0.0
    n = len(ring)
    for i in range(n):
        x1, y1 = joints[ring[i]]
        x2, y2 = joints[ring[(i + 1) % n]]
        total += x1 * y2 - x2 * y1
    return total / 2.0


def _point_in_ring(x: float, y: float, ring: list[str], joints: dict) -> bool:
    inside = False
    n = len(ring)
    j = n - 1
    for i in range(n):
        xi, yi = joints[ring[i]]
        xj, yj = joints[ring[j]]
        if (yi > y) != (yj > y):
            if x < (xj - xi) * (y - yi) / (yj - yi) + xi:
                inside = not inside
        j = i
    return inside


def _find_ring(x: float, y: float, interior: list[tuple[float, list[str]]], joints: dict) -> int | None:
    """Innermost interior ring containing the point (handles nested islands/lakes)."""
    best: int | None = None
    best_area = 0.0
    for idx, (area, ring) in enumerate(interior):
        if area < _AREA_EPS:
            continue
        if best is not None and area >= best_area:
            continue
        if _point_in_ring(x, y, ring, joints):
            best = idx
            best_area = area
    return best


def _fmt_num(v: float) -> str:
    s = f"{v:.4f}".rstrip("0").rstrip(".")
    return "0" if s in ("", "-0") else s


def _ring_to_d(ring: list[str], joints: dict) -> str | None:
    pts: list[tuple[float, float]] = []
    for jid in ring:
        x, y = joints[jid]
        if pts and abs(pts[-1][0] - x) < 1e-9 and abs(pts[-1][1] - y) < 1e-9:
            continue
        pts.append((x, y))
    if len(pts) < 3:
        return None
    parts = [f"M {_fmt_num(pts[0][0])},{_fmt_num(pts[0][1])}"]
    for x, y in pts[1:]:
        parts.append(f"L {_fmt_num(x)},{_fmt_num(y)}")
    parts.append("Z")
    return " ".join(parts)


def _xml_escape(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def _build_svg(width: float, height: float, territories: list[dict[str, str]]) -> str:
    w = _fmt_num(width)
    h = _fmt_num(height)
    lines = [
        '<?xml version="1.0" encoding="UTF-8" standalone="no"?>',
        f'<svg version="1.1" id="svg_map_builder" width="{w}" height="{h}" viewBox="0 0 {w} {h}"',
        '   xmlns:inkscape="http://www.inkscape.org/namespaces/inkscape"',
        '   xmlns:sodipodi="http://sodipodi.sourceforge.net/DTD/sodipodi-0.dtd"',
        '   xmlns="http://www.w3.org/2000/svg"',
        '   xmlns:svg="http://www.w3.org/2000/svg">',
        '  <defs id="defs1" />',
        '  <g inkscape:groupmode="layer" id="svg_borders_layer" inkscape:label="svg_borders" style="opacity:1">',
    ]
    for t in territories:
        lines.append(
            f'    <path style="{PATH_STYLE}" d="{t["d"]}" '
            f'id="{_xml_escape(t["id"])}" inkscape:label="{_xml_escape(t["label"])}" />'
        )
    lines += ["  </g>", "</svg>", ""]
    return "\n".join(lines)


def convert_map_builder(payload: Any) -> dict[str, Any]:
    """Validate a builder document and convert it to SVG territory paths.

    Returns ``{ok, svg, territories, stats, warnings}``. Raises ``MapBuilderError``
    on invalid input.
    """
    width, height, joints, borders, labels = _parse(payload)
    warnings: list[str] = []

    given_borders = len(borders)
    pruned = _prune_dangling(joints, borders)

    rings = _trace_rings(joints, borders)
    interior = [(area, ring) for area, ring in rings if area > _AREA_EPS]
    interior.sort(key=lambda t: (-t[0], t[1]))  # deterministic: largest first

    # Reattach labels to the innermost interior ring containing their anchor.
    ring_owner: dict[int, str] = {}
    assigned: dict[str, int] = {}
    for tid in sorted(labels):
        lab = labels[tid]
        idx = _find_ring(lab["x"], lab["y"], interior, joints)
        if idx is None:
            warnings.append(f"label {tid!r} is not inside any closed region")
            continue
        if idx in ring_owner:
            warnings.append(
                f"label {tid!r} falls in the same region already labelled {ring_owner[idx]!r}"
            )
            continue
        ring_owner[idx] = tid
        assigned[tid] = idx

    unlabelled = len(interior) - len(ring_owner)
    if unlabelled > 0:
        warnings.append(f"{unlabelled} closed region(s) have no label")

    territories: list[dict[str, str]] = []
    for tid in sorted(assigned):
        idx = assigned[tid]
        d = _ring_to_d(interior[idx][1], joints)
        if d is None:
            warnings.append(f"label {tid!r} region is degenerate and was skipped")
            continue
        territories.append({"id": tid, "label": labels[tid]["display_name"], "d": d})

    svg = _build_svg(width, height, territories)
    return {
        "ok": True,
        "svg": svg,
        "territories": territories,
        "stats": {
            "joints": len(joints),
            "borders": given_borders,
            "pruned_borders": len(pruned),
            "regions": len(interior),
            "labelled": len(territories),
        },
        "warnings": warnings[:MAX_WARNINGS],
    }
