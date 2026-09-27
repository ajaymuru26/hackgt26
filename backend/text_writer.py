"""
Write typed text on the board with a single-stroke plotter font (Hershey fonts),
so the robot draws each letter with one pen line, like handwriting.
"""
from functools import lru_cache

from HersheyFonts import HersheyFonts

STYLES = {"print": "futural", "cursive": "scripts"}
FULL = 100.0        # render size used for measuring
CAP = 0.75          # capital letters take up 75% of the font's full height
LINE_GAP = 2.05     # line spacing, as a multiple of the capital height


@lru_cache(maxsize=None)
def _font(style: str) -> HersheyFonts:
    f = HersheyFonts()
    f.load_default_font(STYLES.get(style, "futural"))
    f.normalize_rendering(FULL)
    return f


def _raw(text: str, style: str):
    return [list(s) for s in _font(style).strokes_for_text(text)]


def measure(text: str, style: str, cap_height: float) -> float:
    xs = [p[0] for s in _raw(text, style) for p in s]
    if not xs:
        return len(text) * cap_height * 0.5  # only spaces
    return (max(xs) - min(0, min(xs))) * cap_height / (FULL * CAP)


def wrap(text: str, style: str, cap_height: float, max_width: float) -> list[str]:
    lines = []
    for paragraph in text.splitlines() or [""]:
        line = ""
        for word in paragraph.split():
            trial = f"{line} {word}".strip()
            if not line or measure(trial, style, cap_height) <= max_width:
                line = trial
            else:
                lines.append(line)
                line = word
        lines.append(line)
    return lines


def text_strokes(text: str, x: float, y_top: float, cap_height: float, max_width: float,
                 style: str = "print", max_y: float | None = None):
    """Returns (strokes, lines_written, lines_dropped). y_top is the top of the capitals."""
    text = "".join(ch if 32 <= ord(ch) < 127 or ch == "\n" else "?" for ch in text)
    scale = cap_height / (FULL * CAP)
    lines = wrap(text, style, cap_height, max_width)
    strokes, written = [], 0
    for i, line in enumerate(lines):
        top = y_top + i * cap_height * LINE_GAP
        if max_y is not None and top + cap_height * 1.35 > max_y:
            break  # no room left on the board
        for s in _raw(line, style):
            # the font's y axis points up; the board's points down, so flip it
            strokes.append([[round(x + px * scale, 1), round(top + (FULL - py) * scale, 1)]
                            for px, py in s])
        written += 1
    return strokes, written, len(lines) - written