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


def _decode(image_bytes: bytes):
    data = np.frombuffer(image_bytes, np.uint8)
    img = cv2.imdecode(data, cv2.IMREAD_UNCHANGED)
    if img is None:
        raise ValueError("That file isn't an image I can read.")
    if img.ndim == 3 and img.shape[2] == 4:  # transparent PNG: put it on white
        alpha = img[:, :, 3:4] / 255.0
        img = (img[:, :, :3] * alpha + 255 * (1 - alpha)).astype(np.uint8)
    if img.ndim == 2:
        img = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
    return img


def _drop_thick(ink: np.ndarray, max_radius: float = 18) -> np.ndarray:
    """A marker stroke is thin. A shadow, a person, or a piece of furniture is not."""
    binary = (ink > 0).astype(np.uint8)
    if not binary.any():
        return ink
    dist = cv2.distanceTransform(binary, cv2.DIST_L2, 3)
    count, labels, stats, _ = cv2.connectedComponentsWithStats(binary)
    keep = np.zeros_like(ink)
    for i in range(1, count):
        comp = labels == i
        radius = float(dist[comp].max())
        if radius <= max_radius:
            keep[comp] = 255
    return keep


def _marker_mask(bgr: np.ndarray) -> np.ndarray:
    """Dry-erase ink. Works when the board is dim, and ignores the rest of the photo."""
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    blur = cv2.GaussianBlur(gray, (3, 3), 0)
    # Darker than the board next to it, so a shadow across the whole photo is not ink.
    block = 41 if min(blur.shape) > 41 else max(3, (min(blur.shape) // 2) * 2 + 1)
    ink = cv2.adaptiveThreshold(blur, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY_INV, block, 8)
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    sat, val = hsv[:, :, 1], hsv[:, :, 2]
    # A gray edge between the wall and the board is not marker. Real ink is dark or colored.
    ink[(gray > 110) & (sat < 40)] = 0
    color = ((sat > 40) & (val > 25) & (val < 235)).astype(np.uint8) * 255
    ink = cv2.bitwise_or(ink, color)
    ink = _drop_thick(ink)
    ink = cv2.morphologyEx(ink, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8))
    return ink


def _strokes_from_mask(mask, x, y, w, h, min_len, eps, max_strokes):
    contours, _ = cv2.findContours(mask, cv2.RETR_LIST, cv2.CHAIN_APPROX_NONE)
    work_h, work_w = mask.shape[:2]
    sx, sy = w / work_w, h / work_h
    lines = []
    for c in contours:
        pts = _open_half(c[:, 0, :].astype(np.float32))
        simple = cv2.approxPolyDP(pts.reshape(-1, 1, 2), eps, False)[:, 0, :]
        board = [[round(x + px * sx, 1), round(y + py * sy, 1)] for px, py in simple]
        if len(board) < 2:
            continue
        # The edge of the photo is not marker.
        edge = sum(
            1 for px, py in board
            if px <= x + 3 or py <= y + 3 or px >= x + w - 3 or py >= y + h - 3
        )
        if edge >= max(2, len(board) * 0.6):
            continue
        length = sum(np.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(board, board[1:]))
        if length >= min_len:
            lines.append((length, board))
    lines.sort(key=lambda t: -t[0])
    kept = [b for _, b in lines[:max_strokes]]
    return kept, len(lines)


def _marker(image_bytes: bytes, x: float, y: float, w: float, h: float):
    """Keep the pen strokes and throw away the photograph."""
    bgr = _decode(image_bytes)
    work_w, work_h = max(8, int(w)), max(8, int(h))
    bgr = cv2.resize(bgr, (work_w, work_h), interpolation=cv2.INTER_AREA)
    mask = _marker_mask(bgr)
    # A 1-pixel stroke disappears if it is thinned. Fatten it first, then take the centre.
    if mask.any():
        mask = cv2.dilate(mask, np.ones((3, 3), np.uint8))
        skeleton = _thin(mask)
        if not skeleton.any():
            skeleton = mask
    else:
        skeleton = mask
    kept, found = _strokes_from_mask(skeleton, x, y, w, h, min_len=8, eps=1.4, max_strokes=400)
    return kept, {"found": found, "kept": len(kept), "threshold": 0, "method": "marker"}


def trace(image_bytes: bytes, x: float, y: float, w: float, h: float, detail: str = "medium"):
    """Returns (strokes in board coords, info dict)."""
    if detail == "marker":
        kept, info = _marker(image_bytes, x, y, w, h)
        if kept:
            return kept, info
        detail = "high"
    scale, blur, min_len, max_strokes, eps = DETAIL.get(detail, DETAIL["medium"])
    gray = cv2.cvtColor(_decode(image_bytes), cv2.COLOR_BGR2GRAY)

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


def _order_corners(pts: np.ndarray) -> np.ndarray:
    """Four points as top-left, top-right, bottom-right, bottom-left."""
    pts = np.array(pts, dtype=np.float32)
    total = pts.sum(axis=1)
    diff = np.diff(pts, axis=1).ravel()
    return np.array([
        pts[np.argmin(total)],
        pts[np.argmin(diff)],
        pts[np.argmax(total)],
        pts[np.argmax(diff)],
    ], dtype=np.float32)


def _board_quad(bgr: np.ndarray):
    """The whiteboard is the big bright, low-saturation region. Marker holes get filled in."""
    h, w = bgr.shape[:2]
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, (0, 0, 150), (180, 80, 255))
    k = max(5, int(min(h, w) * 0.02) | 1)
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (k, k))
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return None
    contour = max(contours, key=cv2.contourArea)
    if cv2.contourArea(contour) < 0.08 * w * h:
        return None
    peri = cv2.arcLength(contour, True)
    for scale in (0.02, 0.04, 0.08):
        approx = cv2.approxPolyDP(contour, scale * peri, True)
        if len(approx) == 4:
            return approx.reshape(4, 2)
    return cv2.boxPoints(cv2.minAreaRect(contour))


def crop_whiteboard(image_bytes: bytes) -> bytes | None:
    """Perspective-crop to the whiteboard. None when the photo is already just the board."""
    bgr = _decode(image_bytes)
    h, w = bgr.shape[:2]
    quad = _board_quad(bgr)
    if quad is None:
        return None
    rect = _order_corners(quad)
    width = int(max(np.linalg.norm(rect[1] - rect[0]), np.linalg.norm(rect[2] - rect[3])))
    height = int(max(np.linalg.norm(rect[3] - rect[0]), np.linalg.norm(rect[2] - rect[1])))
    width, height = max(width, 8), max(height, 8)
    margin = 0.04
    on_frame = (
        rect[0][0] <= w * margin and rect[0][1] <= h * margin
        and rect[1][0] >= w * (1 - margin) and rect[1][1] <= h * margin
        and rect[2][0] >= w * (1 - margin) and rect[2][1] >= h * (1 - margin)
        and rect[3][0] <= w * margin and rect[3][1] >= h * (1 - margin)
    )
    if on_frame:
        return None
    dest = np.array([[0, 0], [width - 1, 0], [width - 1, height - 1], [0, height - 1]], dtype=np.float32)
    matrix = cv2.getPerspectiveTransform(rect, dest)
    warped = cv2.warpPerspective(bgr, matrix, (width, height), flags=cv2.INTER_LINEAR, borderValue=(255, 255, 255))
    ok, buf = cv2.imencode(".png", warped)
    if not ok:
        return None
    return buf.tobytes()