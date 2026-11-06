"""Turn a territory SVG (one closed shape per region) into a shared-border builder document.

Existing maps draw every territory as its own closed path, so a shared border is stored
twice and the two copies rarely use the same coordinates. This welds vertices that fall
inside a snap tolerance, splits edges at T-junctions and crossings, and keeps one border
for each undirected segment. The result is the planar graph the map builder edits.
"""

from __future__ import annotations

import math
import re
import xml.etree.ElementTree as ET
from typing import Any

from backend.map_builder import (
    MAX_BORDERS,
    MAX_DIM,
    MAX_JOINTS,
    MAX_LABELS,
    MAX_NAME_LEN,
    MIN_DIM,
    MapBuilderError,
    _TID_RE,
    _point_in_ring,
    _trace_rings,
)

MAX_SVG_CHARS = 8_000_000
MAX_POINTS = 250_000
MAX_WARNINGS = 50
DEFAULT_TOLERANCE = 4.0
MIN_TOLERANCE = 0.0
MAX_TOLERANCE = 40.0
_FLAT = 0.75  # curve flattening error, px

_SVG_NS = "http://www.w3.org/2000/svg"
_INK_NS = "http://www.inkscape.org/namespaces/inkscape"
_SKIP_TAGS = {
    "defs",
    "clipPath",
    "mask",
    "symbol",
    "pattern",
    "marker",
    "linearGradient",
    "radialGradient",
    "filter",
    "metadata",
}
_SHAPE_TAGS = {"path", "polygon", "polyline", "rect", "circle", "ellipse"}

_NUM_RE = re.compile(r"[+-]?(?:\d+\.\d+|\d+\.|\.\d+|\d+)(?:[eE][+-]?\d+)?")
_TF_RE = re.compile(r"(matrix|translate|scale|rotate|skewX|skewY)\s*\(([^)]*)\)", re.I)
_PATH_ID_RE = re.compile(r"^path\d+$", re.I)

Matrix = tuple[float, float, float, float, float, float]
_IDENT: Matrix = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
Pt = tuple[float, float]


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _ink_label(el: ET.Element) -> str:
    return (el.get(f"{{{_INK_NS}}}label") or el.get("inkscape:label") or "").strip()


def _mul(m: Matrix, n: Matrix) -> Matrix:
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


def _apply(m: Matrix, x: float, y: float) -> Pt:
    a, b, c, d, e, f = m
    return (a * x + c * y + e, b * x + d * y + f)


def _parse_transform(raw: str | None) -> Matrix:
    if not raw or not raw.strip():
        return _IDENT
    m = _IDENT
    for name, args in _TF_RE.findall(raw):
        nums = [float(v) for v in _NUM_RE.findall(args)]
        name = name.lower()
        t = _IDENT
        if name == "matrix" and len(nums) >= 6:
            t = (nums[0], nums[1], nums[2], nums[3], nums[4], nums[5])
        elif name == "translate" and nums:
            tx = nums[0]
            ty = nums[1] if len(nums) > 1 else 0.0
            t = (1.0, 0.0, 0.0, 1.0, tx, ty)
        elif name == "scale" and nums:
            sx = nums[0]
            sy = nums[1] if len(nums) > 1 else sx
            t = (sx, 0.0, 0.0, sy, 0.0, 0.0)
        elif name == "rotate" and nums:
            ang = math.radians(nums[0])
            cos, sin = math.cos(ang), math.sin(ang)
            rot: Matrix = (cos, sin, -sin, cos, 0.0, 0.0)
            if len(nums) >= 3:
                cx, cy = nums[1], nums[2]
                t = _mul(_mul((1, 0, 0, 1, cx, cy), rot), (1, 0, 0, 1, -cx, -cy))
            else:
                t = rot
        elif name == "skewx" and nums:
            t = (1.0, 0.0, math.tan(math.radians(nums[0])), 1.0, 0.0, 0.0)
        elif name == "skewy" and nums:
            t = (1.0, math.tan(math.radians(nums[0])), 0.0, 1.0, 0.0, 0.0)
        else:
            continue
        m = _mul(m, t)
    return m


def _nums(text: str) -> list[float]:
    return [float(v) for v in _NUM_RE.findall(text or "")]


def _dedupe_ring(pts: list[Pt]) -> list[Pt]:
    out: list[Pt] = []
    for p in pts:
        if out and abs(out[-1][0] - p[0]) < 1e-9 and abs(out[-1][1] - p[1]) < 1e-9:
            continue
        out.append(p)
    if len(out) >= 2 and abs(out[0][0] - out[-1][0]) < 1e-9 and abs(out[0][1] - out[-1][1]) < 1e-9:
        out.pop()
    return out


def _ring_area(pts: list[Pt]) -> float:
    total = 0.0
    n = len(pts)
    for i in range(n):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % n]
        total += x1 * y2 - x2 * y1
    return total / 2.0


def _point_in_poly(x: float, y: float, pts: list[Pt]) -> bool:
    inside = False
    n = len(pts)
    j = n - 1
    for i in range(n):
        xi, yi = pts[i]
        xj, yj = pts[j]
        if (yi > y) != (yj > y):
            denom = yj - yi
            if denom != 0.0 and x < (xj - xi) * (y - yi) / denom + xi:
                inside = not inside
        j = i
    return inside


def _interior_point(pts: list[Pt]) -> Pt:
    a = 0.0
    cx = 0.0
    cy = 0.0
    n = len(pts)
    for i in range(n):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % n]
        cross = x1 * y2 - x2 * y1
        a += cross
        cx += (x1 + x2) * cross
        cy += (y1 + y2) * cross
    if abs(a) > 1e-9:
        cx /= 3.0 * a
        cy /= 3.0 * a
        if _point_in_poly(cx, cy, pts):
            return (cx, cy)
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    minx, maxx = min(xs), max(xs)
    miny, maxy = min(ys), max(ys)
    for steps in (8, 16, 32):
        for i in range(1, steps):
            for j in range(1, steps):
                x = minx + (maxx - minx) * i / steps
                y = miny + (maxy - miny) * j / steps
                if _point_in_poly(x, y, pts):
                    return (x, y)
    return (sum(xs) / n, sum(ys) / n)


