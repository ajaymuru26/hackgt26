"""
Turn an uploaded picture into pen strokes the robot can draw over it.

Steps: grayscale -> blur -> Canny edge detection -> follow each edge as a contour
-> drop the "there and back" half OpenCV adds on thin lines -> simplify -> keep the
longest lines, so the robot draws the outline of the picture instead of noise.
"""
import cv2
import numpy as np

DETAIL = {
    #          work scale, blur, min line length (board px), max strokes, simplify tolerance
    "low":    (0.45, 5, 40, 120, 2.0),
    "medium": (0.65, 3, 22, 260, 1.4),
    "high":   (0.9,  3, 10, 520, 1.0),
}


def _open_half(pts: np.ndarray) -> np.ndarray:
    """A contour of a 1-pixel line goes along it and comes back. Keep just one pass."""
    n = len(pts)
    if n < 6:
        return pts
    half = n // 2
    fwd = pts[1:half]
    back = pts[n - 1:n - half:-1][: len(fwd)]
    if len(back) == len(fwd) and np.mean(np.linalg.norm(fwd - back, axis=1)) < 2.5:
        return pts[: half + 1]
    return np.vstack([pts, pts[:1]])  # a genuinely closed outline: close the loop


def _thin(binary: np.ndarray, max_iter=60) -> np.ndarray:
    """Zhang-Suen thinning: shrink thick ink down to a 1-pixel centre line."""
    img = (binary > 0).astype(np.uint8)
    for _ in range(max_iter):
        changed = False
        for step in (0, 1):
            p = np.pad(img, 1)
            n = [p[:-2, 1:-1], p[:-2, 2:], p[1:-1, 2:], p[2:, 2:], p[2:, 1:-1], p[2:, :-2], p[1:-1, :-2], p[:-2, :-2]]
            b = sum(n)
            a = sum(((n[i] == 0) & (n[(i + 1) % 8] == 1)).astype(np.uint8) for i in range(8))
            if step == 0:
                c1, c2 = n[0] * n[2] * n[4], n[2] * n[4] * n[6]
            else:
                c1, c2 = n[0] * n[2] * n[6], n[0] * n[4] * n[6]
            remove = (img == 1) & (b >= 2) & (b <= 6) & (a == 1) & (c1 == 0) & (c2 == 0)
            if remove.any():
                img[remove] = 0
                changed = True
        if not changed:
            break
    return img * 255


def trace(image_bytes: bytes, x: float, y: float, w: float, h: float, detail: str = "medium"):
    """Returns (strokes in board coords, info dict)."""
    scale, blur, min_len, max_strokes, eps = DETAIL.get(detail, DETAIL["medium"])
    data = np.frombuffer(image_bytes, np.uint8)
    img = cv2.imdecode(data, cv2.IMREAD_UNCHANGED)
    if img is None:
        raise ValueError("That file isn't an image I can read.")
    if img.ndim == 3 and img.shape[2] == 4:  # transparent PNG: put it on white
        alpha = img[:, :, 3:4] / 255.0
        img = (img[:, :, :3] * alpha + 255 * (1 - alpha)).astype(np.uint8)
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img

    work_w, work_h = max(8, int(w * scale)), max(8, int(h * scale))
    gray = cv2.resize(gray, (work_w, work_h), interpolation=cv2.INTER_AREA)
    gray = cv2.GaussianBlur(gray, (blur, blur), 0)

    otsu, ink = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    dark_share = float((ink > 0).mean())
    line_art = dark_share < 0.22 and float(np.median(gray)) > 170
    if line_art:
        # drawings/logos on a light background: trace down the middle of each line (drawn once)
        method = "centre lines"
        ink = cv2.morphologyEx(ink, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8))
        skeleton = _thin(ink)
        # solid filled areas (like eyes) would thin away to a dot, so outline them instead
        dist = cv2.distanceTransform(ink, cv2.DIST_L2, 3)
        line_half = float(np.median(dist[skeleton > 0])) if (skeleton > 0).any() else 1.0
        core = (dist > max(3.0, line_half * 2.2)).astype(np.uint8)
        blobs = cv2.dilate(core, np.ones((3, 3), np.uint8), iterations=int(max(1, line_half * 2.2))) & (ink > 0)
        blob_edges = cv2.morphologyEx(blobs.astype(np.uint8) * 255, cv2.MORPH_GRADIENT, np.ones((3, 3), np.uint8))
        skeleton[blobs > 0] = 0
        edges = cv2.bitwise_or(skeleton.astype(np.uint8), _thin(blob_edges).astype(np.uint8))
        high = otsu
    else:
        # photos: trace the edges between light and dark areas
        method = "edges"
        high = max(40.0, otsu)
        edges = cv2.Canny(gray, high * 0.5, high)

    contours, _ = cv2.findContours(edges, cv2.RETR_LIST, cv2.CHAIN_APPROX_NONE)
    sx, sy = w / work_w, h / work_h
    lines = []
    for c in contours:
        pts = _open_half(c[:, 0, :].astype(np.float32))
        simple = cv2.approxPolyDP(pts.reshape(-1, 1, 2), eps, False)[:, 0, :]
        board = [[round(x + px * sx, 1), round(y + py * sy, 1)] for px, py in simple]
        length = sum(np.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(board, board[1:]))
        if len(board) >= 2 and length >= min_len:
            lines.append((length, board))

    lines.sort(key=lambda t: -t[0])
    kept = [b for _, b in lines[:max_strokes]]
    return kept, {"found": len(lines), "kept": len(kept), "threshold": round(float(high)), "method": method}