"""
Finish an unfinished drawing with OpenCV, not a language model.

The ink is measured as geometry. A curve that is most of a circle or ellipse
is completed, line ends that point at each other or stop just short of another
line are joined, and a rectangle that is missing a side gets that side.
"""
import math

import cv2
import numpy as np


def _as_array(stroke) -> np.ndarray:
    pts = [[float(p[0]), float(p[1])] for p in stroke if len(p) >= 2]
    return np.array(pts, np.float64)


def _length(pts: np.ndarray) -> float:
    if len(pts) < 2:
        return 0.0
    return float(np.hypot(np.diff(pts[:, 0]), np.diff(pts[:, 1])).sum())


def _unit(v) -> np.ndarray | None:
    n = float(np.hypot(v[0], v[1]))
    if n < 1e-6:
        return None
    return v / n


def _outward(pts: np.ndarray, at_start: bool, reach=18.0) -> np.ndarray | None:
    """Direction the pen would keep going if it ran off this end."""
    if len(pts) < 2:
        return None
    if at_start:
        walked, i = 0.0, 0
        while i < len(pts) - 1 and walked < reach:
            walked += float(np.hypot(*(pts[i + 1] - pts[i])))
            i += 1
        return _unit(pts[0] - pts[i])
    walked, i = 0.0, len(pts) - 1
    while i > 0 and walked < reach:
        walked += float(np.hypot(*(pts[i] - pts[i - 1])))
        i -= 1
    return _unit(pts[-1] - pts[i])


def _raster(strokes, width, height, thickness=5) -> np.ndarray:
    img = np.zeros((int(height), int(width)), np.uint8)
    for stroke in strokes:
        pts = np.array([[int(round(p[0])), int(round(p[1]))] for p in stroke if len(p) >= 2], np.int32)
        if len(pts) >= 2:
            cv2.polylines(img, [pts], False, 255, thickness, cv2.LINE_8)
    return img


def _ink_box(mask: np.ndarray):
    ys, xs = np.where(mask > 0)
    if len(xs) < 8:
        return None
    return int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max())


def _ellipse_arc(cx, cy, rx, ry, angle, a0, sweep) -> list:
    """Points for the missing part of a fitted ellipse. `a0` and `sweep` are radians."""
    rad = math.radians(angle)
    c, s = math.cos(rad), math.sin(rad)
    n = max(6, int(max(rx, ry) * abs(sweep) / 7))
    pts = []
    for i in range(n + 1):
        t = a0 + sweep * i / n
        xr, yr = rx * math.cos(t), ry * math.sin(t)
        pts.append([cx + xr * c - yr * s, cy + xr * s + yr * c])
    return _line(pts)


def _complete_ellipse(pts: np.ndarray, limit: float):
    """The missing arc when the stroke hugs an ellipse but is not a circle."""
    if len(pts) < 16:
        return None
    try:
        (cx, cy), (rw, rh), angle = cv2.fitEllipse(pts.astype(np.float32))
    except cv2.error:
        return None
    if min(rw, rh) < 20 or max(rw, rh) > limit * 2.2:
        return None
    if min(rw, rh) / max(rw, rh) < 0.35:
        return None
    rad = math.radians(float(angle))
    c, s = math.cos(rad), math.sin(rad)
    dx, dy = pts[:, 0] - cx, pts[:, 1] - cy
    xr = dx * c + dy * s
    yr = -dx * s + dy * c
    rx, ry = float(rw) / 2, float(rh) / 2
    if rx < 8 or ry < 8:
        return None
    if float(np.median(np.abs(np.hypot(xr / rx, yr / ry) - 1))) > 0.08:
        return None
    theta = np.arctan2(yr / ry, xr / rx)
    param = np.column_stack([np.cos(theta), np.sin(theta)])
    turn = _covered_turn(param, 0.0, 0.0)
    if turn is None:
        return None
    covered, a0, sweep = turn
    if not (math.radians(200) <= covered <= math.radians(352)):
        return None
    if not (math.radians(8) <= abs(sweep) <= math.radians(170)):
        return None
    return _ellipse_arc(cx, cy, rx, ry, float(angle), a0, sweep), covered, sweep


def _line(pts: np.ndarray) -> list:
    return [[round(float(x), 1), round(float(y), 1)] for x, y in pts]


