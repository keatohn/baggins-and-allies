"""Admin map builder: reverse converter — closed-path territory SVG → shared-border graph.

The map SVGs in frontend/public store one closed ``<path>`` per territory, so every
shared border exists twice (once inside each neighbour) and editing one side means
editing the other in sync. This module imports such an SVG into the builder's
planar-graph document: it flattens path data (curves → polyline), applies transforms,
welds near-coincident neighbour borders into single shared edges, adds the canvas
frame when the map is full-bleed, and reattaches a label anchor per territory.

The result is the same document shape the builder draws and POST /admin/maps/builder
/convert consumes, so an imported map round-trips: import → edit → build → SVG.
"""

from __future__ import annotations

import math
import re
import xml.etree.ElementTree as ET
from typing import Any, Iterable

from backend.map_builder import (
    MAX_BORDERS,
    MAX_DIM,
    MAX_JOINTS,
    MAX_NAME_LEN,
    MIN_DIM,
    MapBuilderError,
    _TID_RE,
    convert_map_builder,
)

MAX_SVG_CHARS = 8_000_000
MAX_TERRITORIES = 2_000
# Weld passes: sub-pixel near-dup clusters converge by halving their spread each
# pass; 8 settles every bundled map (12+ changes nothing).
MAX_PASSES = 8

# Curve flattening chord tolerance (px) — below the maps' own visual detail.
FLATTEN_TOL = 0.4
# Ramer–Douglas–Peucker simplification applied before welding (px).
SIMPLIFY_EPS = 0.5
# Default weld tolerance (px): neighbour borders in existing maps sit within ~3px
# of each other; +0.5px per side covers simplification drift.
DEFAULT_SNAP = 4.5
MIN_SNAP = 0.0
MAX_SNAP = 50.0
# A vertex whose projection lands this close (px) to a segment end joins that end.
ENDPOINT_JOIN_PX = 0.5
# A vertex this close to a border line is "on" it: it must be shared as a joint
# by every overlapping border on that line, even if another move is chosen.
SHARED_POINT_EPS = 0.05
# Subpaths with |area| below this are treated as specks and skipped.
MIN_AREA = 0.5

_FRAME_TID = "__frame__"
_SKIP_TAGS = frozenset({"defs", "clipPath", "mask", "marker", "symbol", "pattern",
                        "linearGradient", "radialGradient", "filter", "metadata"})