def _flat_enough(p0: Pt, p1: Pt, p2: Pt, p3: Pt) -> bool:
    def dist(p: Pt) -> float:
        dx, dy = p3[0] - p0[0], p3[1] - p0[1]
        len2 = dx * dx + dy * dy
        if len2 < 1e-12:
            return math.hypot(p[0] - p0[0], p[1] - p0[1])
        t = ((p[0] - p0[0]) * dx + (p[1] - p0[1]) * dy) / len2
        return math.hypot(p[0] - (p0[0] + t * dx), p[1] - (p0[1] + t * dy))

    return dist(p1) <= _FLAT and dist(p2) <= _FLAT


def _mid(a: Pt, b: Pt) -> Pt:
    return ((a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0)


def _flatten_cubic(p0: Pt, p1: Pt, p2: Pt, p3: Pt, depth: int = 8) -> list[Pt]:
    if depth <= 0 or _flat_enough(p0, p1, p2, p3):
        return [p3]
    ab, bc, cd = _mid(p0, p1), _mid(p1, p2), _mid(p2, p3)
    abc, bcd = _mid(ab, bc), _mid(bc, cd)
    abcd = _mid(abc, bcd)
    return _flatten_cubic(p0, ab, abc, abcd, depth - 1) + _flatten_cubic(abcd, bcd, cd, p3, depth - 1)


def _flatten_quad(p0: Pt, p1: Pt, p2: Pt) -> list[Pt]:
    # Elevate to a cubic so one flattener covers both.
    c1 = (p0[0] + 2.0 / 3.0 * (p1[0] - p0[0]), p0[1] + 2.0 / 3.0 * (p1[1] - p0[1]))
    c2 = (p2[0] + 2.0 / 3.0 * (p1[0] - p2[0]), p2[1] + 2.0 / 3.0 * (p1[1] - p2[1]))
    return _flatten_cubic(p0, c1, c2, p2)


def _arc_points(x1: float, y1: float, rx: float, ry: float, phi_deg: float, large: int, sweep: int, x2: float, y2: float) -> list[Pt]:
    if abs(rx) < 1e-9 or abs(ry) < 1e-9 or (abs(x1 - x2) < 1e-9 and abs(y1 - y2) < 1e-9):
        return [(x2, y2)]
    phi = math.radians(phi_deg) % (2.0 * math.pi)
    cos_p, sin_p = math.cos(phi), math.sin(phi)
    dx, dy = (x1 - x2) / 2.0, (y1 - y2) / 2.0
    x1p = cos_p * dx + sin_p * dy
    y1p = -sin_p * dx + cos_p * dy
    rx, ry = abs(rx), abs(ry)
    lam = (x1p * x1p) / (rx * rx) + (y1p * y1p) / (ry * ry)
    if lam > 1.0:
        scale = math.sqrt(lam)
        rx *= scale
        ry *= scale
    sign = -1.0 if large == sweep else 1.0
    num = rx * rx * ry * ry - rx * rx * y1p * y1p - ry * ry * x1p * x1p
    den = rx * rx * y1p * y1p + ry * ry * x1p * x1p
    coef = sign * math.sqrt(max(0.0, num) / den) if den > 1e-12 else 0.0
    cxp = coef * (rx * y1p / ry)
    cyp = coef * -(ry * x1p / rx)
    cx = cos_p * cxp - sin_p * cyp + (x1 + x2) / 2.0
    cy = sin_p * cxp + cos_p * cyp + (y1 + y2) / 2.0

    def _ang(ux: float, uy: float, vx: float, vy: float) -> float:
        dot = ux * vx + uy * vy
        norm = math.hypot(ux, uy) * math.hypot(vx, vy)
        a = math.acos(max(-1.0, min(1.0, dot / norm))) if norm > 1e-12 else 0.0
        if ux * vy - uy * vx < 0:
            a = -a
        return a

    theta1 = _ang(1.0, 0.0, (x1p - cxp) / rx, (y1p - cyp) / ry)
    dtheta = _ang((x1p - cxp) / rx, (y1p - cyp) / ry, (-x1p - cxp) / rx, (-y1p - cyp) / ry)
    if sweep == 0 and dtheta > 0:
        dtheta -= 2.0 * math.pi
    elif sweep == 1 and dtheta < 0:
        dtheta += 2.0 * math.pi
    rmax = max(rx, ry, 1.0)
    # Chord error ≈ r (1 - cos(step/2)) ≤ _FLAT.
    cos_arg = max(-1.0, min(1.0, 1.0 - _FLAT / rmax))
    step = max(0.04, 2.0 * math.acos(cos_arg))
    n = max(1, min(256, int(math.ceil(abs(dtheta) / step))))
    pts: list[Pt] = []
    for i in range(1, n + 1):
        th = theta1 + dtheta * i / n
        x = cx + rx * math.cos(th) * cos_p - ry * math.sin(th) * sin_p
        y = cy + rx * math.cos(th) * sin_p + ry * math.sin(th) * cos_p
        pts.append((x, y))
    return pts


def _tokenize_path(d: str) -> list[str | float]:
    tokens: list[str | float] = []
    i = 0
    n = len(d)
    while i < n:
        c = d[i]
        if c in " \t\r\n,":
            i += 1
            continue
        if c.isalpha():
            tokens.append(c)
            i += 1
            continue
        m = _NUM_RE.match(d, i)
        if not m:
            raise MapBuilderError(f"unreadable path data near {d[i : i + 24]!r}")
        tokens.append(float(m.group()))
        i = m.end()
    return tokens


def _parse_path_rings(d: str) -> tuple[list[list[Pt]], int]:
    """Return closed rings and a count of open subpaths that were skipped.

    Extra coordinate pairs after a moveto are implicit lineto commands, matching
    the relative ``m x,y dx,dy … z`` paths used by the in-repo maps.
    """
    tokens = _tokenize_path(d)
    rings: list[list[Pt]] = []
    open_count = 0
    cx = cy = 0.0
    sx = sy = 0.0
    cmd: str | None = None
    # Last control point, and whether the previous command can be reflected.
    prev_cx = prev_cy = 0.0
    prev_qx = prev_qy = 0.0
    smooth_cubic = False
    smooth_quad = False
    current: list[Pt] = []
    i = 0

    def take(count: int) -> list[float]:
        nonlocal i
        if i + count > len(tokens):
            raise MapBuilderError("path command ran out of numbers")
        vals: list[float] = []
        for _ in range(count):
            tok = tokens[i]
            i += 1
            if not isinstance(tok, float):
                raise MapBuilderError("path command ran out of numbers")
            vals.append(tok)
        return vals

    def close_current() -> None:
        nonlocal current, cx, cy
        if len(current) >= 2:
            if math.hypot(current[0][0] - current[-1][0], current[0][1] - current[-1][1]) > 1e-6:
                current.append((sx, sy))
            ring = _dedupe_ring(current)
            if len(ring) >= 3 and abs(_ring_area(ring)) > 1e-4:
                rings.append(ring)
        current = []
        cx, cy = sx, sy

    while i < len(tokens):
        tok = tokens[i]
        if isinstance(tok, str):
            cmd = tok
            i += 1
        elif cmd is None:
            raise MapBuilderError("path data must start with a command")
        was_cubic = smooth_cubic
        was_quad = smooth_quad
        smooth_cubic = False
        smooth_quad = False
        if cmd in ("M", "m"):
            if len(current) >= 2:
                open_count += 1
            current = []
            x, y = take(2)
            if cmd == "m":
                x += cx
                y += cy
            cx, cy = x, y
            sx, sy = x, y
            current = [(x, y)]
            cmd = "L" if cmd == "M" else "l"
        elif cmd in ("L", "l"):
            x, y = take(2)
            if cmd == "l":
                x += cx
                y += cy
            cx, cy = x, y
            current.append((cx, cy))
        elif cmd in ("H", "h"):
            (x,) = take(1)
            if cmd == "h":
                x += cx
            cx = x
            current.append((cx, cy))
        elif cmd in ("V", "v"):
            (y,) = take(1)
            if cmd == "v":
                y += cy
            cy = y
            current.append((cx, cy))
        elif cmd in ("C", "c"):
            x1, y1, x2, y2, x, y = take(6)
            if cmd == "c":
                x1 += cx
                y1 += cy
                x2 += cx
                y2 += cy
                x += cx
                y += cy
            for p in _flatten_cubic((cx, cy), (x1, y1), (x2, y2), (x, y)):
                current.append(p)
            prev_cx, prev_cy = x2, y2
            cx, cy = x, y
            smooth_cubic = True
        elif cmd in ("S", "s"):
            x2, y2, x, y = take(4)
            if cmd == "s":
                x2 += cx
                y2 += cy
                x += cx
                y += cy
            if was_cubic:
                x1, y1 = 2 * cx - prev_cx, 2 * cy - prev_cy
            else:
                x1, y1 = cx, cy
            for p in _flatten_cubic((cx, cy), (x1, y1), (x2, y2), (x, y)):
                current.append(p)
            prev_cx, prev_cy = x2, y2
            cx, cy = x, y
            smooth_cubic = True
        elif cmd in ("Q", "q"):
            x1, y1, x, y = take(4)
            if cmd == "q":
                x1 += cx
                y1 += cy
                x += cx
                y += cy
            for p in _flatten_quad((cx, cy), (x1, y1), (x, y)):
                current.append(p)
            prev_qx, prev_qy = x1, y1
            cx, cy = x, y
            smooth_quad = True
        elif cmd in ("T", "t"):
            x, y = take(2)
            if cmd == "t":
                x += cx
                y += cy
            if was_quad:
                x1, y1 = 2 * cx - prev_qx, 2 * cy - prev_qy
            else:
                x1, y1 = cx, cy
            for p in _flatten_quad((cx, cy), (x1, y1), (x, y)):
                current.append(p)
            prev_qx, prev_qy = x1, y1
            cx, cy = x, y
            smooth_quad = True
        elif cmd in ("A", "a"):
            rx, ry, phi, large, sweep, x, y = take(7)
            if cmd == "a":
                x += cx
                y += cy
            for p in _arc_points(cx, cy, rx, ry, phi, int(abs(large)), int(abs(sweep)), x, y):
                current.append(p)
            cx, cy = x, y
        elif cmd in ("Z", "z"):
            close_current()
        else:
            raise MapBuilderError(f"unsupported path command {cmd!r}")
    if len(current) >= 2:
        open_count += 1
    return rings, open_count


def _sample_count(radius: float) -> int:
    r = max(radius, 1.0)
    cos_arg = max(-1.0, min(1.0, 1.0 - _FLAT / r))
    step = max(0.04, 2.0 * math.acos(cos_arg))
    return max(12, min(256, int(math.ceil(2.0 * math.pi / step))))


def _shape_rings(el: ET.Element, ctm: Matrix) -> tuple[list[list[Pt]], int]:
    """Closed rings in parent coordinates, plus skipped open subpaths."""
    tag = _local(el.tag)
    rings: list[list[Pt]] = []
    open_count = 0
    if tag == "path":
        d = el.get("d") or ""
        if not d.strip():
            return [], 0
        rings, open_count = _parse_path_rings(d)
    elif tag in ("polygon", "polyline"):
        nums = _nums(el.get("points") or "")
        if len(nums) < 4:
            return [], 0
        pts = [(nums[i], nums[i + 1]) for i in range(0, len(nums) - 1, 2)]
        if tag == "polyline":
            if math.hypot(pts[0][0] - pts[-1][0], pts[0][1] - pts[-1][1]) > 1.0:
                return [], 1
        ring = _dedupe_ring(pts)
        if len(ring) >= 3:
            rings = [ring]
    elif tag == "rect":
        x = float(el.get("x") or 0)
        y = float(el.get("y") or 0)
        w = float(el.get("width") or 0)
        h = float(el.get("height") or 0)
        if w > 0 and h > 0:
            rings = [[(x, y), (x + w, y), (x + w, y + h), (x, y + h)]]
    elif tag == "circle":
        cx = float(el.get("cx") or 0)
        cy = float(el.get("cy") or 0)
        r = float(el.get("r") or 0)
        if r > 0:
            n = _sample_count(r)
            rings = [[(cx + r * math.cos(2 * math.pi * i / n), cy + r * math.sin(2 * math.pi * i / n)) for i in range(n)]]
    elif tag == "ellipse":
        cx = float(el.get("cx") or 0)
        cy = float(el.get("cy") or 0)
        rx = float(el.get("rx") or 0)
        ry = float(el.get("ry") or 0)
        if rx > 0 and ry > 0:
            n = _sample_count(max(rx, ry))
            rings = [[(cx + rx * math.cos(2 * math.pi * i / n), cy + ry * math.sin(2 * math.pi * i / n)) for i in range(n)]]
    transformed = [[_apply(ctm, x, y) for x, y in ring] for ring in rings]
    return transformed, open_count


def _identity_for(el: ET.Element) -> tuple[str, str] | None:
    raw_id = (el.get("id") or "").strip()
    label = _ink_label(el)
    if raw_id and not _PATH_ID_RE.match(raw_id):
        name = label or raw_id
        tid = raw_id.lower()
    elif label:
        name = label
        tid = label.lower().replace("'", "").replace(" ", "_").replace("-", "_")
    else:
        return None
    tid = re.sub(r"[^a-z0-9_.-]", "", tid)
    if not tid or not _TID_RE.match(tid):
        return None
    name = name.strip() or tid
    if len(name) > MAX_NAME_LEN:
        name = name[:MAX_NAME_LEN]
    return tid, name


def _viewbox(root: ET.Element) -> tuple[float, float, float, float] | None:
    raw = root.get("viewBox") or root.get("viewbox")
    if raw:
        nums = _nums(raw)
        if len(nums) >= 4 and nums[2] > 0 and nums[3] > 0:
            return nums[0], nums[1], nums[2], nums[3]
    w_raw = root.get("width") or ""
    h_raw = root.get("height") or ""
    w_nums, h_nums = _nums(w_raw), _nums(h_raw)
    if w_nums and h_nums and w_nums[0] > 0 and h_nums[0] > 0 and "%" not in w_raw and "%" not in h_raw:
        return 0.0, 0.0, w_nums[0], h_nums[0]
    return None


def _collect_territories(root: ET.Element) -> tuple[list[dict[str, Any]], list[str]]:
    borders_only = any(_local(el.tag) == "g" and _ink_label(el) == "svg_borders" for el in root.iter())
    warnings: list[str] = []
    found: list[dict[str, Any]] = []
    used: set[str] = set()

    def walk(el: ET.Element, ctm: Matrix, skip: int, in_borders: int) -> None:
        tag = _local(el.tag)
        if tag in _SKIP_TAGS:
            skip += 1
        local_tf = _parse_transform(el.get("transform"))
        ctm = _mul(ctm, local_tf)
        if tag == "g" and _ink_label(el) == "svg_borders":
            in_borders += 1
        if skip == 0 and (not borders_only or in_borders > 0) and tag in _SHAPE_TAGS:
            ident = _identity_for(el)
            if ident is None:
                if (el.get("d") or el.get("points") or tag in ("rect", "circle", "ellipse")):
                    warnings.append(f"skipped a <{tag}> with no territory id or label")
            else:
                tid, name = ident
                if tid in used:
                    n = 2
                    while f"{tid}_{n}" in used:
                        n += 1
                    warnings.append(f"duplicate id {tid!r} renamed to {tid}_{n!r}")
                    tid = f"{tid}_{n}"
                used.add(tid)
                try:
                    rings, open_count = _shape_rings(el, ctm)
                except MapBuilderError as e:
                    warnings.append(f"{tid}: {e}")
                    rings, open_count = [], 0
                if open_count:
                    warnings.append(f"{tid}: skipped {open_count} open subpath(s); only closed paths become borders")
                if not rings:
                    warnings.append(f"{tid}: no closed path")
                else:
                    if len(rings) > 1:
                        warnings.append(
                            f"{tid}: {len(rings)} closed subpaths; the largest is labelled and the others stay in the graph"
                        )
                    found.append({"id": tid, "name": name, "rings": rings})
        for child in list(el):
            walk(child, ctm, skip, in_borders)

    walk(root, _IDENT, 0, 0)
    if len(found) > MAX_LABELS:
        raise MapBuilderError(f"too many territories (max {MAX_LABELS})")
    return found, warnings


class _UnionFind:
    def __init__(self, n: int) -> None:
        self.parent = list(range(n))
        self.members: list[list[int]] = [[i] for i in range(n)]

    def find(self, i: int) -> int:
        while self.parent[i] != i:
            self.parent[i] = self.parent[self.parent[i]]
            i = self.parent[i]
        return i

    def union(self, a: int, b: int, points: list[Pt], tol: float) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return
        ma, mb = self.members[ra], self.members[rb]
        if len(ma) + len(mb) > 32:
            return
        limit = tol * tol + 1e-9
        for i in ma:
            pi = points[i]
            for j in mb:
                pj = points[j]
                if (pi[0] - pj[0]) ** 2 + (pi[1] - pj[1]) ** 2 > limit:
                    return
        self.parent[rb] = ra
        ma.extend(mb)
        self.members[rb] = []


def _weld_points(points: list[Pt], tol: float) -> list[int]:
    """Map each point index to a cluster root. Clusters stay inside a ball of diameter ``tol``."""
    n = len(points)
    if n == 0:
        return []
    uf = _UnionFind(n)
    if tol <= 0:
        # Exact duplicates only (exporter round-trip).
        seen: dict[tuple[int, int], int] = {}
        for i, (x, y) in enumerate(points):
            key = (round(x * 1_000_000), round(y * 1_000_000))
            prev = seen.get(key)
            if prev is None:
                seen[key] = i
            else:
                uf.union(prev, i, points, 1e-4)
        return [uf.find(i) for i in range(n)]

    cell = tol
    buckets: dict[tuple[int, int], list[int]] = {}
    for i, (x, y) in enumerate(points):
        buckets.setdefault((math.floor(x / cell), math.floor(y / cell)), []).append(i)
    pairs: list[tuple[float, int, int]] = []
    limit = tol * tol + 1e-9
    for i, (x, y) in enumerate(points):
        cx, cy = math.floor(x / cell), math.floor(y / cell)
        for ox in (-1, 0, 1):
            for oy in (-1, 0, 1):
                for j in buckets.get((cx + ox, cy + oy), ()):
                    if j <= i:
                        continue
                    d2 = (x - points[j][0]) ** 2 + (y - points[j][1]) ** 2
                    if d2 <= limit:
                        pairs.append((d2, i, j))
    pairs.sort()
    for _d2, i, j in pairs:
        uf.union(i, j, points, tol)
    return [uf.find(i) for i in range(n)]


def _split_tjunctions(rings: list[list[int]], pos: dict[int, Pt], tol: float) -> list[list[int]]:
    if tol <= 0 or not rings:
        return rings
    cell = tol
    grid: dict[tuple[int, int], list[int]] = {}
    for cid, (x, y) in pos.items():
        grid.setdefault((math.floor(x / cell), math.floor(y / cell)), []).append(cid)

    def near(a: int, b: int) -> list[int]:
        ax, ay = pos[a]
        bx, by = pos[b]
        dx, dy = bx - ax, by - ay
        len2 = dx * dx + dy * dy
        if len2 < 1e-12:
            return []
        length = math.sqrt(len2)
        minx, maxx = min(ax, bx) - tol, max(ax, bx) + tol
        miny, maxy = min(ay, by) - tol, max(ay, by) + tol
        hits: list[tuple[float, int]] = []
        seen: set[int] = set()
        for ix in range(math.floor(minx / cell), math.floor(maxx / cell) + 1):
            for iy in range(math.floor(miny / cell), math.floor(maxy / cell) + 1):
                for cid in grid.get((ix, iy), ()):
                    if cid in seen or cid == a or cid == b:
                        continue
                    seen.add(cid)
                    px, py = pos[cid]
                    t = ((px - ax) * dx + (py - ay) * dy) / len2
                    if t <= 1e-4 or t >= 1.0 - 1e-4:
                        continue
                    dist = abs(dx * (ay - py) - dy * (ax - px)) / length
                    if dist <= tol:
                        hits.append((t, cid))
        hits.sort()
        return [cid for _t, cid in hits]

    out: list[list[int]] = []
    for ring in rings:
        rebuilt: list[int] = []
        n = len(ring)
        for i in range(n):
            a = ring[i]
            b = ring[(i + 1) % n]
            if not rebuilt or rebuilt[-1] != a:
                rebuilt.append(a)
            for cid in near(a, b):
                if rebuilt[-1] != cid:
                    rebuilt.append(cid)
        compact: list[int] = []
        for cid in rebuilt:
            if not compact or compact[-1] != cid:
                compact.append(cid)
        if len(compact) >= 2 and compact[0] == compact[-1]:
            compact.pop()
        if len(set(compact)) >= 3:
            out.append(compact)
    return out


def _split_crossings(rings: list[list[int]], pos: dict[int, Pt]) -> tuple[list[list[int]], dict[int, Pt]]:
    """Insert a joint where two borders cross so the graph stays planar."""
    edges: list[tuple[int, int]] = []
    seen: set[tuple[int, int]] = set()
    for ring in rings:
        n = len(ring)
        for i in range(n):
            a, b = ring[i], ring[(i + 1) % n]
            if a == b:
                continue
            key = (a, b) if a < b else (b, a)
            if key in seen:
                continue
            seen.add(key)
            edges.append(key)

    def bbox(a: int, b: int) -> tuple[float, float, float, float]:
        ax, ay = pos[a]
        bx, by = pos[b]
        return min(ax, bx), min(ay, by), max(ax, bx), max(ay, by)

    boxes = [bbox(a, b) for a, b in edges]
    cell = 32.0
    buckets: dict[tuple[int, int], list[int]] = {}
    for idx, (x0, y0, x1, y1) in enumerate(boxes):
        for ix in range(math.floor(x0 / cell), math.floor(x1 / cell) + 1):
            for iy in range(math.floor(y0 / cell), math.floor(y1 / cell) + 1):
                buckets.setdefault((ix, iy), []).append(idx)

    cuts: dict[tuple[int, int], list[tuple[float, Pt]]] = {}
    next_id = (max(pos) + 1) if pos else 1
    checked: set[tuple[int, int]] = set()
    for idxs in buckets.values():
        for ii in range(len(idxs)):
            for jj in range(ii + 1, len(idxs)):
                i, j = idxs[ii], idxs[jj]
                if i == j:
                    continue
                pair = (i, j) if i < j else (j, i)
                if pair in checked:
                    continue
                checked.add(pair)
                a, b = edges[i]
                c, d = edges[j]
                if len({a, b, c, d}) < 4:
                    continue
                ax0, ay0, ax1, ay1 = boxes[i]
                bx0, by0, bx1, by1 = boxes[j]
                if ax1 < bx0 or bx1 < ax0 or ay1 < by0 or by1 < ay0:
                    continue
                p, p2 = pos[a], pos[b]
                q, q2 = pos[c], pos[d]
                rx, ry = p2[0] - p[0], p2[1] - p[1]
                sx, sy = q2[0] - q[0], q2[1] - q[1]
                den = rx * sy - ry * sx
                if abs(den) < 1e-8:
                    continue
                qpx, qpy = q[0] - p[0], q[1] - p[1]
                t = (qpx * sy - qpy * sx) / den
                u = (qpx * ry - qpy * rx) / den
                if not (1e-4 < t < 1.0 - 1e-4 and 1e-4 < u < 1.0 - 1e-4):
                    continue
                pt = (p[0] + t * rx, p[1] + t * ry)
                cuts.setdefault((a, b), []).append((t, pt))
                cuts.setdefault((c, d), []).append((u, pt))
                if len(cuts) > 8000:
                    break

    if not cuts:
        return rings, pos

    # One joint per geometric point so both edges share it.
    point_ids: list[tuple[Pt, int]] = []

    def joint_for(pt: Pt) -> int:
        nonlocal next_id
        for prev, jid in point_ids:
            if abs(prev[0] - pt[0]) < 1e-6 and abs(prev[1] - pt[1]) < 1e-6:
                return jid
        jid = next_id
        next_id += 1
        pos[jid] = pt
        point_ids.append((pt, jid))
        return jid

    split_ids: dict[tuple[int, int], list[int]] = {}
    for key, hits in cuts.items():
        hits.sort()
        ids = []
        for _t, pt in hits:
            jid = joint_for(pt)
            if not ids or ids[-1] != jid:
                ids.append(jid)
        split_ids[key] = ids

    out: list[list[int]] = []
    for ring in rings:
        rebuilt: list[int] = []
        n = len(ring)
        for i in range(n):
            a = ring[i]
            b = ring[(i + 1) % n]
            if not rebuilt or rebuilt[-1] != a:
                rebuilt.append(a)
            key = (a, b) if a < b else (b, a)
            extra = split_ids.get(key, [])
            if a > b:
                extra = list(reversed(extra))
            for jid in extra:
                if rebuilt[-1] != jid:
                    rebuilt.append(jid)
        compact = []
        for jid in rebuilt:
            if not compact or compact[-1] != jid:
                compact.append(jid)
        if len(compact) >= 2 and compact[0] == compact[-1]:
            compact.pop()
        if len(set(compact)) >= 3:
            out.append(compact)
    return out, pos


def _collapse_slivers(
    pos: dict[int, Pt],
    edge_count: dict[tuple[int, int], int],
    labels: list[Pt],
    tol: float,
) -> tuple[dict[tuple[int, int], int], int]:
    """Pull the two rails of a hairline gap onto one centerline and merge them.

    Double-traced borders that missed the snap tolerance leave a long, paper-thin
    face. Those faces are unlabelled. Real gaps and labelled territories are left
    alone. Returns the rebuilt edge map and how many faces were collapsed.
    """
    collapsed = 0
    for _ in range(4):
        used = {cid for pair in edge_count for cid in pair}
        if len(used) < 3:
            break
        joints = {str(cid): pos[cid] for cid in used}
        borders = {str(i): (str(a), str(b)) for i, (a, b) in enumerate(edge_count)}
        rings = _trace_rings(joints, borders)
        targets: dict[int, list[Pt]] = {}
        batch = 0
        max_width = max(tol * 2.0, 8.0)
        for area, ring in rings:
            if area <= 1.0 or area > 50_000:
                continue
            if any(_point_in_ring(x, y, ring, joints) for x, y in labels):
                continue
            pts = [joints[j] for j in ring]
            per = 0.0
            for i in range(len(ring)):
                a, b = pts[i], pts[(i + 1) % len(pts)]
                per += math.hypot(a[0] - b[0], a[1] - b[1])
            if per < 1.0:
                continue
            mean_width = (2.0 * area) / per
            if mean_width > max_width:
                continue
            cx = sum(p[0] for p in pts) / len(pts)
            cy = sum(p[1] for p in pts) / len(pts)
            sxx = sum((p[0] - cx) ** 2 for p in pts)
            syy = sum((p[1] - cy) ** 2 for p in pts)
            sxy = sum((p[0] - cx) * (p[1] - cy) for p in pts)
            ang = 0.5 * math.atan2(2.0 * sxy, sxx - syy)
            dx, dy = math.cos(ang), math.sin(ang)
            for jid in ring:
                cid = int(jid)
                x, y = pos[cid]
                t = (x - cx) * dx + (y - cy) * dy
                targets.setdefault(cid, []).append((cx + t * dx, cy + t * dy))
            batch += 1
        if batch == 0:
            break
        collapsed += batch
        for cid, pts in targets.items():
            pos[cid] = (sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts))
        # Rails now lie on one line, so a tight tolerance merges them without
        # grabbing unrelated borders that merely pass nearby.
        edge_count = _resnap_edges(pos, edge_count, 1.25)
    return edge_count, collapsed