def _interior_points(pts: np.ndarray, n=5) -> np.ndarray:
    """Points along the middle of a stroke. The ends are allowed to touch existing ink."""
    if len(pts) == 2:
        return np.linspace(pts[0], pts[1], n + 2)[1:-1]
    seg = np.hypot(np.diff(pts[:, 0]), np.diff(pts[:, 1]))
    total = float(seg.sum())
    if total < 1:
        return pts[len(pts) // 2:len(pts) // 2 + 1]
    cum = np.concatenate([[0.0], np.cumsum(seg)])
    marks = np.linspace(0.28 * total, 0.72 * total, n)
    out = []
    for mark in marks:
        i = int(np.searchsorted(cum, mark, side="right") - 1)
        i = min(max(i, 0), len(pts) - 2)
        span = cum[i + 1] - cum[i]
        t = 0.0 if span < 1e-6 else (mark - cum[i]) / span
        out.append(pts[i] * (1 - t) + pts[i + 1] * t)
    return np.array(out)


def _mostly_new(candidate, existing_mask, min_free=0.6) -> bool:
    """Keep a stroke when its middle is not already sitting on the ink."""
    pts = _as_array(candidate)
    if len(pts) < 2:
        return False
    h, w = existing_mask.shape
    free = 0
    sample = _interior_points(pts)
    for x, y in sample:
        xi, yi = int(round(x)), int(round(y))
        if xi < 0 or yi < 0 or xi >= w or yi >= h or existing_mask[yi, xi] == 0:
            free += 1
    return free / max(len(sample), 1) >= min_free


def _fit_circle(pts: np.ndarray):
    x, y = pts[:, 0], pts[:, 1]
    mat = np.c_[x, y, np.ones(len(pts))]
    try:
        d, e, f = np.linalg.lstsq(mat, -(x * x + y * y), rcond=None)[0]
    except np.linalg.LinAlgError:
        return None
    cx, cy = -d / 2, -e / 2
    r2 = cx * cx + cy * cy - f
    if r2 <= 1:
        return None
    r = math.sqrt(r2)
    err = np.abs(np.hypot(x - cx, y - cy) - r)
    return cx, cy, r, float(np.median(err))


def _covered_turn(pts: np.ndarray, cx, cy) -> tuple[float, float, float] | None:
    """Signed sweep already drawn, the end angle, and the sweep still missing.

    The missing sweep continues in the same direction the pen was moving.
    """
    ang = np.arctan2(pts[:, 1] - cy, pts[:, 0] - cx)
    unwrapped = [float(ang[0])]
    for a in ang[1:]:
        da = float(a) - unwrapped[-1]
        while da > math.pi:
            da -= 2 * math.pi
        while da < -math.pi:
            da += 2 * math.pi
        unwrapped.append(unwrapped[-1] + da)
    delta = unwrapped[-1] - unwrapped[0]
    covered = abs(delta)
    if covered < 0.2:
        return None
    missing = 2 * math.pi - covered
    sweep = math.copysign(missing, delta)
    return covered, unwrapped[-1], sweep


def _arc(cx, cy, r, a0, sweep, step=7.0) -> list:
    n = max(4, int(abs(r * sweep) / step))
    pts = []
    for i in range(n + 1):
        a = a0 + sweep * i / n
        pts.append([cx + r * math.cos(a), cy + r * math.sin(a)])
    return _line(pts)


def _complete_curves(strokes, width, height) -> tuple[list, list, set]:
    """Finish a circle that is already more than half drawn."""
    added, notes, done = [], [], set()
    limit = 0.48 * min(width, height)
    for stroke in strokes:
        pts = _as_array(stroke)
        if len(pts) < 12 or _length(pts) < 40:
            continue
        if float(np.hypot(*(pts[0] - pts[-1]))) < 8:
            continue
        fit = _fit_circle(pts)
        used_circle = False
        if fit is not None:
            cx, cy, r, err = fit
            if 16 <= r <= limit and err <= max(5.0, 0.075 * r):
                turn = _covered_turn(pts, cx, cy)
                if turn is not None:
                    covered, a0, sweep = turn
                    # More than half a circle, but not already closed. A shallow arc is left alone.
                    if math.radians(200) <= covered <= math.radians(352) and math.radians(8) <= abs(sweep) <= math.radians(170):
                        added.append(_arc(cx, cy, r, a0, sweep))
                        done.add(id(stroke))
                        notes.append(
                            f"A curve covers {math.degrees(covered):.0f}° of a circle with radius {r:.0f} px. "
                            f"Drew the missing {math.degrees(abs(sweep)):.0f}°."
                        )
                        used_circle = True
        if used_circle:
            continue
        oval = _complete_ellipse(pts, limit)
        if oval is None:
            continue
        arc, covered, sweep = oval
        added.append(arc)
        done.add(id(stroke))
        notes.append(
            f"A curve covers {math.degrees(covered):.0f}° of an ellipse. "
            f"Drew the missing {math.degrees(abs(sweep)):.0f}°."
        )
    return added, notes, done


def _segments(strokes) -> list[tuple[np.ndarray, np.ndarray]]:
    segs = []
    for stroke in strokes:
        pts = _as_array(stroke)
        if len(pts) < 2:
            continue
        # Collapse dense pen points so a segment is a real straight run.
        simple = cv2.approxPolyDP(pts.reshape(-1, 1, 2).astype(np.float32), 3.5, False)[:, 0, :].astype(np.float64)
        for a, b in zip(simple, simple[1:]):
            if float(np.hypot(*(b - a))) >= 10:
                segs.append((a, b))
    return segs


def _ray_hit(origin, direction, segs, own_tip, max_dist):
    best, best_t = None, max_dist
    for a, b in segs:
        if float(np.hypot(*(a - own_tip))) < 6 or float(np.hypot(*(b - own_tip))) < 6:
            continue
        edge = b - a
        mat = np.array([direction, -edge], np.float64).T
        if abs(np.linalg.det(mat)) < 1e-6:
            continue
        try:
            t, u = np.linalg.solve(mat, a - origin)
        except np.linalg.LinAlgError:
            continue
        if 8 < t < best_t and -0.02 <= u <= 1.02:
            best_t = float(t)
            best = origin + t * direction
    return None if best is None else (best, best_t)


def _join_ends(strokes, width, height, skip_strokes=()) -> tuple[list, list]:
    """Close gaps: ends that aim at each other, and ends that stop just short of another line."""
    tips = []
    for si, stroke in enumerate(strokes):
        pts = _as_array(stroke)
        if len(pts) < 2 or _length(pts) < 12:
            continue
        if id(stroke) in skip_strokes:
            continue
        for at_start in (True, False):
            direction = _outward(pts, at_start)
            if direction is None:
                continue
            tips.append({
                "p": pts[0] if at_start else pts[-1],
                "d": direction,
                "len": _length(pts),
                "i": len(tips),
                "stroke": si,
            })
    if len(tips) < 2:
        return [], []
    diag = math.hypot(width, height)
    max_gap = min(110.0, max(36.0, 0.09 * diag))
    used = set()
    added = []
    bridges = corners = extensions = 0

    pairs = []
    for i, a in enumerate(tips):
        for b in tips[i + 1:]:
            if a["stroke"] == b["stroke"] and a["len"] < 30:
                continue
            gap = b["p"] - a["p"]
            dist = float(np.hypot(*gap))
            if dist < 6 or dist > max_gap:
                continue
            g = gap / dist
            face = float(np.dot(a["d"], g))
            back = float(np.dot(b["d"], -g))
            pairs.append((dist, face, back, a, b))
    pairs.sort(key=lambda item: item[0])
    for dist, face, back, a, b in pairs:
        if a["i"] in used or b["i"] in used:
            continue
        if face > 0.72 and back > 0.72:
            added.append(_line([a["p"], b["p"]]))
            used.add(a["i"])
            used.add(b["i"])
            bridges += 1

    corners_found = []
    for i, a in enumerate(tips):
        if a["i"] in used:
            continue
        for b in tips[i + 1:]:
            if b["i"] in used or a["stroke"] == b["stroke"]:
                continue
            mat = np.array([a["d"], -b["d"]], np.float64).T
            if abs(np.linalg.det(mat)) < 0.25:
                continue
            try:
                t, s = np.linalg.solve(mat, b["p"] - a["p"])
            except np.linalg.LinAlgError:
                continue
            if not (6 < t < max_gap and 6 < s < max_gap):
                continue
            hit = a["p"] + t * a["d"]
            if not (0 <= hit[0] <= width and 0 <= hit[1] <= height):
                continue
            score = float(t + s)
            corners_found.append((score, a, b, hit))
    corners_found.sort(key=lambda item: item[0])
    for _, a, b, hit in corners_found:
        if a["i"] in used or b["i"] in used:
            continue
        added.append(_line([a["p"], hit]))
        added.append(_line([b["p"], hit]))
        used.add(a["i"])
        used.add(b["i"])
        corners += 1

    segs = _segments(strokes)
    for tip in tips:
        if tip["i"] in used:
            continue
        reach = min(max_gap, max(28.0, 0.22 * tip["len"]))
        hit = _ray_hit(tip["p"], tip["d"], segs, tip["p"], reach)
        if hit is None:
            continue
        point, _ = hit
        added.append(_line([tip["p"], point]))
        used.add(tip["i"])
        extensions += 1

    notes = []
    if bridges:
        notes.append(f"Joined {bridges} gap(s) where two line ends pointed at each other.")
    if corners:
        notes.append(f"Extended {corners} corner(s) out to where the lines would meet.")
    if extensions:
        notes.append(f"Extended {extensions} line end(s) until they met another line.")
    return added, notes


def _complete_rect(strokes) -> tuple[list, list]:
    """If the outline is a rectangle with a side missing, draw that side."""
    pts = np.vstack([_as_array(s) for s in strokes if len(s) >= 2])
    if len(pts) < 4:
        return [], []
    hull = cv2.convexHull(pts.astype(np.float32))
    rect = cv2.minAreaRect(pts.astype(np.float32))
    (_, _), (rw, rh), _ = rect
    if rw < 24 or rh < 24:
        return [], []
    rect_area = float(rw * rh)
    hull_area = float(cv2.contourArea(hull))
    if rect_area <= 1 or hull_area / rect_area < 0.9:
        return [], []
    pad = 30
    shifted = [_line(_as_array(s) + pad) for s in strokes if len(s) >= 2]
    box = cv2.boxPoints(rect) + pad
    far = int(max(np.max(box[:, 0]), np.max(box[:, 1])) + pad)
    mask = _raster(shifted, far, far, thickness=7)
    # Distance from every pixel to the nearest ink, so each side can be scored.
    dist = cv2.distanceTransform(cv2.bitwise_not(mask), cv2.DIST_L2, 3)
    h, w = dist.shape
    tol = max(14.0, 0.035 * min(rw, rh))
    present, missing = [], []
    for i in range(4):
        a, b = box[i], box[(i + 1) % 4]
        samples = max(8, int(np.hypot(*(b - a)) / 6))
        near = 0
        total = 0
        # Score the middle of the side. Corners touch the neighbouring sides even when this one is missing.
        for t in np.linspace(0.28, 0.72, samples):
            x, y = a * (1 - t) + b * t
            xi, yi = int(round(x)), int(round(y))
            if xi < 0 or yi < 0 or xi >= w or yi >= h:
                continue
            total += 1
            if dist[yi, xi] <= tol:
                near += 1
        if total < 4:
            continue
        share = near / total
        edge = (a, b, share)
        if share >= 0.55:
            present.append(edge)
        elif share <= 0.22:
            missing.append(edge)
    if len(present) < 2 or not missing or len(missing) > 2:
        return [], []
    added = []
    for a, b, _ in missing:
        added.append(_line([a - pad, b - pad]))
    note = (
        f"The outline fills {hull_area / rect_area:.0%} of its bounding rectangle "
        f"({rw:.0f} x {rh:.0f} px) but {len(missing)} side(s) are open. Drew the missing side(s)."
    )
    return added, [note]


def finish(strokes, width: int, height: int):
    """Return (new strokes, notes). Notes are what OpenCV measured, one line each."""
    notes = []
    usable = [s for s in strokes if len(s) >= 2]
    if not usable:
        return [], ["There aren't enough lines for computer vision to finish."]
    mask = _raster(usable, width, height)
    box = _ink_box(mask)
    if box is None:
        return [], ["OpenCV found no ink."]
    left, top, right, bottom = box
    notes.append(
        f"OpenCV measured the ink from ({left}, {top}) to ({right}, {bottom}), "
        f"{right - left} px wide and {bottom - top} px tall."
    )

    curves, curve_notes, curved = _complete_curves(usable, width, height)
    joins, join_notes = _join_ends(usable, width, height, curved)
    sides, side_notes = _complete_rect(usable)

    occupied = mask.copy()

    def take(cands):
        kept = []
        for stroke in cands:
            if not _mostly_new(stroke, occupied):
                continue
            kept.append(stroke)
            pts = np.array([[int(round(p[0])), int(round(p[1]))] for p in stroke], np.int32)
            if len(pts) >= 2:
                cv2.polylines(occupied, [pts], False, 255, 7)
        return kept

    new = []
    kept_curves = take(curves)
    kept_joins = take(joins)
    kept_sides = take(sides)
    new.extend(kept_curves + kept_joins + kept_sides)
    if kept_curves:
        notes.extend(curve_notes[:4] if len(kept_curves) == len(curves) else [
            f"Completed {len(kept_curves)} partial circle(s)."
        ])
    if kept_joins:
        notes.extend(join_notes)
    if kept_sides:
        notes.extend(side_notes)
    if not new:
        notes.append(
            "No partial circle, open gap, or missing rectangle side was clear enough to finish. "
            "Draw a bit more of the outline."
        )
    return new, notes