_NUM_RE = re.compile(r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")
_CMD_RE = re.compile(r"([MmZzLlHhVvCcSsQqTtAa])")
_PATH_ID_RE = re.compile(r"^path\d+$", re.I)
_TRANSFORM_RE = re.compile(r"([a-zA-Z]+)\s*\(([^)]*)\)")
_LENGTH_RE = re.compile(r"^([+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)(px|pt|pc|mm|cm|in)?$")

# Unit sizes in px for absolute CSS units (96dpi).
_UNIT_PX = {"px": 1.0, "pt": 96.0 / 72.0, "pc": 16.0, "in": 96.0, "cm": 96.0 / 2.54, "mm": 96.0 / 25.4}


# --- path data parsing ---------------------------------------------------------

def _tokenize_d(d: str) -> Iterable[tuple[str, list[float]]]:
    parts = _CMD_RE.split(d)
    i = 0
    while i < len(parts):
        chunk = parts[i]
        i += 1
        if not chunk:
            continue
        if len(chunk) == 1 and chunk.isalpha():
            argstr = parts[i] if i < len(parts) else ""
            i += 1
            yield chunk, [float(x) for x in _NUM_RE.findall(argstr)]
        else:
            nums = [float(x) for x in _NUM_RE.findall(chunk)]
            if nums:
                yield "", nums  # stray numbers: caller attaches to previous command


def _chord_deviation(p0, c1, c2, p3) -> float:
    dx, dy = p3[0] - p0[0], p3[1] - p0[1]
    denom = math.hypot(dx, dy)
    if denom < 1e-12:
        return math.hypot(c1[0] - p0[0], c1[1] - p0[1]) + math.hypot(c2[0] - p0[0], c2[1] - p0[1])
    return max(
        abs((c1[0] - p0[0]) * dy - (c1[1] - p0[1]) * dx) / denom,
        abs((c2[0] - p0[0]) * dy - (c2[1] - p0[1]) * dx) / denom,
    )


def _flatten_cubic(p0, c1, c2, p3, out: list, tol: float, depth: int = 0) -> None:
    if depth >= 12 or _chord_deviation(p0, c1, c2, p3) <= tol:
        out.append(p3)
        return
    # de Casteljau split at t=0.5
    m01 = ((p0[0] + c1[0]) / 2, (p0[1] + c1[1]) / 2)
    m12 = ((c1[0] + c2[0]) / 2, (c1[1] + c2[1]) / 2)
    m23 = ((c2[0] + p3[0]) / 2, (c2[1] + p3[1]) / 2)
    m012 = ((m01[0] + m12[0]) / 2, (m01[1] + m12[1]) / 2)
    m123 = ((m12[0] + m23[0]) / 2, (m12[1] + m23[1]) / 2)
    mid = ((m012[0] + m123[0]) / 2, (m012[1] + m123[1]) / 2)
    _flatten_cubic(p0, m01, m012, mid, out, tol, depth + 1)
    _flatten_cubic(mid, m123, m23, p3, out, tol, depth + 1)


def _flatten_quad(p0, c1, p2, out: list, tol: float, depth: int = 0) -> None:
    dx, dy = p2[0] - p0[0], p2[1] - p0[1]
    denom = math.hypot(dx, dy)
    dev = (
        math.hypot(c1[0] - p0[0], c1[1] - p0[1])
        if denom < 1e-12
        else abs((c1[0] - p0[0]) * dy - (c1[1] - p0[1]) * dx) / denom
    )
    if depth >= 10 or dev <= tol:
        out.append(p2)
        return
    m01 = ((p0[0] + c1[0]) / 2, (p0[1] + c1[1]) / 2)
    m12 = ((c1[0] + p2[0]) / 2, (c1[1] + p2[1]) / 2)
    mid = ((m01[0] + m12[0]) / 2, (m01[1] + m12[1]) / 2)
    _flatten_quad(p0, m01, mid, out, tol, depth + 1)
    _flatten_quad(mid, m12, p2, out, tol, depth + 1)


def _flatten_arc(p0, rx, ry, phi_deg, large, sweep, p1, out: list, tol: float) -> None:
    if rx == 0.0 or ry == 0.0 or (abs(p0[0] - p1[0]) < 1e-12 and abs(p0[1] - p1[1]) < 1e-12):
        out.append(p1)
        return
    phi = math.radians(phi_deg)
    cosp, sinp = math.cos(phi), math.sin(phi)
    dx2, dy2 = (p0[0] - p1[0]) / 2, (p0[1] - p1[1]) / 2
    x1p = cosp * dx2 + sinp * dy2
    y1p = -sinp * dx2 + cosp * dy2
    rx, ry = abs(rx), abs(ry)
    lam = (x1p ** 2) / (rx ** 2) + (y1p ** 2) / (ry ** 2)
    if lam > 1.0:
        s = math.sqrt(lam)
        rx *= s
        ry *= s
    den = rx ** 2 * y1p ** 2 + ry ** 2 * x1p ** 2
    num = rx ** 2 * ry ** 2 - den
    co = math.sqrt(max(0.0, num / den)) if den > 0 else 0.0
    if large == sweep:
        co = -co
    cxp = co * rx * y1p / ry
    cyp = -co * ry * x1p / rx
    cx = cosp * cxp - sinp * cyp + (p0[0] + p1[0]) / 2
    cy = sinp * cxp + cosp * cyp + (p0[1] + p1[1]) / 2

    def ang(ux: float, uy: float, vx: float, vy: float) -> float:
        dot = ux * vx + uy * vy
        n = math.hypot(ux, uy) * math.hypot(vx, vy)
        a = math.acos(max(-1.0, min(1.0, dot / n))) if n else 0.0
        return -a if ux * vy - uy * vx < 0 else a

    th1 = ang(1.0, 0.0, (x1p - cxp) / rx, (y1p - cyp) / ry)
    dth = ang((x1p - cxp) / rx, (y1p - cyp) / ry, (-x1p - cxp) / rx, (-y1p - cyp) / ry)
    if not sweep and dth > 0:
        dth -= 2 * math.pi
    elif sweep and dth < 0:
        dth += 2 * math.pi
    rmax = max(rx, ry)
    step = 2 * math.acos(max(-1.0, min(1.0, 1.0 - tol / rmax))) if rmax > tol else math.pi / 4
    n = max(2, int(math.ceil(abs(dth) / max(step, 1e-3))))
    for k in range(1, n + 1):
        th = th1 + dth * k / n
        out.append(
            (
                cx + rx * math.cos(th) * cosp - ry * math.sin(th) * sinp,
                cy + rx * math.cos(th) * sinp + ry * math.sin(th) * cosp,
            )
        )
    out[-1] = p1


def _path_subpaths(d: str, tol: float) -> list[list[tuple[float, float]]]:
    """Parse path data into polylines. Closed subpaths repeat their start point last."""
    subpaths: list[list[tuple[float, float]]] = []
    cur: list[tuple[float, float]] = []
    pos = (0.0, 0.0)
    start = (0.0, 0.0)
    prev_cmd = ""
    prev_ctrl: tuple[float, float] | None = None

    def flush() -> None:
        nonlocal cur
        if len(cur) >= 2:
            subpaths.append(cur)
        cur = []

    for cmd, nums in _tokenize_d(d):
        if cmd == "":
            cmd = prev_cmd
            if not cmd:
                continue
        rel = cmd.islower()
        c = cmd.upper()
        i = 0
        if c == "M":
            first_pair = True
            while i + 1 < len(nums):
                x, y = nums[i], nums[i + 1]
                i += 2
                if rel:
                    x += pos[0]
                    y += pos[1]
                if first_pair:
                    flush()
                    pos = (x, y)
                    start = pos
                    cur = [pos]
                    first_pair = False
                else:
                    pos = (x, y)
                    cur.append(pos)
            prev_ctrl = None
        elif c == "Z":
            if cur:
                if abs(cur[0][0] - pos[0]) > 1e-9 or abs(cur[0][1] - pos[1]) > 1e-9:
                    cur.append(cur[0])
                flush()
                pos = start
            prev_ctrl = None
        elif c == "L":
            while i + 1 < len(nums):
                x, y = nums[i], nums[i + 1]
                i += 2
                if rel:
                    x += pos[0]
                    y += pos[1]
                pos = (x, y)
                cur.append(pos)
            prev_ctrl = None
        elif c == "H":
            while i < len(nums):
                x = nums[i] + (pos[0] if rel else 0.0)
                i += 1
                pos = (x, pos[1])
                cur.append(pos)
            prev_ctrl = None
        elif c == "V":
            while i < len(nums):
                y = nums[i] + (pos[1] if rel else 0.0)
                i += 1
                pos = (pos[0], y)
                cur.append(pos)
            prev_ctrl = None
        elif c == "C":
            while i + 5 < len(nums):
                x1, y1, x2, y2, x, y = nums[i : i + 6]
                i += 6
                if rel:
                    x1 += pos[0]
                    y1 += pos[1]
                    x2 += pos[0]
                    y2 += pos[1]
                    x += pos[0]
                    y += pos[1]
                _flatten_cubic(pos, (x1, y1), (x2, y2), (x, y), cur, tol)
                pos = (x, y)
                prev_ctrl = (x2, y2)
        elif c == "S":
            while i + 3 < len(nums):
                x2, y2, x, y = nums[i : i + 4]
                i += 4
                if rel:
                    x2 += pos[0]
                    y2 += pos[1]
                    x += pos[0]
                    y += pos[1]
                if prev_cmd and prev_cmd.upper() in ("C", "S") and prev_ctrl:
                    x1 = 2 * pos[0] - prev_ctrl[0]
                    y1 = 2 * pos[1] - prev_ctrl[1]
                else:
                    x1, y1 = pos
                _flatten_cubic(pos, (x1, y1), (x2, y2), (x, y), cur, tol)
                pos = (x, y)
                prev_ctrl = (x2, y2)
        elif c == "Q":
            while i + 3 < len(nums):
                x1, y1, x, y = nums[i : i + 4]
                i += 4
                if rel:
                    x1 += pos[0]
                    y1 += pos[1]
                    x += pos[0]
                    y += pos[1]
                _flatten_quad(pos, (x1, y1), (x, y), cur, tol)
                pos = (x, y)
                prev_ctrl = (x1, y1)
        elif c == "T":
            while i + 1 < len(nums):
                x, y = nums[i], nums[i + 1]
                i += 2
                if rel:
                    x += pos[0]
                    y += pos[1]
                if prev_cmd and prev_cmd.upper() in ("Q", "T") and prev_ctrl:
                    x1 = 2 * pos[0] - prev_ctrl[0]
                    y1 = 2 * pos[1] - prev_ctrl[1]
                else:
                    x1, y1 = pos
                _flatten_quad(pos, (x1, y1), (x, y), cur, tol)
                pos = (x, y)
                prev_ctrl = (x1, y1)
        elif c == "A":
            while i + 6 < len(nums):
                rx, ry, rot, laf, sf, x, y = nums[i : i + 7]
                i += 7
                if rel:
                    x += pos[0]
                    y += pos[1]
                _flatten_arc(pos, rx, ry, rot, int(laf) != 0, int(sf) != 0, (x, y), cur, tol)
                pos = (x, y)
            prev_ctrl = None
        prev_cmd = cmd
    flush()
    return subpaths


# --- transforms ----------------------------------------------------------------

Matrix = tuple[float, float, float, float, float, float]  # a b c d e f (SVG order)
_IDENTITY: Matrix = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


def _mat_mul(m: Matrix, n: Matrix) -> Matrix:
    a1, b1, c1, d1, e1, f1 = m
    a2, b2, c2, d2, e2, f2 = n
    return (
        a1 * a2 + c1 * b2,
        b1 * a2 + d1 * b2,
        a1 * c2 + c1 * d2,
        b1 * c2 + d1 * d2,
        a1 * e2 + c1 * f2 + e1,
        b1 * e2 + d1 * f2 + f1,
    )


def _apply_mat(m: Matrix, x: float, y: float) -> tuple[float, float]:
    a, b, c, d, e, f = m
    return (a * x + c * y + e, b * x + d * y + f)


def _parse_transform(text: str | None) -> Matrix | None:
    if not text or not text.strip():
        return None
    m: Matrix = _IDENTITY
    found = False
    for name, argstr in _TRANSFORM_RE.findall(text):
        args = [float(v) for v in _NUM_RE.findall(argstr)]
        name = name.lower()
        t: Matrix | None = None
        if name == "matrix" and len(args) >= 6:
            t = (args[0], args[1], args[2], args[3], args[4], args[5])
        elif name == "translate":
            t = (1.0, 0.0, 0.0, 1.0, args[0] if args else 0.0, args[1] if len(args) > 1 else 0.0)
        elif name == "scale":
            sx = args[0] if args else 1.0
            sy = args[1] if len(args) > 1 else sx
            t = (sx, 0.0, 0.0, sy, 0.0, 0.0)
        elif name == "rotate":
            ang = math.radians(args[0] if args else 0.0)
            cos_a, sin_a = math.cos(ang), math.sin(ang)
            r: Matrix = (cos_a, sin_a, -sin_a, cos_a, 0.0, 0.0)
            if len(args) >= 3:
                cx, cy = args[1], args[2]
                r = _mat_mul(_mat_mul((1.0, 0.0, 0.0, 1.0, cx, cy), r), (1.0, 0.0, 0.0, 1.0, -cx, -cy))
            t = r
        elif name == "skewx":
            t = (1.0, 0.0, math.tan(math.radians(args[0] if args else 0.0)), 1.0, 0.0, 0.0)
        elif name == "skewy":
            t = (1.0, math.tan(math.radians(args[0] if args else 0.0)), 0.0, 1.0, 0.0, 0.0)
        if t is not None:
            m = _mat_mul(m, t)
            found = True
    return m if found else None


# --- svg walking ---------------------------------------------------------------

def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _inkscape_label(el: ET.Element) -> str | None:
    for key, val in el.attrib.items():
        if key.endswith("}label") and "inkscape" in key:
            return val
    return None


def _parse_length(raw: str | None) -> float | None:
    if raw is None:
        return None
    m = _LENGTH_RE.match(raw.strip())
    if not m:
        return None
    value = float(m.group(1))
    unit = m.group(2)
    if unit and unit != "px":
        if unit not in _UNIT_PX:
            return None
        value *= _UNIT_PX[unit]
    return value


def _canvas_size(root: ET.Element) -> tuple[float, float, float, float]:
    """Return (width, height, viewbox_min_x, viewbox_min_y) in viewBox user units."""
    vb = root.get("viewBox") or root.get("viewbox")
    vb_vals: list[float] = []
    if vb:
        try:
            vb_vals = [float(v) for v in _NUM_RE.findall(vb)]
        except ValueError:
            vb_vals = []
    if len(vb_vals) >= 4 and vb_vals[2] > 0 and vb_vals[3] > 0:
        # The builder works in viewBox coordinates; width/height attrs may be scaled.
        return vb_vals[2], vb_vals[3], vb_vals[0], vb_vals[1]
    w = _parse_length(root.get("width"))
    h = _parse_length(root.get("height"))
    if w is None or h is None:
        raise MapBuilderError("SVG needs width/height or a viewBox to define the map size")
    return w, h, 0.0, 0.0


def _territory_id(el: ET.Element) -> tuple[str | None, str | None]:
    """Return (territory id, display name) using the same rules as the map viewer."""
    raw_id = (el.get("id") or "").strip()
    label = _inkscape_label(el)
    use_id = bool(raw_id) and not _PATH_ID_RE.match(raw_id)
    raw = raw_id if use_id else (label.strip() if label else None)
    if not raw:
        return None, None
    if use_id:
        tid = raw.lower()
    else:
        tid = raw.lower().replace("'", "").replace(" ", "_").replace("-", "_")
    tid = re.sub(r"[^a-z0-9_.]+", "_", tid).strip("_")
    tid = re.sub(r"^[^a-z0-9]+", "", tid)  # ids must start with a letter or digit
    if not tid or not _TID_RE.match(tid[:64]):
        return None, None
    name = (label or raw_id).strip() or tid
    return tid[:64], name[:MAX_NAME_LEN]


def _circle_points(cx: float, cy: float, r: float, segments: int = 48) -> list[tuple[float, float]]:
    pts = []
    for i in range(segments):
        a = 2.0 * math.pi * i / segments
        pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    pts.append(pts[0])
    return pts


def _element_transform(el: ET.Element) -> Matrix | None:
    return _parse_transform(el.get("transform"))


def _inherit(parent: Matrix | None, el: ET.Element) -> Matrix | None:
    local = _element_transform(el)
    if local is None:
        return parent
    return _mat_mul(parent or _IDENTITY, local) if parent is not None else local


def _collect_polygons(
    root: ET.Element,
    min_x: float,
    min_y: float,
    warnings: list[str],
) -> list[dict[str, Any]]:
    # Prefer the svg_borders layer (matches every map in frontend/public).
    borders_layer: ET.Element | None = None
    layer_tf: Matrix | None = None
    for el in root.iter():
        if _local(el.tag) == "g" and _inkscape_label(el) == "svg_borders":
            borders_layer = el
            # Accumulate transforms from root down to (excluding) the layer.
            parent_map = {child: parent for parent in root.iter() for child in parent}
            path: list[ET.Element] = []
            node: ET.Element | None = el
            while node is not None:
                path.append(node)
                node = parent_map.get(node)
            for anc in reversed(path[:-1]):
                layer_tf = _inherit(layer_tf, anc)
            break

    territories: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    skipped_open = 0
    skipped_speck = 0
    skipped_unnamed = 0
    multi_subpath = 0
    holes_dropped = 0

    def consider(el: ET.Element, inherited: Matrix | None) -> None:
        nonlocal skipped_open, skipped_speck, skipped_unnamed, multi_subpath, holes_dropped
        tid, name = _territory_id(el)
        if tid is None:
            skipped_unnamed += 1
            return
        if tid.startswith("hint_"):
            return
        tag = _local(el.tag)
        mat = _inherit(inherited, el)
        if tag == "path":
            d = el.get("d")
            if not d:
                return
            subpaths = _path_subpaths(d, FLATTEN_TOL)
            closed: list[list[tuple[float, float]]] = []
            for sp in subpaths:
                pts = sp
                if abs(pts[0][0] - pts[-1][0]) < 1e-9 and abs(pts[0][1] - pts[-1][1]) < 1e-9:
                    pts = pts[:-1]
                else:
                    # Open subpath: only accept it when the endpoints nearly meet.
                    gap = math.hypot(pts[0][0] - pts[-1][0], pts[0][1] - pts[-1][1])
                    if gap > FLATTEN_TOL:
                        skipped_open += 1
                        continue
                if len(pts) >= 3:
                    closed.append(pts)
            if not closed:
                return
            if len(closed) > 1:
                multi_subpath += 1
                closed.sort(key=lambda p: abs(_poly_area(p)), reverse=True)
                holes_dropped += len(closed) - 1
            pts = closed[0]
        elif tag == "circle":
            try:
                cx, cy, r = float(el.get("cx", "0")), float(el.get("cy", "0")), float(el.get("r", "0"))
            except ValueError:
                return
            if r <= 0:
                return
            pts = _circle_points(cx, cy, r)
        else:
            return
        if mat is not None:
            pts = [_apply_mat(mat, x, y) for x, y in pts]
        # The viewBox origin always shifts user space, transform or not.
        pts = [(x - min_x, y - min_y) for x, y in pts]
        if abs(_poly_area(pts)) < MIN_AREA:
            skipped_speck += 1
            return
        if tid in seen_ids:
            base = tid
            n = 2
            while f"{base}_{n}" in seen_ids:
                n += 1
            tid = f"{base}_{n}"
            warnings.append(f"duplicate territory id renamed to {tid!r}")
        seen_ids.add(tid)
        territories.append({"tid": tid, "name": name, "pts": pts})

    if borders_layer is not None:
        for el in borders_layer.iter():
            if el is borders_layer:
                continue
            if _local(el.tag) in ("path", "circle"):
                consider(el, layer_tf)
    else:

        def walk(el: ET.Element, inherited: Matrix | None) -> None:
            tag = _local(el.tag)
            if tag in _SKIP_TAGS:
                return
            mat = _inherit(inherited, el)
            if tag in ("path", "circle"):
                consider(el, inherited)
            for child in el:
                walk(child, mat)

        walk(root, None)

    if skipped_unnamed:
        warnings.append(f"{skipped_unnamed} shape(s) had no id/inkscape:label and were skipped")
    if skipped_open:
        warnings.append(f"{skipped_open} open (non-closed) path subpath(es) were skipped")
    if multi_subpath:
        warnings.append(
            f"{multi_subpath} path(s) had multiple closed subpaths — outer boundary kept, "
            f"{holes_dropped} inner island/hole outline(s) dropped (they can be redrawn)"
        )
    if skipped_speck:
        warnings.append(f"{skipped_speck} degenerate shape(s) were skipped")
    if len(territories) > MAX_TERRITORIES:
        raise MapBuilderError(f"too many territories (max {MAX_TERRITORIES})")
    if not territories:
        raise MapBuilderError("no closed territory paths found in the SVG")
    return territories


# --- geometry helpers ----------------------------------------------------------

def _poly_area(pts: list[tuple[float, float]]) -> float:
    total = 0.0
    n = len(pts)
    for i in range(n):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % n]
        total += x1 * y2 - x2 * y1
    return total / 2.0


