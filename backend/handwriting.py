"""
A tiny single-stroke "plotter font" for writing answers.

Each character is a list of polylines in a unit box: x goes right, y goes DOWN
(same as the canvas), y=0 is the top of a digit and y=1 is the baseline.
Because every glyph is made of lines (not filled shapes), the same output can
be animated in the browser or sent to the robot as pen moves.
"""
import math

from text_writer import plus_minus

Point = tuple[float, float]


def arc(cx, cy, rx, ry, start_deg, end_deg, n=18) -> list[Point]:
    """Points along an ellipse arc. Angles use screen coords: -90 is top, 90 is bottom."""
    pts = []
    for i in range(n + 1):
        t = math.radians(start_deg + (end_deg - start_deg) * i / n)
        pts.append((cx + rx * math.cos(t), cy + ry * math.sin(t)))
    return pts


def ellipse(cx, cy, rx, ry, n=28) -> list[Point]:
    return arc(cx, cy, rx, ry, -90, 270, n)


# glyph = (advance_width, [polyline, polyline, ...])
GLYPHS: dict[str, tuple[float, list[list[Point]]]] = {
    "0": (0.6, [ellipse(0.3, 0.5, 0.3, 0.5)]),
    "1": (0.45, [[(0.1, 0.2), (0.28, 0.0), (0.28, 1.0)]]),
    "2": (0.6, [arc(0.3, 0.28, 0.28, 0.28, 180, 400) + [(0.0, 1.0), (0.6, 1.0)]]),
    "3": (0.6, [arc(0.3, 0.25, 0.26, 0.25, 200, 450) + arc(0.3, 0.75, 0.3, 0.25, -90, 160)[1:]]),
    "4": (0.6, [[(0.45, 1.0), (0.45, 0.0), (0.0, 0.7), (0.6, 0.7)]]),
    "5": (0.6, [[(0.55, 0.0), (0.08, 0.0), (0.05, 0.45)] + arc(0.3, 0.68, 0.3, 0.32, -120, 150)]),
    "6": (0.6, [arc(0.55, 0.7, 0.55, 0.7, 250, 180), ellipse(0.3, 0.7, 0.3, 0.3)]),
    "7": (0.6, [[(0.0, 0.0), (0.6, 0.0), (0.2, 1.0)]]),
    "8": (0.6, [ellipse(0.3, 0.24, 0.24, 0.24), ellipse(0.3, 0.72, 0.3, 0.28)]),
    "9": (0.6, [ellipse(0.3, 0.3, 0.3, 0.3), [(0.6, 0.3), (0.42, 1.0)]]),
    "+": (0.6, [[(0.05, 0.5), (0.55, 0.5)], [(0.3, 0.25), (0.3, 0.75)]]),
    "-": (0.6, [[(0.05, 0.5), (0.55, 0.5)]]),
    "±": (0.6, [[(0.05, 0.42), (0.55, 0.42)], [(0.3, 0.18), (0.3, 0.66)], [(0.05, 0.88), (0.55, 0.88)]]),
    "=": (0.6, [[(0.05, 0.38), (0.55, 0.38)], [(0.05, 0.62), (0.55, 0.62)]]),
    ".": (0.25, [ellipse(0.1, 0.95, 0.04, 0.04, 10)]),
    ",": (0.25, [[(0.12, 0.9), (0.06, 1.12)]]),
    "/": (0.6, [[(0.55, 0.0), (0.05, 1.0)]]),
    "(": (0.35, [arc(0.4, 0.5, 0.35, 0.58, 240, 120)]),
    ")": (0.35, [arc(-0.05, 0.5, 0.35, 0.58, -60, 60)]),
    "x": (0.6, [[(0.05, 0.35), (0.55, 1.0)], [(0.55, 0.35), (0.05, 1.0)]]),
    "y": (0.6, [[(0.05, 0.35), (0.3, 0.75)], [(0.55, 0.35), (0.15, 1.2)]]),
    "z": (0.6, [[(0.05, 0.35), (0.55, 0.35), (0.05, 1.0), (0.55, 1.0)]]),
    "a": (0.6, [ellipse(0.27, 0.67, 0.25, 0.32), [(0.52, 0.35), (0.52, 1.0)]]),
    "b": (0.6, [[(0.05, 0.0), (0.05, 1.0)], ellipse(0.3, 0.67, 0.25, 0.33)]),
    "c": (0.55, [arc(0.32, 0.67, 0.28, 0.33, -40, -320)]),
    "n": (0.6, [[(0.05, 0.35), (0.05, 1.0)], arc(0.3, 0.6, 0.25, 0.25, 180, 360) + [(0.55, 1.0)]]),
    "t": (0.5, [[(0.25, 0.05), (0.25, 0.9), (0.42, 1.0)], [(0.05, 0.35), (0.45, 0.35)]]),
    "✓": (0.7, [[(0.05, 0.6), (0.25, 0.95), (0.65, 0.05)]]),
    "✗": (0.7, [[(0.05, 0.1), (0.6, 0.95)], [(0.6, 0.1), (0.05, 0.95)]]),
    " ": (0.4, []),
}

LETTER_SPACING = 0.22  # gap between characters, as a fraction of text height


def text_to_strokes(text: str, x: float, y_top: float, height: float) -> list[list[list[float]]]:
    """Turn text into strokes (lists of [x, y] points) starting at (x, y_top)."""
    text = plus_minus(text)
    strokes = []
    cursor = x
    for ch in text:
        glyph = GLYPHS.get(ch) or GLYPHS.get(ch.lower())
        if glyph is None:  # unknown character: leave a gap rather than crash
            cursor += 0.5 * height
            continue
        width, polylines = glyph
        for line in polylines:
            strokes.append([[round(cursor + px * height, 1), round(y_top + py * height, 1)] for px, py in line])
        cursor += (width + LETTER_SPACING) * height
    return strokes


def text_width(text: str, height: float) -> float:
    total = 0.0
    for ch in plus_minus(text):
        glyph = GLYPHS.get(ch) or GLYPHS.get(ch.lower())
        total += ((glyph[0] if glyph else 0.5) + LETTER_SPACING) * height
    return total
