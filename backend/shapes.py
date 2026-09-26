"""
Turn the AI's drawing instructions into pen strokes.

Instead of listing every point, the AI describes shapes (lines, circles, arcs,
curves...) which we convert to points here, so curves come out smooth and exact.
It can also ask us to MIRROR the person's real strokes for symmetric drawings,
and afterwards we SNAP loose ends onto the person's lines so nothing almost-touches.
"""
import math

Point = list[float]
Stroke = list[Point]

STEP = 12  # pixels between points along curves


def _pt(p) -> Point:
    return [float(p[0]), float(p[1])]


def _n_steps(length: float) -> int:
    return max(8, min(200, int(length / STEP)))


# ---------- summary of the person's strokes (sent to the AI) ----------

def summarize_strokes(user_strokes: list[Stroke], max_strokes=60, max_points=12) -> str:
    """Compact text list of the person's strokes with exact coordinates."""
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
        lines.append(
            f"S{i}: start ({round(s[0][0])},{round(s[0][1])}) end ({round(s[-1][0])},{round(s[-1][1])}) "
            f"bbox [{round(min(xs))},{round(min(ys))},{round(max(xs))},{round(max(ys))}] path {path_txt}"
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


def _flip(stroke: Stroke, axis: str, at: float) -> Stroke:
    if axis == "horizontal":
        return [[p[0], 2 * at - p[1]] for p in stroke]
    return [[2 * at - p[0], p[1]] for p in stroke]


def _refine_axis(chosen: list[Stroke], all_strokes: list[Stroke], axis: str, guess: float) -> float:
    """The AI's mirror line is approximate. Try nearby lines and keep the one where the
    flipped strokes' ends land on the person's real line ends/corners (halves meet cleanly)."""
    anchors = [p for s in all_strokes if s for p in simplify(s)]
    ends = [p for s in chosen if s for p in (s[0], s[-1])]
    if not anchors or not ends:
        return guess
    k = 1 if axis == "horizontal" else 0

    def cost(at):
        total = 0.0
        for p in ends:
            q = list(p)
            q[k] = 2 * at - q[k]
            total += min(min(math.dist(q, a) for a in anchors), 30)
        return total + abs(at - guess) * 0.05  # tiny preference for the AI's guess

    best = min((guess + d for d in range(-60, 61)), key=cost)
    fine = min((best + d / 4 for d in range(-4, 5)), key=cost)
    return fine


def _straddles(stroke: Stroke, axis: str, at: float, margin=15) -> bool:
    """True if a stroke sits across the mirror line (like an eye or nose in the middle)."""
    k = 1 if axis == "horizontal" else 0
    vals = [p[k] for p in stroke]
    return min(vals) < at - margin and max(vals) > at + margin


def mirror(user_strokes: list[Stroke], ids, axis: str, at: float, log=None) -> list[Stroke]:
    if ids == "all" or ids is None:
        chosen = list(user_strokes)
    else:
        chosen = []
        for i in ids:
            try:
                idx = int(str(i).strip().lstrip("Ss"))
            except ValueError:
                continue
            if 0 <= idx < len(user_strokes):
                chosen.append(user_strokes[idx])
    chosen = [s for s in chosen if len(s) >= 2]
    guess = at
    at = _refine_axis(chosen, user_strokes, axis, at)
    # Strokes crossing the middle are already symmetric-ish; flipping them makes a double image.
    kept = [s for s in chosen if not _straddles(s, axis, at)]
    if log is not None:
        line = "x" if axis != "horizontal" else "y"
        note = (f"Mirrored {len(kept)} of your strokes across {line} = {at:.0f}"
                + (f" (the AI guessed {guess:.0f}; adjusted so the halves meet)" if abs(at - guess) >= 2 else ""))
        if len(kept) < len(chosen):
            note += f". Skipped {len(chosen) - len(kept)} stroke(s) sitting on the centre line"
        log.append(note + ".")
    return [_flip(s, axis, at) for s in kept]


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
        x, y, w, h = (float(shape[k]) for k in ("x", "y", "w", "h"))
        return [[[x, y], [x + w, y], [x + w, y + h], [x, y + h], [x, y]]]
    if kind == "circle":
        cx, cy = _pt(shape["center"])
        r = float(shape["r"])
        return [ellipse_arc(cx, cy, r, r, 0, 360)]
    if kind == "ellipse":
        cx, cy = _pt(shape["center"])
        return [ellipse_arc(cx, cy, float(shape["rx"]), float(shape["ry"]), 0, 360)]
    if kind == "arc":
        cx, cy = _pt(shape["center"])
        r = shape.get("r")
        rx = float(shape.get("rx", r))
        ry = float(shape.get("ry", r))
        return [ellipse_arc(cx, cy, rx, ry, float(shape["start"]), float(shape["end"]))]
    if kind == "curve":
        ctrl = [_pt(p) for p in shape["points"]]
        if len(ctrl) < 3:
            return [ctrl]
        return [bezier(ctrl)]
    if kind == "mirror":
        return mirror(user_strokes, shape.get("strokes", "all"),
                      str(shape.get("axis", "vertical")).lower(), float(shape["at"]), log)
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


def build_strokes(ai: dict, user_strokes: list[Stroke], log=None) -> list[Stroke]:
    """Everything the AI asked for, as strokes, with duplicates removed and ends snapped."""
    new, kinds, bad = [], {}, 0
    for shape in ai.get("shapes") or []:
        try:
            made = shape_to_strokes(shape, user_strokes, log)
        except (KeyError, TypeError, ValueError, IndexError):
            made = []
        if made:
            new.extend(made)
            k = str(shape.get("type", "shape")).lower()
            if k != "mirror":
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
    return snap_endpoints(new, user_strokes, log=log)