def _rdp(pts: list[tuple[float, float]], eps: float) -> list[tuple[float, float]]:
    if len(pts) < 3 or eps <= 0:
        return pts
    keep = [False] * len(pts)
    keep[0] = keep[-1] = True
    stack: list[tuple[int, int]] = [(0, len(pts) - 1)]
    while stack:
        s, e = stack.pop()
        if e - s < 2:
            continue
        ax, ay = pts[s]
        bx, by = pts[e]
        dx, dy = bx - ax, by - ay
        denom = math.hypot(dx, dy)
        max_d = -1.0
        idx = -1
        for i in range(s + 1, e):
            px, py = pts[i]
            if denom < 1e-12:
                d = math.hypot(px - ax, py - ay)
            else:
                d = abs((px - ax) * dy - (py - ay) * dx) / denom
            if d > max_d:
                max_d = d
                idx = i
        if max_d > eps and idx > 0:
            keep[idx] = True
            stack.append((s, idx))
            stack.append((idx, e))
    return [p for p, k in zip(pts, keep) if k]


def _point_in_poly(x: float, y: float, pts: list[tuple[float, float]]) -> bool:
    inside = False
    n = len(pts)
    j = n - 1
    for i in range(n):
        xi, yi = pts[i]
        xj, yj = pts[j]
        if (yi > y) != (yj > y):
            if x < (xj - xi) * (y - yi) / (yj - yi) + xi:
                inside = not inside
        j = i
    return inside


