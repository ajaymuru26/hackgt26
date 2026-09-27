"""
Turn the AI's drawing instructions into pen strokes.

Instead of listing every point, the AI describes shapes (lines, circles, arcs,
curves...) which we convert to points here, so curves come out smooth and exact.
It can also ask us to MIRROR the person's real strokes for symmetric drawings,
and afterwards we SNAP loose ends onto the person's lines so nothing almost-touches.
"""
import math
import re
from contextvars import ContextVar

Point = list[float]
Stroke = list[Point]

STEP = 12  # pixels between points along curves


_ANCHORS: ContextVar[dict] = ContextVar("anchors", default={})
_BOXES: ContextVar[dict] = ContextVar("boxes", default={})
_HIGHLIGHT: ContextVar[list] = ContextVar("highlight", default=[])
_BOARD: ContextVar[tuple] = ContextVar("board", default=(1200, 700))


def _pt(p) -> Point:
    """A point is [x, y], an anchor like "K7", or a corner of the measured box like "TL".

    x and y may also be expressions of the measured box, such as "T-0.5*W".
    """
    if isinstance(p, str):
        key = p.strip().replace(" ", "")
        anchors = _ANCHORS.get()
        if key in anchors:
            return list(anchors[key])
        corners = _BOXES.get().get("_corners", {})
        if key in corners:
            return list(corners[key])
        m = re.fullmatch(r"[Ss](\d+)", key)  # bare "S2" -> its centre
        if m and f"S{m.group(1)}.center" in anchors:
            return list(anchors[f"S{m.group(1)}.center"])
        raise KeyError(f"unknown anchor {p}")
    return [_measure(p[0]), _measure(p[1])]


def _measure(value) -> float:
    """A number, or an expression using the measured box: L, T, R, B, W, H, CX, CY, S0.W, ..."""
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(" ", "")
    if not text:
        raise ValueError("empty measure")
    env = _BOXES.get().get("_env", {})
    return _eval_measure(text, env)


def _eval_measure(text: str, env: dict) -> float:
    """+ - * / and parentheses only, so a placement is arithmetic on the real coordinates."""
    i = 0

    def peek():
        return text[i] if i < len(text) else ""

    def parse_add():
        nonlocal i
        v = parse_mul()
        while i < len(text) and text[i] in "+-":
            op = text[i]
            i += 1
            r = parse_mul()
            v = v + r if op == "+" else v - r
        return v

    def parse_mul():
        nonlocal i
        v = parse_unary()
        while i < len(text) and text[i] in "*/":
            op = text[i]
            i += 1
            r = parse_unary()
            if op == "/" and r == 0:
                raise ValueError("divide by zero")
            v = v * r if op == "*" else v / r
        return v

    def parse_unary():
        nonlocal i
        if peek() == "-":
            i += 1
            return -parse_unary()
        if peek() == "+":
            i += 1
            return parse_unary()
        return parse_atom()

    def parse_atom():
        nonlocal i
        if peek() == "(":
            i += 1
            v = parse_add()
            if peek() != ")":
                raise ValueError(text)
            i += 1
            return v
        start = i
        if peek().isdigit() or peek() == ".":
            i += 1
            while i < len(text) and (text[i].isdigit() or text[i] == "."):
                i += 1
            return float(text[start:i])
        while i < len(text) and (text[i].isalnum() or text[i] in "._"):
            i += 1
        name = text[start:i]
        if name not in env:
            raise ValueError(f"unknown measure {name}")
        return float(env[name])

    value = parse_add()
    if i != len(text):
        raise ValueError(text)
    return value


def _stroke_box(stroke: Stroke) -> dict:
    xs, ys = [p[0] for p in stroke], [p[1] for p in stroke]
    L, T, R, B = min(xs), min(ys), max(xs), max(ys)
    return {"L": L, "T": T, "R": R, "B": B, "W": R - L, "H": B - T,
            "CX": (L + R) / 2, "CY": (T + B) / 2}