def _resnap_edges(
    pos: dict[int, Pt], edge_count: dict[tuple[int, int], int], tol: float
) -> dict[tuple[int, int], int]:
    """Weld vertices that now coincide and split edges at vertices lying on them."""
    ids = list({cid for pair in edge_count for cid in pair})
    # Weld.
    points = [pos[cid] for cid in ids]
    roots = _weld_points(points, min(tol, 1.5))
    root_of = {ids[i]: roots[i] for i in range(len(ids))}
    groups: dict[int, list[int]] = {}
    for cid, root in root_of.items():
        groups.setdefault(root, []).append(cid)
    canon: dict[int, int] = {}
    for group in groups.values():
        keep = min(group)
        sx = sum(pos[c][0] for c in group) / len(group)
        sy = sum(pos[c][1] for c in group) / len(group)
        pos[keep] = (sx, sy)
        for c in group:
            canon[c] = keep

    edges: dict[tuple[int, int], int] = {}
    for a, b in edge_count:
        ca, cb = canon.get(a, a), canon.get(b, b)
        if ca == cb:
            continue
        key = (ca, cb) if ca < cb else (cb, ca)
        edges[key] = edges.get(key, 0) + edge_count[(a, b)]

    # Split at vertices that sit on another edge (staggered rails of a collapsed sliver).
    cell = max(tol, 1.0)
    grid: dict[tuple[int, int], list[int]] = {}
    live = {cid for pair in edges for cid in pair}
    for cid in live:
        x, y = pos[cid]
        grid.setdefault((math.floor(x / cell), math.floor(y / cell)), []).append(cid)

    pending: dict[tuple[int, int], list[tuple[float, int]]] = {}
    for a, b in list(edges):
        ax, ay = pos[a]
        bx, by = pos[b]
        dx, dy = bx - ax, by - ay
        len2 = dx * dx + dy * dy
        if len2 < 1e-8:
            continue
        length = math.sqrt(len2)
        minx, maxx = min(ax, bx) - tol, max(ax, bx) + tol
        miny, maxy = min(ay, by) - tol, max(ay, by) + tol
        hits: list[tuple[float, int]] = []
        seen: set[int] = set()
        for ix in range(math.floor(minx / cell), math.floor(maxx / cell) + 1):
            for iy in range(math.floor(miny / cell), math.floor(maxy / cell) + 1):
                for cid in grid.get((ix, iy), ()):
                    if cid in seen or cid == a or cid == b:
                        continue
                    seen.add(cid)
                    px, py = pos[cid]
                    t = ((px - ax) * dx + (py - ay) * dy) / len2
                    if t <= 1e-3 or t >= 1.0 - 1e-3:
                        continue
                    dist = abs(dx * (ay - py) - dy * (ax - px)) / length
                    if dist <= tol:
                        hits.append((t, cid))
        if hits:
            hits.sort()
            pending[(a, b)] = hits

    for (a, b), hits in pending.items():
        count = edges.pop((a, b), 0)
        if count <= 0:
            continue
        chain = [a] + [cid for _t, cid in hits] + [b]
        for u, v in zip(chain, chain[1:]):
            if u == v:
                continue
            key = (u, v) if u < v else (v, u)
            if (pos[u][0] - pos[v][0]) ** 2 + (pos[u][1] - pos[v][1]) ** 2 < 1e-8:
                continue
            edges[key] = edges.get(key, 0) + count
    return edges