def _seg_dist(px: float, py: float, ax: float, ay: float, bx: float, by: float) -> tuple[float, float]:
    """Return (distance, t) with t clamped to [0, 1]."""
    dx, dy = bx - ax, by - ay
    len2 = dx * dx + dy * dy
    t = 0.0 if len2 == 0 else ((px - ax) * dx + (py - ay) * dy) / len2
    t = max(0.0, min(1.0, t))
    cx, cy = ax + t * dx, ay + t * dy
    return math.hypot(px - cx, py - cy), t


def _anchor_in_poly(pts: list[tuple[float, float]]) -> tuple[float, float] | None:
    """Approximate pole of inaccessibility: the deepest grid point inside the polygon."""
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
    if x1 - x0 < 1e-9 or y1 - y0 < 1e-9:
        return None
    n = len(pts)
    best: tuple[float, float] | None = None
    best_d = -1.0
    steps = 16
    for iy in range(steps + 1):
        for ix in range(steps + 1):
            x = x0 + (x1 - x0) * ix / steps
            y = y0 + (y1 - y0) * iy / steps
            if not _point_in_poly(x, y, pts):
                continue
            d = min(_seg_dist(x, y, *pts[i], *pts[(i + 1) % n])[0] for i in range(n))
            if d > best_d:
                best_d = d
                best = (x, y)
    if best is None:
        cx = (x0 + x1) / 2
        cy = (y0 + y1) / 2
        return (cx, cy) if _point_in_poly(cx, cy, pts) else None
    return best