def measure_boxes(user_strokes: list[Stroke]) -> dict:
    """Exact frame of the ink, and of each stroke. Placements are computed from these."""
    boxes, env, corners = {}, {}, {}
    pts = [p for s in user_strokes for p in s]
    if pts:
        boxes["ALL"] = _stroke_box(pts)
        env.update(boxes["ALL"])
        b = boxes["ALL"]
        corners.update({
            "TL": [b["L"], b["T"]], "TR": [b["R"], b["T"]],
            "BL": [b["L"], b["B"]], "BR": [b["R"], b["B"]],
            "TC": [b["CX"], b["T"]], "BC": [b["CX"], b["B"]],
            "LC": [b["L"], b["CY"]], "RC": [b["R"], b["CY"]],
            "C": [b["CX"], b["CY"]],
        })
    for i, s in enumerate(user_strokes):
        if len(s) < 2:
            continue
        box = _stroke_box(s)
        boxes[f"S{i}"] = box
        for key, val in box.items():
            env[f"S{i}.{key}"] = val
    boxes["_env"] = env
    boxes["_corners"] = corners
    return boxes


def _target_box(shape: dict) -> dict:
    boxes = _BOXES.get()
    which = str(shape.get("on") or "ALL").strip()
    which = which.upper() if which.upper() == "ALL" else which[0].upper() + which[1:]
    if which.startswith("s"):
        which = "S" + which[1:]
    box = boxes.get(which) or boxes.get("ALL")
    if not box or "W" not in box or box["W"] < 1 or box["H"] < 1:
        raise ValueError("no measured box")
    return box