def _round_pt(p: Pt) -> Pt:
    return (round(p[0], 4), round(p[1], 4))


def _build_doc(
    territories: list[dict[str, Any]],
    tol: float,
    width: float,
    height: float,
    warnings: list[str],
) -> dict[str, Any]:
    points: list[Pt] = []
    ring_slices: list[tuple[int, int]] = []  # start, count into points, one per ring
    owners: list[tuple[str, str, int]] = []  # tid, name, ring index in ring_slices
    for terr in territories:
        rings: list[list[Pt]] = terr["rings"]
        best = max(range(len(rings)), key=lambda i: abs(_ring_area(rings[i])))
        for idx, ring in enumerate(rings):
            ring_slices.append((len(points), len(ring)))
            points.extend(ring)
            if idx == best:
                owners.append((terr["id"], terr["name"], len(ring_slices) - 1))
    if len(points) > MAX_POINTS:
        raise MapBuilderError(f"SVG has too many points (max {MAX_POINTS})")

    roots = _weld_points(points, tol)
    # Representative position: centroid of the cluster.
    buckets: dict[int, list[Pt]] = {}
    for idx, root in enumerate(roots):
        buckets.setdefault(root, []).append(points[idx])
    root_ids: dict[int, int] = {}
    pos: dict[int, Pt] = {}
    for n, root in enumerate(sorted(buckets), start=1):
        pts = buckets[root]
        pos[n] = (sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts))
        root_ids[root] = n

    rings: list[list[int]] = []
    for start, count in ring_slices:
        ring: list[int] = []
        for idx in range(start, start + count):
            cid = root_ids[roots[idx]]
            if not ring or ring[-1] != cid:
                ring.append(cid)
        if len(ring) >= 2 and ring[0] == ring[-1]:
            ring.pop()
        if len(set(ring)) >= 3:
            rings.append(ring)
        else:
            rings.append([])

    pos_rings = [ring for ring in rings if ring]
    pos_rings = _split_tjunctions(pos_rings, pos, tol)
    pos_rings, pos = _split_crossings(pos_rings, pos)

    # Rebuild a parallel list so label rings that collapsed become empty.
    # Labels use the original owner ring's interior point, computed before welding.
    label_pts: dict[str, tuple[str, Pt]] = {}
    for tid, name, ring_index in owners:
        start, count = ring_slices[ring_index]
        original = points[start : start + count]
        label_pts[tid] = (name, _interior_point(original))

    # Edges.
    edge_count: dict[tuple[int, int], int] = {}
    for ring in pos_rings:
        n = len(ring)
        for i in range(n):
            a, b = ring[i], ring[(i + 1) % n]
            if a == b:
                continue
            ax, ay = pos[a]
            bx, by = pos[b]
            if (ax - bx) ** 2 + (ay - by) ** 2 < 1e-12:
                continue
            key = (a, b) if a < b else (b, a)
            edge_count[key] = edge_count.get(key, 0) + 1

    label_anchors = [pt for _name, pt in label_pts.values()]
    edge_count, collapsed = _collapse_slivers(pos, edge_count, label_anchors, tol)
    if collapsed:
        warnings.append(
            f"collapsed {collapsed} hairline gap(s) into shared borders (snap tolerance {tol:g} px)"
        )

    if len(edge_count) > MAX_BORDERS:
        raise MapBuilderError(f"too many borders (max {MAX_BORDERS})")
    if len(pos) > MAX_JOINTS:
        raise MapBuilderError(f"too many joints (max {MAX_JOINTS})")

    # Drop joints that no edge uses, and renumber deterministically.
    used = {cid for pair in edge_count for cid in pair}
    ordered = sorted(used, key=lambda cid: (round(pos[cid][1], 4), round(pos[cid][0], 4), cid))
    id_of = {cid: f"j{n}" for n, cid in enumerate(ordered, start=1)}
    joints = {id_of[cid]: {"x": _round_pt(pos[cid])[0], "y": _round_pt(pos[cid])[1]} for cid in ordered}
    borders = {}
    for n, (a, b) in enumerate(sorted(edge_count, key=lambda e: (id_of[e[0]], id_of[e[1]])), start=1):
        borders[f"b{n}"] = {"a": id_of[a], "b": id_of[b]}

    # Fit the frame. A few units of Inkscape overrun is clamped so the viewBox
    # stays aligned with the map's background image. Larger overrun grows the frame.
    xs = [j["x"] for j in joints.values()] or [0.0]
    ys = [j["y"] for j in joints.values()] or [0.0]
    minx, miny, maxx, maxy = min(xs), min(ys), max(xs), max(ys)
    slack = 8.0
    shift_x = 0.0
    shift_y = 0.0
    if minx >= -slack and miny >= -slack and maxx <= width + slack and maxy <= height + slack:
        overrun = minx < -0.01 or miny < -0.01 or maxx > width + 0.01 or maxy > height + 0.01
        for j in joints.values():
            j["x"] = round(min(max(j["x"], 0.0), width), 4)
            j["y"] = round(min(max(j["y"], 0.0), height), 4)
        if overrun:
            warnings.append("clamped a few coordinates that fell just outside the viewBox")
    else:
        shift_x = -minx if minx < 0 else 0.0
        shift_y = -miny if miny < 0 else 0.0
        if shift_x or shift_y:
            warnings.append("coordinates fell outside the viewBox and were shifted into the frame")
            for j in joints.values():
                j["x"] = round(j["x"] + shift_x, 4)
                j["y"] = round(j["y"] + shift_y, 4)
        width = max(width + shift_x, max(j["x"] for j in joints.values()))
        height = max(height + shift_y, max(j["y"] for j in joints.values()))
    if not (MIN_DIM <= width <= MAX_DIM) or not (MIN_DIM <= height <= MAX_DIM):
        raise MapBuilderError(
            f"map size {width:g}×{height:g} is outside {MIN_DIM:g}–{MAX_DIM:g} px"
        )

    labels: dict[str, dict[str, Any]] = {}
    for tid, (name, (lx, ly)) in label_pts.items():
        lx = min(max(lx + shift_x, 0.0), width)
        ly = min(max(ly + shift_y, 0.0), height)
        labels[tid] = {"display_name": name, "x": round(lx, 4), "y": round(ly, 4)}

    dead = [
        bid
        for bid, b in borders.items()
        if abs(joints[b["a"]]["x"] - joints[b["b"]]["x"]) < 1e-6
        and abs(joints[b["a"]]["y"] - joints[b["b"]]["y"]) < 1e-6
    ]
    for bid in dead:
        del borders[bid]
    used_joints = {b["a"] for b in borders.values()} | {b["b"] for b in borders.values()}
    joints = {jid: joint for jid, joint in joints.items() if jid in used_joints}

    shared = sum(1 for n in edge_count.values() if n >= 2)
    # Rounding can land a joint a fraction past the frame; grow the frame to match.
    if joints:
        width = max(width, max(j["x"] for j in joints.values()))
        height = max(height, max(j["y"] for j in joints.values()))
    return {
        "doc": {"width": width, "height": height, "joints": joints, "borders": borders, "labels": labels},
        "stats": {
            "territories": len(labels),
            "joints": len(joints),
            "borders": len(borders),
            "shared_borders": shared,
            "source_paths": len(territories),
            "tolerance": tol,
        },
    }