# --- welding: merge near-coincident neighbour borders into shared edges ---------

class _Seg:
    __slots__ = ("a", "b", "tid", "idx")

    def __init__(self, a: tuple[float, float], b: tuple[float, float], tid: str, idx: int):
        self.a = a
        self.b = b
        self.tid = tid
        self.idx = idx  # index of a in its owner's pts at snapshot time


def _build_seg_grid(segs: list[_Seg], snap: float) -> dict[tuple[int, int], list[int]]:
    grid: dict[tuple[int, int], list[int]] = {}
    cell = max(snap, 1.0) * 2.0
    for idx, s in enumerate(segs):
        x0, x1 = sorted((s.a[0], s.b[0]))
        y0, y1 = sorted((s.a[1], s.b[1]))
        for cx in range(int((x0 - snap) // cell), int((x1 + snap) // cell) + 1):
            for cy in range(int((y0 - snap) // cell), int((y1 + snap) // cell) + 1):
                grid.setdefault((cx, cy), []).append(idx)
    return grid


def _weld(polys: list[dict[str, Any]], snap: float) -> tuple[int, int]:
    """Snap vertices of each polygon onto nearby borders of *other* polygons.

    Runs to convergence (max MAX_PASSES passes). Each pass computes every move
    against a consistent snapshot, so results are deterministic. Returns
    (vertices moved, borders split).
    """
    if snap <= 0:
        return 0, 0
    total_moves = 0
    total_splits = 0
    cell = max(snap, 1.0) * 2.0
    # Immune vertices (frame corners) are identified by position: split
    # insertions shift list indices, so index-based immunity would silently
    # detach as soon as a corner gains a neighbour vertex.
    immune_positions: dict[str, set[tuple[float, float]]] = {}
    for poly in polys:
        immune = poly.get("immune")
        if immune:
            positions = {poly["pts"][i] for i in immune}
            poly["_immune_pos"] = positions
            immune_positions[poly["tid"]] = positions
    for _pass in range(MAX_PASSES):
        segs: list[_Seg] = []
        for poly in polys:
            pts = poly["pts"]
            n = len(pts)
            for i in range(n):
                a, b = pts[i], pts[(i + 1) % n]
                if a != b:
                    segs.append(_Seg(a, b, poly["tid"], i))
        grid = _build_seg_grid(segs, snap)

        moves: dict[tuple[str, int], tuple[float, float]] = {}
        splits: dict[int, list[tuple[float, float]]] = {}
        for poly in polys:
            pts = poly["pts"]
            poly_immune = poly.get("_immune_pos")
            for vi, v in enumerate(pts):
                if poly_immune and v in poly_immune:
                    continue
                gx, gy = int(v[0] // cell), int(v[1] // cell)
                best_d = snap + 1.0
                best: tuple[tuple[float, float], int, _Seg, bool] | None = None
                for cx in range(gx - 1, gx + 2):
                    for cy in range(gy - 1, gy + 2):
                        for si in grid.get((cx, cy), ()):
                            s = segs[si]
                            if s.tid == poly["tid"]:
                                continue
                            d, t = _seg_dist(v[0], v[1], s.a[0], s.a[1], s.b[0], s.b[1])
                            if d > snap:
                                continue
                            needs_split = False
                            if t <= 0.0:
                                target = s.a
                                owner_immune = immune_positions.get(s.tid)
                                if owner_immune and s.a in owner_immune and d > ENDPOINT_JOIN_PX:
                                    # Immune frame corners anchor the border frame;
                                    # a farther vertex chasing one oscillates between
                                    # the corner and its own already-shared joint.
                                    continue
                            elif t >= 1.0:
                                target = s.b
                                owner_immune = immune_positions.get(s.tid)
                                if owner_immune and s.b in owner_immune and d > ENDPOINT_JOIN_PX:
                                    continue
                            else:
                                dx, dy = s.b[0] - s.a[0], s.b[1] - s.a[1]
                                target = (s.a[0] + t * dx, s.a[1] + t * dy)
                                if math.hypot(target[0] - s.a[0], target[1] - s.a[1]) <= ENDPOINT_JOIN_PX:
                                    target = s.a
                                elif math.hypot(target[0] - s.b[0], target[1] - s.b[1]) <= ENDPOINT_JOIN_PX:
                                    target = s.b
                                else:
                                    needs_split = True
                            if needs_split and d <= SHARED_POINT_EPS:
                                # v lies on this border: share the point right now,
                                # regardless of which move wins — otherwise other
                                # overlapping borders on the same line keep an
                                # orphaned T-junction after v moves on.
                                bucket = splits.setdefault(si, [])
                                if all(math.hypot(target[0] - q[0], target[1] - q[1]) > 1e-9 for q in bucket):
                                    bucket.append(target)
                            if target == v:
                                continue
                            if d < best_d:
                                best_d = d
                                best = (target, si, s, needs_split)
                if best is not None:
                    target, si, s, needs_split = best
                    if needs_split:
                        bucket = splits.setdefault(si, [])
                        if all(math.hypot(target[0] - q[0], target[1] - q[1]) > 1e-9 for q in bucket):
                            bucket.append(target)
                    moves[(poly["tid"], vi)] = target

        if not moves and not splits:
            break
        # Apply moves first: indices refer to the snapshot and moves never shift
        # them. Then insert split points by their recorded snapshot index (owner
        # may be hit by several splits in one pass → insert descending so
        # earlier indices stay valid), so splits can never be lost to drift.
        by_tid = {p["tid"]: p for p in polys}
        # Vertices at the same position share a joint: whichever of them has the
        # best real move leads, and every other poly's copy follows it — a
        # shared vertex must never be left behind when its partner moves.
        leaders: dict[tuple[float, float], tuple[float, tuple[float, float], tuple[str, int], tuple[float, float]]] = {}
        for (tid, vi), target in sorted(moves.items()):
            p = by_tid[tid]["pts"][vi]
            d = math.hypot(target[0] - p[0], target[1] - p[1])
            key = (round(p[0], 6), round(p[1], 6))
            cur = leaders.get(key)
            if cur is None or d < cur[0] or (d == cur[0] and (tid, vi) < cur[2]):
                leaders[key] = (d, target, (tid, vi), p)
        # Resolve mutual-chase cycles (A targets B while B targets A, or longer
        # chains) into a shared centroid, otherwise vertices oscillate forever.
        succ = {k: (round(v[1][0], 6), round(v[1][1], 6)) for k, v in leaders.items()}
        resolved: dict[tuple[float, float], tuple[float, float]] = {}
        color: dict[tuple[float, float], int] = {}
        for start in sorted(leaders):
            path: list[tuple[float, float]] = []
            k = start
            while k in leaders and k not in color:
                color[k] = 0
                path.append(k)
                k = succ[k]
            if k in leaders and color.get(k) == 0:
                cycle = path[path.index(k):]
                cx = sum(leaders[m][3][0] for m in cycle) / len(cycle)
                cy = sum(leaders[m][3][1] for m in cycle) / len(cycle)
                for m in cycle:
                    resolved[m] = (cx, cy)
            for m in path:
                color[m] = 1
        # Drain chains into their resolved endpoint (cycles first, stationary ends last).
        changed = True
        while changed:
            changed = False
            for key in sorted(leaders):
                if key in resolved:
                    continue
                s = succ[key]
                if s not in leaders:
                    resolved[key] = leaders[key][1]
                    changed = True
                elif s in resolved:
                    resolved[key] = resolved[s]
                    changed = True
        # A point that lands on a border line must be shared by every segment
        # of that line (the winner was chosen against one segment only, and
        # cycle resolution can move the vertex away from a raw split target):
        # otherwise overlapping collinear borders keep orphaned T-junctions
        # that no later pass can ever fix.
        all_targets = set(resolved.values())
        for bucket in splits.values():
            all_targets.update(bucket)
        for tgt in sorted(all_targets):
            gx2, gy2 = int(tgt[0] // cell), int(tgt[1] // cell)
            for cx in range(gx2 - 1, gx2 + 2):
                for cy in range(gy2 - 1, gy2 + 2):
                    for si in grid.get((cx, cy), ()):
                        s = segs[si]
                        d, t = _seg_dist(tgt[0], tgt[1], s.a[0], s.a[1], s.b[0], s.b[1])
                        if d <= SHARED_POINT_EPS and 0.0 < t < 1.0:
                            bucket = splits.setdefault(si, [])
                            if all(math.hypot(tgt[0] - q[0], tgt[1] - q[1]) > 1e-9 for q in bucket):
                                bucket.append(tgt)
        for poly in polys:
            poly_immune = poly.get("_immune_pos")
            for vi, p in enumerate(poly["pts"]):
                if poly_immune and p in poly_immune:
                    continue
                rkey = (round(p[0], 6), round(p[1], 6))
                tgt = resolved.get(rkey)
                if tgt is not None and tgt != p:
                    poly["pts"][vi] = tgt
        by_owner: dict[str, list[tuple[int, tuple[tuple[float, float], ...], tuple[float, float]]]] = {}
        for si, pts_to_add in splits.items():
            s = segs[si]
            by_owner.setdefault(s.tid, []).append((s.idx, tuple(pts_to_add), s.a))
        for tid, items in by_owner.items():
            owner = by_tid.get(tid)
            if owner is None:
                continue
            opts = owner["pts"]
            for idx, add, origin in sorted(items, key=lambda t: -t[0]):
                if idx >= len(opts):
                    continue
                order = sorted(add, key=lambda q: (q[0] - origin[0]) ** 2 + (q[1] - origin[1]) ** 2)
                for off, q in enumerate(order):
                    opts.insert(idx + 1 + off, q)
        # Drop consecutive duplicates created by joins.
        for poly in polys:
            cleaned: list[tuple[float, float]] = []
            for p in poly["pts"]:
                if not cleaned or p != cleaned[-1]:
                    cleaned.append(p)
            if len(cleaned) > 1 and cleaned[0] == cleaned[-1]:
                cleaned.pop()
            poly["pts"] = cleaned
        total_moves += len(moves)
        total_splits += sum(len(v) for v in splits.values())
    return total_moves, total_splits


# --- main entry ----------------------------------------------------------------

def _clamp_pts(pts: list[tuple[float, float]], w: float, h: float) -> tuple[list[tuple[float, float]], int]:
    out = []
    clamped = 0
    for x, y in pts:
        cx = min(max(x, 0.0), w)
        cy = min(max(y, 0.0), h)
        if cx != x or cy != y:
            clamped += 1
        out.append((cx, cy))
    return out, clamped


def _clean_poly(pts: list[tuple[float, float]]) -> list[tuple[float, float]]:
    cleaned: list[tuple[float, float]] = []
    for p in pts:
        if not cleaned or abs(p[0] - cleaned[-1][0]) > 1e-9 or abs(p[1] - cleaned[-1][1]) > 1e-9:
            cleaned.append(p)
    if len(cleaned) > 1 and abs(cleaned[0][0] - cleaned[-1][0]) < 1e-9 and abs(cleaned[0][1] - cleaned[-1][1]) < 1e-9:
        cleaned.pop()
    return cleaned


def import_map_svg(svg_text: str, snap: float | None = None) -> dict[str, Any]:
    """Convert a closed-path territory SVG into a builder document.

    Returns ``{ok, doc, stats, warnings}``. Raises ``MapBuilderError`` when the SVG
    cannot be imported (bad XML, no territories, size out of range).
    """
    if not isinstance(svg_text, str) or not svg_text.strip():
        raise MapBuilderError("svg must be a non-empty string")
    if len(svg_text) > MAX_SVG_CHARS:
        raise MapBuilderError(f"svg is too large (max {MAX_SVG_CHARS} characters)")
    if snap is None:
        snap = DEFAULT_SNAP
    if isinstance(snap, bool) or not isinstance(snap, (int, float)) or not math.isfinite(float(snap)):
        raise MapBuilderError("snap must be a number")
    snap = float(snap)
    if not (MIN_SNAP <= snap <= MAX_SNAP):
        raise MapBuilderError(f"snap must be between {MIN_SNAP:g} and {MAX_SNAP:g} px")

    try:
        root = ET.fromstring(svg_text)
    except ET.ParseError as e:
        raise MapBuilderError(f"invalid SVG XML: {e}") from e
    if _local(root.tag) != "svg":
        raise MapBuilderError("root element is not <svg>")

    warnings: list[str] = []
    w, h, min_x, min_y = _canvas_size(root)
    if not (MIN_DIM <= w <= MAX_DIM and MIN_DIM <= h <= MAX_DIM):
        raise MapBuilderError(f"map size must be between {MIN_DIM:g} and {MAX_DIM:g} px")

    territories = _collect_polygons(root, min_x, min_y, warnings)

    clamped_total = 0
    polys: list[dict[str, Any]] = []
    for t in territories:
        pts, clamped = _clamp_pts(t["pts"], w, h)
        clamped_total += clamped
        pts = _clean_poly(pts)
        pts = _rdp(pts, SIMPLIFY_EPS)
        pts = _clean_poly(pts)
        if len(pts) < 3 or abs(_poly_area(pts)) < MIN_AREA:
            warnings.append(f"territory {t['tid']!r} degenerated while simplifying and was skipped")
            continue
        polys.append({"tid": t["tid"], "name": t["name"], "pts": pts})
    if not polys:
        raise MapBuilderError("no usable territory polygons after simplification")
    if clamped_total:
        warnings.append(f"{clamped_total} point(s) were outside the canvas and clamped to its edges")

    # Canvas frame: only when the map is full-bleed on all four edges, so a
    # margin map doesn't gain a spurious unlabelled ring around its content.
    xs = [p[0] for poly in polys for p in poly["pts"]]
    ys = [p[1] for poly in polys for p in poly["pts"]]
    edge_tol = snap
    touches = [
        min(xs) <= edge_tol,
        max(xs) >= w - edge_tol,
        min(ys) <= edge_tol,
        max(ys) >= h - edge_tol,
    ]
    if all(touches):
        polys.append(
            {
                "tid": _FRAME_TID,
                "name": "frame",
                "pts": [(0.0, 0.0), (w, 0.0), (w, h), (0.0, h)],
                "immune": {0, 1, 2, 3},
            }
        )

    moves, splits = _weld(polys, snap)

    # Re-drop duplicates and degenerates the weld may have produced.
    kept: list[dict[str, Any]] = []
    for poly in polys:
        pts = _clean_poly(poly["pts"])
        if poly["tid"] == _FRAME_TID:
            if len(pts) >= 3:
                poly["pts"] = pts
                kept.append(poly)
            continue
        if len(pts) >= 3 and abs(_poly_area(pts)) >= MIN_AREA:
            poly["pts"] = pts
            kept.append(poly)
        else:
            warnings.append(f"territory {poly['tid']!r} collapsed during welding and was skipped")
    polys = kept

    # Face tracing relies on consistent winding: every territory ring must come
    # out CCW (positive shoelace) so its interior counts as an interior region.
    for poly in polys:
        if _poly_area(poly["pts"]) < 0:
            poly["pts"] = list(reversed(poly["pts"]))

    doc = _build_doc(polys, w, h)

    if len(doc["joints"]) > MAX_JOINTS:
        raise MapBuilderError(
            f"imported map is too complex ({len(doc['joints'])} joints > {MAX_JOINTS}) — try a larger snap"
        )
    if len(doc["borders"]) > MAX_BORDERS:
        raise MapBuilderError(
            f"imported map is too complex ({len(doc['borders'])} borders > {MAX_BORDERS}) — try a larger snap"
        )

    # Label anchors from the final welded geometry, then validate against the
    # same face trace the converter will run (surfacing over-merges as warnings).
    for poly in polys:
        if poly["tid"] == _FRAME_TID:
            continue
        anchor = _anchor_in_poly(poly["pts"])
        if anchor is None:
            warnings.append(f"could not find an interior point for {poly['tid']!r} — label it manually")
            continue
        doc["labels"][poly["tid"]] = {"display_name": poly["name"], "x": anchor[0], "y": anchor[1]}

    out = convert_map_builder(doc)
    stats = {
        "territories": len(polys) - (1 if any(p["tid"] == _FRAME_TID for p in polys) else 0),
        "joints": len(doc["joints"]),
        "borders": len(doc["borders"]),
        "regions": out["stats"]["regions"],
        "labelled": out["stats"]["labelled"],
        "welded_vertices": moves,
        "split_borders": splits,
        "frame": any(p["tid"] == _FRAME_TID for p in polys),
    }
    merged = warnings + out["warnings"]
    if len(doc["labels"]) < stats["territories"]:
        missing = stats["territories"] - len(doc["labels"])
        merged.append(f"{missing} territor(y/ies) could not be auto-labelled")
    return {"ok": True, "doc": doc, "stats": stats, "warnings": merged[:50]}


def _build_doc(polys: list[dict[str, Any]], w: float, h: float) -> dict[str, Any]:
    """Turn welded polygons into the builder's joints/borders/labels document."""
    joints: dict[str, dict[str, float]] = {}
    pos_to_id: dict[tuple[float, float], str] = {}

    def joint_id(x: float, y: float) -> str:
        key = (round(x, 6), round(y, 6))
        jid = pos_to_id.get(key)
        if jid is None:
            jid = f"j{len(pos_to_id) + 1}"
            pos_to_id[key] = jid
            joints[jid] = {"x": key[0], "y": key[1]}
        return jid

    # Frame corners get the stable ids the builder's resize logic expects.
    for jid, x, y in (("f_tl", 0.0, 0.0), ("f_tr", w, 0.0), ("f_br", w, h), ("f_bl", 0.0, h)):
        key = (round(x, 6), round(y, 6))
        if key not in pos_to_id:
            pos_to_id[key] = jid
            joints[jid] = {"x": key[0], "y": key[1]}

    edges: set[tuple[str, str]] = set()
    for poly in polys:
        pts = poly["pts"]
        n = len(pts)
        ids = [joint_id(x, y) for x, y in pts]
        for i in range(n):
            a, b = ids[i], ids[(i + 1) % n]
            if a == b:
                continue
            edges.add((a, b) if a < b else (b, a))

    borders: dict[str, dict[str, str]] = {}

    def add_edge(a: str, b: str, bid: str) -> None:
        borders[bid] = {"a": a, "b": b}

    def take_edge(a: str, b: str) -> tuple[str, str] | None:
        key = (a, b) if a < b else (b, a)
        if key in edges:
            edges.discard(key)
            return key
        return None

    def canonical(corner: str, other_corner: str, axis: str, value: float, bid: str) -> None:
        """Name an edge along a canvas side with its builder frame id."""
        got = take_edge(corner, other_corner)
        if got:
            add_edge(got[0], got[1], bid)
            return
        # The side got split by welded map vertices: claim the piece leaving the corner.
        candidates = sorted(
            (e for e in edges if corner in e and next(p for p in e if p != corner)),
            key=str,
        )
        for e in candidates:
            other = e[0] if e[1] == corner else e[1]
            pt = joints[other]
            if abs(pt[axis] - value) <= 1e-6:
                edges.discard(e)
                add_edge(e[0], e[1], bid)
                return

    if any(p["tid"] == _FRAME_TID for p in polys):
        canonical("f_tl", "f_tr", "y", 0.0, "fb_t")
        canonical("f_tr", "f_br", "x", w, "fb_r")
        canonical("f_br", "f_bl", "y", h, "fb_b")
        canonical("f_bl", "f_tl", "x", 0.0, "fb_l")

    for i, (a, b) in enumerate(sorted(edges), start=1):
        add_edge(a, b, f"b{i}")

    return {
        "width": w,
        "height": h,
        "joints": joints,
        "borders": borders,
        "labels": {},
    }