def anchor_points(user_strokes: list[Stroke], max_corners=90):
    """Name the useful points of the person's drawing so the AI can refer to them exactly.
    Returns (anchors dict, per-stroke list of corner names)."""
    anchors, per_stroke, k = {}, [], 0
    for i, s in enumerate(user_strokes):
        if not s:
            per_stroke.append([])
            continue
        xs, ys = [p[0] for p in s], [p[1] for p in s]
        anchors[f"S{i}.start"], anchors[f"S{i}.end"] = s[0], s[-1]
        anchors[f"S{i}.top"] = min(s, key=lambda p: p[1])
        anchors[f"S{i}.bottom"] = max(s, key=lambda p: p[1])
        anchors[f"S{i}.left"] = min(s, key=lambda p: p[0])
        anchors[f"S{i}.right"] = max(s, key=lambda p: p[0])
        anchors[f"S{i}.center"] = [(min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2]
        names = []
        for p in simplify(s):
            if k >= max_corners:
                break
            anchors[f"K{k}"] = p
            names.append(f"K{k}")
            k += 1
        per_stroke.append(names)
    return anchors, per_stroke


def _n_steps(length: float) -> int:
    return max(8, min(200, int(length / STEP)))


# ---------- summary of the person's strokes (sent to the AI) ----------

def summarize_strokes(user_strokes: list[Stroke], max_strokes=60, max_points=12) -> str:
    """Compact text list of the person's strokes with exact coordinates and named corners."""
    anchors, per_stroke = anchor_points(user_strokes)
    lines = []
    for i, s in enumerate(user_strokes[:max_strokes]):
        if not s:
            continue
        xs, ys = [p[0] for p in s], [p[1] for p in s]
        step = max(1, len(s) // max_points)
        path = s[::step]
        if path[-1] is not s[-1]:
            path = path + [s[-1]]
        path_txt = " ".join(f"({round(p[0])},{round(p[1])})" for p in path)
        corners = " ".join(f"{n}=({round(anchors[n][0])},{round(anchors[n][1])})" for n in per_stroke[i])
        lines.append(
            f"S{i}: start ({round(s[0][0])},{round(s[0][1])}) end ({round(s[-1][0])},{round(s[-1][1])}) "
            f"bbox [{round(min(xs))},{round(min(ys))},{round(max(xs))},{round(max(ys))}] corners {corners} path {path_txt}"
        )
    frame = measure_boxes(user_strokes).get("ALL")
    if frame:
        lines.append(
            "MEASURED FRAME (exact; place new parts from these, do not guess pixels): "
            f"L={frame['L']:.1f} T={frame['T']:.1f} R={frame['R']:.1f} B={frame['B']:.1f} "
            f"W={frame['W']:.1f} H={frame['H']:.1f} CX={frame['CX']:.1f} CY={frame['CY']:.1f}. "
            "Corners TL TR BL BR, edge midpoints TC BC LC RC, centre C. "
            "S0.L S0.T S0.W and so on are that stroke's own box."
        )
    if len(user_strokes) > max_strokes:
        lines.append(f"... and {len(user_strokes) - max_strokes} more strokes")
    return "\n".join(lines)


# ---------- shape primitives ----------

def ellipse_arc(cx, cy, rx, ry, start_deg, end_deg) -> Stroke:
    """Angles in screen degrees: 0 = right, 90 = down, 180 = left, 270 = up."""
    sweep = end_deg - start_deg
    length = abs(math.radians(sweep)) * (rx + ry) / 2
    n = _n_steps(length)
    pts = []
    for i in range(n + 1):
        t = math.radians(start_deg + sweep * i / n)
        pts.append([cx + rx * math.cos(t), cy + ry * math.sin(t)])
    return pts


def bezier(ctrl: list[Point]) -> Stroke:
    approx_len = sum(math.dist(a, b) for a, b in zip(ctrl, ctrl[1:]))
    n = _n_steps(approx_len)
    pts = []
    for i in range(n + 1):
        t = i / n
        pts_level = [list(p) for p in ctrl]
        while len(pts_level) > 1:  # de Casteljau
            pts_level = [[a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t]
                         for a, b in zip(pts_level, pts_level[1:])]
        pts.append(pts_level[0])
    return pts


def highlight_ids() -> list[int]:
    """Stroke indexes the last mirror marked, so the board can draw them in green."""
    return list(_HIGHLIGHT.get())


def mirror(user_strokes: list[Stroke], ids, axis: str, at: float, log=None) -> list[Stroke]:
    """Flip the chosen strokes and park the copy beside them.

    The fold is the right edge of the ink, not its middle. A left half of a face
    is copied onto the right of that edge, so the missing side sits beside it.
    """
    horizontal = str(axis).lower() == "horizontal"
    if isinstance(ids, (int, float)):
        ids = [ids]
    elif isinstance(ids, str):
        ids = "all" if ids.strip().lower() == "all" else re.findall(r"\d+", ids)
    if ids == "all" or ids is None or ids == []:
        chosen_i = list(range(len(user_strokes)))
    else:
        chosen_i = []
        for i in ids:
            found = re.findall(r"\d+", str(i))
            if not found:
                continue
            idx = int(found[0])
            if 0 <= idx < len(user_strokes) and idx not in chosen_i:
                chosen_i.append(idx)
        if not chosen_i:
            chosen_i = list(range(len(user_strokes)))
    chosen_i = [i for i in chosen_i if len(user_strokes[i]) >= 2]
    chosen = [user_strokes[i] for i in chosen_i]
    if not chosen:
        return []
    _HIGHLIGHT.get().extend(chosen_i)

    xs = [p[0] for s in chosen for p in s]
    ys = [p[1] for s in chosen for p in s]
    left, right = min(xs), max(xs)
    top, bottom = min(ys), max(ys)
    board_w, board_h = _BOARD.get()
    gap, margin = 48.0, 8.0

    if horizontal:
        # The bottom of the selection maps to the far side of the copy.
        far = 2 * top - bottom - gap
        if far >= margin:
            def flip_y(y, g=gap):
                return 2 * top - y - g
            where = f"above it, from the bottom y={bottom:.0f} up to y={far:.0f}"
        else:
            far = 2 * bottom - top + gap
            if far > board_h - margin:
                gap = max(24.0, gap - (far - (board_h - margin)))
                far = 2 * bottom - top + gap

            def flip_y(y, g=gap):
                return 2 * bottom - y + g
            where = f"below it (no room above), from y={top:.0f} down to y={far:.0f}"
        out = [[[p[0], flip_y(p[1])] for p in s] for s in chosen]
    else:
        # Fold at the right edge. The left side they drew stays put, and the
        # copy (the other side) starts just to the right of that edge.
        far = 2 * right - left + gap
        if far > board_w - margin:
            gap = (board_w - margin) - (2 * right - left)
        if gap >= 16:
            far = 2 * right - left + gap

            def flip_x(x, g=gap):
                return 2 * right - x + g
            where = f"to its right, from the right edge x={right:.0f} out to x={far:.0f}"
        else:
            gap = 48.0
            far = 2 * left - right - gap
            if far < margin:
                gap = max(16.0, left - margin - (right - left))
                far = 2 * left - right - gap

            def flip_x(x, g=gap):
                return 2 * left - x - g
            where = f"to its left (no room on the right), from x={right:.0f} to x={far:.0f}"
        out = [[[flip_x(p[0]), p[1]] for p in s] for s in chosen]

    if log is not None:
        log.append(
            f"Highlighted {len(chosen)} stroke(s) in green, then reflected them {where}. "
            "The copy sits beside the original, not across the middle, so both sides stay visible."
        )
    return out


def shape_to_strokes(shape: dict, user_strokes: list[Stroke], log=None) -> list[Stroke]:
    kind = str(shape.get("type", "")).lower()
    if kind == "line":
        return [[_pt(shape["from"]), _pt(shape["to"])]]
    if kind == "polyline":
        pts = [_pt(p) for p in shape["points"]]
        if shape.get("closed") and pts:
            pts.append(pts[0])
        return [pts]
    if kind == "rect":
        x, y = _measure(shape["x"]), _measure(shape["y"])
        w, h = _measure(shape["w"]), _measure(shape["h"])
        return [[[x, y], [x + w, y], [x + w, y + h], [x, y + h], [x, y]]]
    if kind == "roof":
        b = _target_box(shape)
        peak = [b["CX"], b["T"] - 0.5 * b["W"]]
        if log is not None:
            log.append(f"Roof from the measured top edge ({b['L']:.0f},{b['T']:.0f})–({b['R']:.0f},{b['T']:.0f}), "
                       f"peak at ({peak[0]:.0f},{peak[1]:.0f}), which is half the width above the top.")
        return [[[b["L"], b["T"]], peak, [b["R"], b["T"]]]]
    if kind == "door":
        b = _target_box(shape)
        w, h = b["W"] / 3, b["H"] / 2
        x, y = b["CX"] - w / 2, b["B"] - h
        if log is not None:
            log.append(f"Door width W/3 and height H/2, centred, bottom flush with y={b['B']:.0f}.")
        return [[[x, y], [x + w, y], [x + w, y + h], [x, y + h]]]
    if kind == "window":
        b = _target_box(shape)
        side = b["W"] / 5
        x, y = b["CX"] - side / 2, b["T"] + b["H"] * 0.22
        if log is not None:
            log.append(f"Window side W/5, centred in the upper part of the measured box.")
        return [[[x, y], [x + side, y], [x + side, y + side], [x, y + side], [x, y]]]
    if kind == "circle":
        cx, cy = _pt(shape["center"])
        r = _measure(shape["r"])
        return [ellipse_arc(cx, cy, r, r, 0, 360)]
    if kind == "ellipse":
        cx, cy = _pt(shape["center"])
        return [ellipse_arc(cx, cy, _measure(shape["rx"]), _measure(shape["ry"]), 0, 360)]
    if kind == "arc":
        cx, cy = _pt(shape["center"])
        r = shape.get("r")
        rx = _measure(shape.get("rx", r))
        ry = _measure(shape.get("ry", r))
        return [ellipse_arc(cx, cy, rx, ry, _measure(shape["start"]), _measure(shape["end"]))]
    if kind == "curve":
        ctrl = [_pt(p) for p in shape["points"]]
        if len(ctrl) < 3:
            return [ctrl]
        return [bezier(ctrl)]
    if kind in ("mirror", "reflect", "reflection", "flip"):
        try:
            at = float(shape.get("at", 0))
        except (TypeError, ValueError):
            at = 0.0
        return mirror(user_strokes, shape.get("strokes", "all"),
                      str(shape.get("axis", "vertical")).lower(), at, log)
    if kind == "stroke" or ("points" in shape and not kind):
        return [[_pt(p) for p in shape["points"]]]
    return []


# ---------- clean-up ----------

def _near_existing(stroke: Stroke, user_points: list[Point], tol=12, fraction=0.8) -> bool:
    """True if most of the stroke already sits on the person's drawing (a duplicate)."""
    if not user_points:
        return False
    sample = stroke[:: max(1, len(stroke) // 20)]
    close = sum(1 for p in sample if min(math.dist(p, q) for q in user_points) < tol)
    return close >= fraction * len(sample)


def _closest_on_segment(p, a, b):
    ax, ay, bx, by = a[0], a[1], b[0], b[1]
    dx, dy = bx - ax, by - ay
    L2 = dx * dx + dy * dy
    if L2 == 0:
        return [ax, ay]
    t = max(0, min(1, ((p[0] - ax) * dx + (p[1] - ay) * dy) / L2))
    return [ax + t * dx, ay + t * dy]


def simplify(points: Stroke, epsilon=6.0) -> Stroke:
    """Ramer-Douglas-Peucker: keeps the corners of a hand-drawn stroke, drops the jitter."""
    if len(points) < 3:
        return list(points)
    a, b = points[0], points[-1]
    far_i, far_d = 0, -1.0
    for i in range(1, len(points) - 1):
        d = math.dist(points[i], _closest_on_segment(points[i], a, b))
        if d > far_d:
            far_i, far_d = i, d
    if far_d <= epsilon:
        return [a, b]
    return simplify(points[: far_i + 1], epsilon)[:-1] + simplify(points[far_i:], epsilon)


def snap_endpoints(new_strokes: list[Stroke], user_strokes: list[Stroke],
                   endpoint_radius=20, line_radius=12, log=None) -> list[Stroke]:
    """Pull loose ends of new strokes onto the person's lines so they connect cleanly."""
    # anchors = line ends AND corners (like the top corners of a box drawn in one stroke)
    anchors = [p for s in user_strokes if s for p in simplify(s)]
    segments = [(a, b) for s in user_strokes for a, b in zip(s, s[1:])]

    def snap(p):
        best, best_d = None, endpoint_radius
        for a in anchors:  # 1) snap to the person's line ends / corners
            d = math.dist(p, a)
            if d < best_d:
                best, best_d = list(a), d
        if best:
            return best
        best_d = line_radius
        for a, b in segments:  # 2) otherwise land exactly on a nearby line
            c = _closest_on_segment(p, a, b)
            d = math.dist(p, c)
            if d < best_d:
                best, best_d = c, d
        return best or p

    out, moved = [], 0
    for s in new_strokes:
        if len(s) < 2 or math.dist(s[0], s[-1]) < 1:  # closed shapes (circles) stay as they are
            out.append(s)
            continue
        s = [list(p) for p in s]
        a, b = snap(s[0]), snap(s[-1])
        moved += (a != s[0]) + (b != s[-1])
        s[0], s[-1] = a, b
        out.append(s)
    if log is not None and moved:
        log.append(f"Snapped {moved} loose end(s) onto your lines so everything connects.")
    return out


# ---------- keeping new lines off the person's ink ----------

_CELL = 24  # spatial-hash cell for looking up nearby ink segments


def _ink_index(user_strokes):
    """The person's line segments, bucketed by grid cell so nearby ones are quick to find."""
    grid = {}
    for s in user_strokes:
        for a, b in zip(s, s[1:]):
            for cx in range(int(min(a[0], b[0]) // _CELL), int(max(a[0], b[0]) // _CELL) + 1):
                for cy in range(int(min(a[1], b[1]) // _CELL), int(max(a[1], b[1]) // _CELL) + 1):
                    grid.setdefault((cx, cy), []).append((a, b))
    return grid


def _nearby(grid, x1, y1, x2, y2, pad=0.0):
    seen = set()
    for cx in range(int((min(x1, x2) - pad) // _CELL), int((max(x1, x2) + pad) // _CELL) + 1):
        for cy in range(int((min(y1, y2) - pad) // _CELL), int((max(y1, y2) + pad) // _CELL) + 1):
            for seg in grid.get((cx, cy), ()):
                if id(seg) not in seen:
                    seen.add(id(seg))
                    yield seg


def _cross(p, q, a, b):
    """Where segment p-q crosses segment a-b, or None."""
    d = (q[0] - p[0]) * (b[1] - a[1]) - (q[1] - p[1]) * (b[0] - a[0])
    if abs(d) < 1e-9:
        return None
    t = ((a[0] - p[0]) * (b[1] - a[1]) - (a[1] - p[1]) * (b[0] - a[0])) / d
    u = ((a[0] - p[0]) * (q[1] - p[1]) - (a[1] - p[1]) * (q[0] - p[0])) / d
    if 0 <= t <= 1 and 0 <= u <= 1:
        return [p[0] + t * (q[0] - p[0]), p[1] + t * (q[1] - p[1])]
    return None


def _resample(stroke, step=3.0):
    out = [list(stroke[0])]
    for a, b in zip(stroke, stroke[1:]):
        n = max(1, int(math.dist(a, b) / step))
        out += [[a[0] + (b[0] - a[0]) * i / n, a[1] + (b[1] - a[1]) * i / n] for i in range(1, n + 1)]
    return out


def _on_ink(grid, p, touch):
    return any(math.dist(p, _closest_on_segment(p, a, b)) < touch
               for a, b in _nearby(grid, p[0], p[1], p[0], p[1], touch))


def ink_conflicts(new_strokes, user_strokes, touch=6.0, end_ok=18.0, min_run=20.0):
    """Where new lines cross the person's ink, or run along it, away from their own ends.

    A new line's ends are meant to join the drawing (they're snapped onto it), so contact
    within `end_ok` px of an end is fine. Returns (crossings, overlaps): lists of
    (stroke index, [x, y]) and (stroke index, [x, y] midpoint, length in px)."""
    grid = _ink_index(user_strokes)
    crossings, overlaps = [], []
    for i, s in enumerate(new_strokes):
        if len(s) < 2:
            continue
        ends = (s[0], s[-1]) if math.dist(s[0], s[-1]) >= 1 else ()  # closed shapes have no joining ends
        found = []
        for p, q in zip(s, s[1:]):
            for a, b in _nearby(grid, p[0], p[1], q[0], q[1]):
                c = _cross(p, q, a, b)
                if c and all(math.dist(c, e) >= end_ok for e in ends) and all(math.dist(c, f) >= 10 for f in found):
                    found.append(c)
        crossings += [(i, [round(c[0]), round(c[1])]) for c in found]
        run = []
        for p in _resample(s) + [None]:
            hit = p is not None and all(math.dist(p, e) >= end_ok for e in ends) and _on_ink(grid, p, touch)
            if hit:
                run.append(p)
            elif run:
                length = sum(math.dist(a, b) for a, b in zip(run, run[1:]))
                if length >= min_run:
                    mid = run[len(run) // 2]
                    overlaps.append((i, [round(mid[0]), round(mid[1])], round(length)))
                run = []
    return crossings, overlaps


def conflict_note(new_strokes, user_strokes) -> str:
    """A plain-language note for the AI's check pass, or "" if nothing crosses the ink."""
    crossings, overlaps = ink_conflicts(new_strokes, user_strokes)
    if not crossings and not overlaps:
        return ""
    lines = ["MEASURED PROBLEMS (exact, from the coordinates): your blue lines touch the person's black ink "
             "where they should not (not at a line's own ends, where joining is fine):"]
    if overlaps:
        lines.append("- Running along / over existing lines (the robot would trace over their ink): "
                     + "; ".join(f"about {n} px near ({x}, {y})" for _, (x, y), n in overlaps[:8]))
    if crossings:
        lines.append("- Cutting across existing lines at: "
                     + ", ".join(f"({x}, {y})" for _, (x, y) in crossings[:12]))
    lines.append("If these are mistakes (a shape placed on top of the drawing, a line through it), move or "
                 "resize those shapes so they sit beside the existing lines and only touch them at their ends. "
                 "Keep a crossing only if the drawing really needs it (like the points of a star).")
    return "\n".join(lines)


def lift_over_ink(new_strokes, user_strokes, touch=6.0, end_ok=18.0, min_run=20.0, log=None):
    """Last safety net: lift the pen wherever a new line would run along the person's ink.
    Single crossings are left alone (some drawings need them); only tracing over ink is cut."""
    _, overlaps = ink_conflicts(new_strokes, user_strokes, touch, end_ok, min_run)
    if not overlaps:
        return new_strokes
    grid = _ink_index(user_strokes)
    bad = {i for i, _, _ in overlaps}
    out, cut = [], 0
    for i, s in enumerate(new_strokes):
        if i not in bad:
            out.append(s)
            continue
        ends = (s[0], s[-1]) if math.dist(s[0], s[-1]) >= 1 else ()
        piece = []
        for p in _resample(s) + [None]:
            on = p is not None and all(math.dist(p, e) >= end_ok for e in ends) and _on_ink(grid, p, touch)
            if p is not None and not on:
                piece.append(p)
                continue
            if len(piece) >= 2 and sum(math.dist(a, b) for a, b in zip(piece, piece[1:])) >= 6:
                out.append([[round(x, 1), round(y, 1)] for x, y in simplify(piece, 1.0)])
            piece = []
        cut += 1
    if log is not None:
        log.append(f"Lifted the pen where {cut} new line(s) would have run over what you drew.")
    return out


def build_strokes(ai: dict, user_strokes: list[Stroke], log=None, board=(1200, 700)) -> list[Stroke]:
    """Everything the AI asked for, as strokes, with duplicates removed and ends snapped."""
    new, mirrored, kinds, bad = [], [], {}, 0
    _HIGHLIGHT.set([])
    _BOARD.set((float(board[0]), float(board[1])))
    _ANCHORS.set(anchor_points(user_strokes)[0])
    _BOXES.set(measure_boxes(user_strokes))
    for shape in ai.get("shapes") or []:
        if not isinstance(shape, dict):
            bad += 1
            continue
        try:
            made = shape_to_strokes(shape, user_strokes, log)
        except (KeyError, TypeError, ValueError, IndexError, AttributeError):
            made = []
        if made:
            k = str(shape.get("type", "shape")).lower()
            if k in ("mirror", "reflect", "reflection", "flip"):
                mirrored.extend(made)
            else:
                new.extend(made)
                kinds[k] = kinds.get(k, 0) + 1
        else:
            bad += 1
    if log is not None and kinds:
        parts = [f"{n} {k}{'s' if n > 1 else ''}" for k, n in kinds.items()]
        log.append("Turned the AI's shapes into smooth pen paths: " + ", ".join(parts) + ".")
    if log is not None and bad:
        log.append(f"Ignored {bad} shape(s) the AI described incorrectly.")
    for s in ai.get("strokes") or []:  # older raw-points format still works
        try:
            new.append([_pt(p) for p in s])
        except (TypeError, ValueError, IndexError):
            continue

    user_points = [p for s in user_strokes for p in s[:: max(1, len(s) // 40)]]
    before = len(new)
    new = [s for s in new if len(s) >= 2 and not _near_existing(s, user_points)]
    if log is not None and before - len(new):
        log.append(f"Removed {before - len(new)} line(s) that would trace over what you already drew.")
    # The reflection is a copy placed beside the ink. Don't drop it as a duplicate
    # and don't snap it back onto the original.
    return snap_endpoints(new, user_strokes, log=log) + mirrored