def import_territory_svg(svg_text: str, tolerance: float = DEFAULT_TOLERANCE) -> dict[str, Any]:
    """Convert closed territory shapes into a shared-border builder document.

    Returns ``{ok, doc, warnings, stats}``. ``doc`` is the map-builder payload
    (width, height, joints, borders, labels). Raises ``MapBuilderError`` on input
    that cannot become a map.
    """
    if not isinstance(svg_text, str) or not svg_text.strip():
        raise MapBuilderError("svg is empty")
    if len(svg_text) > MAX_SVG_CHARS:
        raise MapBuilderError(f"svg is too large (max {MAX_SVG_CHARS} characters)")
    if isinstance(tolerance, bool) or not isinstance(tolerance, (int, float)):
        raise MapBuilderError("tolerance must be a number")
    tol = float(tolerance)
    if not math.isfinite(tol) or not (MIN_TOLERANCE <= tol <= MAX_TOLERANCE):
        raise MapBuilderError(f"tolerance must be between {MIN_TOLERANCE:g} and {MAX_TOLERANCE:g}")
    text = svg_text.lstrip("\ufeff").strip()
    try:
        root = ET.fromstring(text)
    except ET.ParseError as e:
        raise MapBuilderError(f"SVG is not valid XML: {e}") from e
    if _local(root.tag) != "svg":
        raise MapBuilderError("file is not an SVG document")

    territories, warnings = _collect_territories(root)
    if not territories:
        raise MapBuilderError("SVG has no closed territory paths with an id or inkscape:label")

    box = _viewbox(root)
    all_pts = [p for terr in territories for ring in terr["rings"] for p in ring]
    if box is None:
        xs = [p[0] for p in all_pts]
        ys = [p[1] for p in all_pts]
        minx, miny = min(xs), min(ys)
        width, height = max(xs) - minx, max(ys) - miny
        warnings.append("SVG has no viewBox; the frame is the path bounds")
        for terr in territories:
            terr["rings"] = [[(p[0] - minx, p[1] - miny) for p in ring] for ring in terr["rings"]]
    else:
        vx, vy, width, height = box
        if abs(vx) > 1e-9 or abs(vy) > 1e-9:
            for terr in territories:
                terr["rings"] = [[(p[0] - vx, p[1] - vy) for p in ring] for ring in terr["rings"]]

    built = _build_doc(territories, tol, width, height, warnings)
    if built["stats"]["source_paths"] > 1 and built["stats"]["shared_borders"] == 0:
        warnings.append(
            "no borders were shared — neighbouring paths may sit further apart than the snap tolerance"
        )
    return {
        "ok": True,
        "doc": built["doc"],
        "warnings": warnings[:MAX_WARNINGS],
        "stats": built["stats"],
    }
