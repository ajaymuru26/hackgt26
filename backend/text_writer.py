"""
Write typed text on the board with a single-stroke plotter font (Hershey fonts),
so the robot draws each letter with one pen line, like handwriting.

Math is written as notation, not spelled out: sqrt(...) becomes a radical sign with
its bar over everything inside, +- becomes a single plus-minus sign, and ∫ (or
integral(...), int_a^b, "integral from a to b") becomes an integral sign with its
limits under and over it. The fonts have none of these glyphs, so they are drawn
here from pen strokes.
"""
import math
import re
from functools import lru_cache

from HersheyFonts import HersheyFonts

STYLES = {"print": "futural", "cursive": "scripts"}
FULL = 100.0        # render size used for measuring
CAP = 0.75          # capital letters take up 75% of the font's full height
LINE_GAP = 3.2      # line spacing, as a multiple of the capital height; roomy so marker lines never touch

PM = "±"
# "+-" is plus-minus, except tight between two digits ("3+-2"), where it is plus a negative number.
_PLUS_MINUS = re.compile(r"(?<!\d)\+-|\+-(?!\d)")
_ROOT_WORD = re.compile(r"√\s*([A-Za-z0-9.]+)")

RADICAL_HOOK = 44.0   # font units from the start of the radical to where the expression under it begins
RADICAL_GAP = 10.0    # how far the bar sits above the tallest mark under it
RADICAL_TAIL = 4.0    # the bar runs this far past the end of the expression

INT = "∫"
INT_LOW, INT_HIGH = -14.0, 124.0  # the sign runs from below the descenders to well above the capitals
INT_HOOK = 11.0       # radius of the curl at each end
INT_SLANT = 26.0      # how far right the top of the stem sits from the bottom
INT_WIDTH = INT_SLANT + 4 * INT_HOOK
LIMIT_SCALE = 0.55    # limits are written at about half size
LIMIT_GAP = 7.0       # space between the sign and a limit

# A limit after _ or ^: {group}, (group), a number, or a word such as pi.
_LIMIT_TOKEN = r"(?:\{[^{}]*\}|\([^()]*\)|-?\d+(?:\.\d+)?|-?[A-Za-z]+)"
_LIMIT = re.compile(r"\s*([_^])\s*(-?\d+(?:\.\d+)?|-?[A-Za-z]+|[{(])")
_INT_HEAD = re.compile(rf"{INT}(?:\s*[_^]\s*{_LIMIT_TOKEN})*")
_INT_CALL = re.compile(r"\b(integral|int)\s*\(", re.I)
_INT_RANGE = re.compile(r"\b(?:integral|int)\s+from\s+(\S+)\s+to\s+(\S+)(?:\s+of\b)?\s*", re.I)
_INT_SUB = re.compile(r"\b(?:integral|int)\s*(?=[_^])", re.I)
# "int x^2 dx" or "the integral of x^2 dx": the word is the operator only when a dx closes the clause.
_INT_OPERATOR = re.compile(
    r"\b(?:integral(?:\s+of)?|int)\s+"
    r"(?=(?:(?!\b(?:is|of|and|the|from|with|to|equals)\b)[^=\n,;])*?(?<![A-Za-z])d[a-z]\b)", re.I)


@lru_cache(maxsize=None)
def _font(style: str) -> HersheyFonts:
    f = HersheyFonts()
    f.load_default_font(STYLES.get(style, "futural"))
    f.normalize_rendering(FULL)
    return f


def _raw(text: str, style: str):
    return [list(s) for s in _font(style).strokes_for_text(text)]


def _advance(text: str, style: str) -> float:
    f = _font(style)
    return sum(g.char_width for g in f.glyphs_for_text(text)) * f.render_options.scalex


@lru_cache(maxsize=None)
def _baseline(style: str) -> float:
    return min(p[1] for s in _raw("x", style) for p in s)


def plus_minus(text: str) -> str:
    return _PLUS_MINUS.sub(PM, text)


def _split_args(inner: str) -> list[str]:
    parts, depth, cur = [], 0, ""
    for ch in inner:
        if ch == "," and depth == 0:
            parts.append(cur.strip())
            cur = ""
            continue
        depth += {"(": 1, ")": -1}.get(ch, 0)
        cur += ch
    return parts + [cur.strip()]


def _integral_call(args: list[str]) -> str | None:
    """integral(f, x), Integral(f, (x, a, b)) or integral(f, x, a, b)  ->  ∫_{a}^{b} f dx"""
    if len(args) == 2 and args[1].startswith("(") and args[1].endswith(")"):
        args = [args[0]] + _split_args(args[1][1:-1])
    if not args[0] or len(args) not in (1, 2, 4):
        return None
    var = args[1] if len(args) > 1 else "x"
    if not re.fullmatch(r"[A-Za-z]", var):
        return None
    body = args[0].replace("**", "^")
    depth, loose = 0, False
    for i, ch in enumerate(body):
        depth += {"(": 1, ")": -1}.get(ch, 0)
        loose |= depth == 0 and i > 0 and ch in "+-"
    if loose:
        body = f"({body})"
    limits = "".join(f"{mark}{{{''.join(lim.split())}}}" for mark, lim in zip("_^", args[2:]))
    return f"{INT}{limits} {body} d{var}"


def _integral_calls(text: str) -> str:
    start = 0
    while True:
        match = _INT_CALL.search(text, start)
        if not match:
            return text
        depth, j = 1, match.end()
        while j < len(text) and depth:
            depth += {"(": 1, ")": -1}.get(text[j], 0)
            j += 1
        args = _split_args(text[match.end():j - 1]) if depth == 0 else []
        # int(...) with no variable is more likely a programming int() than an integral
        drawn = _integral_call(args) if args and (len(args) > 1 or match.group(1).lower() != "int") else None
        if drawn is None:
            start = match.end()
            continue
        text = text[:match.start()] + drawn + text[j:]
        start = match.start() + len(drawn)


def integral_notation(text: str) -> str:
    """Every way an integral gets spelled, as ∫ with its limits right after it: ∫_{0}^{1} x^2 dx."""
    text = _integral_calls(text)
    text = _INT_RANGE.sub(lambda m: f"{INT}_{{{m.group(1)}}}^{{{m.group(2)}}} ", text)
    text = _INT_SUB.sub(INT, text)
    text = _INT_OPERATOR.sub(INT + " ", text)
    # keep a sign and its limits in one word so a line never breaks between them
    return _INT_HEAD.sub(lambda m: re.sub(r"\s+", "", m.group(0)), text)


def math_notation(text: str) -> str:
    """Spell math the way it is drawn: plus-minus as ±, √ as sqrt(...), and integrals as ∫."""
    text = text.replace("√(", "sqrt(")
    text = _ROOT_WORD.sub(r"sqrt(\1)", text)
    return plus_minus(integral_notation(text))


def _limits(text: str, i: int):
    """The _lower and ^upper limits written right after an integral sign, and where they end."""
    found = {}
    while True:
        match = _LIMIT.match(text, i)
        if not match or match.group(1) in found:
            return found, i
        token = match.group(2)
        if token in ("{", "("):
            close = "}" if token == "{" else ")"
            depth, j = 1, match.end()
            while j < len(text) and depth:
                depth += (text[j] == token) - (text[j] == close)
                j += 1
            if depth:
                return found, i
            found[match.group(1)] = text[match.end():j - 1].strip()
            i = j
        else:
            found[match.group(1)] = token
            i = match.end()


def _parse(text: str) -> list:
    """Pieces of a line: ("text", str), ("pm", ""), ("sqrt", pieces under the bar),
    or ("int", (lower limit pieces, upper limit pieces))."""
    pieces, buf, i = [], "", 0
    while i < len(text):
        if text[i] == INT:
            if buf:
                pieces.append(("text", buf))
                buf = ""
            found, i = _limits(text, i + 1)
            while i < len(text) and text[i] == " ":
                i += 1
            pieces.append(("int", tuple(_parse(found[m]) if found.get(m) else [] for m in "_^")))
            continue
        if text[i:i + 5].lower() == "sqrt(":
            depth, j = 1, i + 5
            while j < len(text) and depth:
                depth += {"(": 1, ")": -1}.get(text[j], 0)
                j += 1
            inner = text[i + 5:j - 1] if depth == 0 else text[i + 5:]
            if buf:
                pieces.append(("text", buf))
                buf = ""
            pieces.append(("sqrt", _parse(inner)))
            i = j
        elif text[i] == PM:
            if buf:
                pieces.append(("text", buf))
                buf = ""
            pieces.append(("pm", ""))
            i += 1
        else:
            buf += text[i]
            i += 1
    if buf:
        pieces.append(("text", buf))
    return pieces


def _plus_minus(style: str, x: float):
    """A plus sign raised a little, with a bar on the baseline under it, as wide as the font's +."""
    plus = _raw("+", style)
    xs = [p[0] for s in plus for p in s]
    ys = [p[1] for s in plus for p in s]
    left, right, low, high = x + min(xs), x + max(xs), min(ys), max(ys)
    mid = (left + right) / 2
    cy = low + (high - low) * 0.6
    arm = (high - low) * 0.39
    return [[(mid, cy + arm), (mid, cy - arm)],
            [(left, cy), (right, cy)],
            [(left, low), (right, low)]]


def _arc(cx: float, cy: float, r: float, start: float, end: float, n: int = 10):
    """Points around a circle, angles in degrees counterclockwise (font y points up)."""
    return [(cx + r * math.cos(math.radians(start + (end - start) * k / n)),
             cy + r * math.sin(math.radians(start + (end - start) * k / n))) for k in range(n + 1)]


def _integral_sign(cx: float):
    """One pen line: a curl at the top right, a slanted S down the middle, a curl at the bottom left."""
    r, half = INT_HOOK, INT_SLANT / 2
    top, bottom = (cx + half, INT_HIGH - r), (cx - half, INT_LOW + r)
    pull = (top[1] - bottom[1]) * 0.45
    c1, c2 = (top[0], top[1] - pull), (bottom[0], bottom[1] + pull)
    stem = []
    for k in range(1, 17):
        t = k / 16
        a, b, c, d = (1 - t) ** 3, 3 * (1 - t) ** 2 * t, 3 * (1 - t) * t ** 2, t ** 3
        stem.append((a * top[0] + b * c1[0] + c * c2[0] + d * bottom[0],
                     a * top[1] + b * c1[1] + c * c2[1] + d * bottom[1]))
    return (_arc(top[0] + r, top[1], r, -40, 180) + stem
            + _arc(bottom[0] - r, bottom[1], r, 0, -220)[1:])


def _limit(pieces: list, style: str):
    """A limit at about half size, and its bounding box (left, bottom, right, top)."""
    strokes = [[(px * LIMIT_SCALE, py * LIMIT_SCALE) for px, py in s]
               for s in _layout(pieces, style, 0.0)[0]]
    xs, ys = [p[0] for s in strokes for p in s], [p[1] for s in strokes for p in s]
    return strokes, (min(xs), min(ys), max(xs), max(ys)) if xs else (0.0, 0.0, 0.0, 0.0)


def _glyphs(pieces: list, style: str, x: float):
    """Strokes in font units (y up) for parsed pieces starting at x, grouped one mark per group
    (a character, a sign, a radical, a limit) in reading order, and the x where they end."""
    groups = []
    base = _baseline(style)
    for kind, value in pieces:
        if kind == "text":
            for ch in value:
                drawn = [[(x + px, py) for px, py in s] for s in _raw(ch, style)]
                if drawn:
                    groups.append(drawn)
                x += _advance(ch, style)
        elif kind == "pm":
            groups.append(_plus_minus(style, x))
            x += _advance("+", style)
        elif kind == "int":
            # the lower limit sits under the bottom curl and the upper one over the top curl
            curl = INT_SLANT / 2 + INT_HOOK
            lower, lo = _limit(value[0], style)
            upper, hi = _limit(value[1], style)
            half_lo, half_hi = (lo[2] - lo[0]) / 2, (hi[2] - hi[0]) / 2
            cx = x + 6 + max(INT_WIDTH / 2, curl + half_lo, half_hi - curl)
            groups.append([_integral_sign(cx)])
            for part, box, mid, dy in ((lower, lo, cx - curl, INT_LOW - LIMIT_GAP - lo[3]),
                                       (upper, hi, cx + curl, INT_HIGH + LIMIT_GAP - hi[1])):
                dx = mid - (box[0] + box[2]) / 2
                if part:
                    groups.append([[(px + dx, py + dy) for px, py in s] for s in part])
            x = max(cx + INT_WIDTH / 2, cx - curl + half_lo, cx + curl + half_hi) + 14
        else:
            inner, end = _glyphs(value, style, x + RADICAL_HOOK)
            top = max([FULL] + [p[1] for g in inner for s in g for p in s])
            bar = top + RADICAL_GAP
            # one pen line: a short tick up, down below the baseline, up to the bar, and across
            groups.append([[(x + 2, base + 22), (x + 10, base + 30), (x + 24, base - 9),
                            (x + RADICAL_HOOK - 4, bar), (end + RADICAL_TAIL, bar)]])
            groups += inner
            x = end + RADICAL_TAIL + 4
    return groups, x


def _layout(pieces: list, style: str, x: float):
    """Strokes in font units (y up) for parsed pieces starting at x, and the x where they end."""
    groups, x = _glyphs(pieces, style, x)
    return [s for g in groups for s in g], x


def _line(text: str, style: str):
    if PM not in text and INT not in text and "sqrt(" not in text.lower():
        return _raw(text, style)
    return _layout(_parse(text), style, 0.0)[0]


def measure(text: str, style: str, cap_height: float) -> float:
    xs = [p[0] for s in _line(text, style) for p in s]
    if not xs:
        return len(text) * cap_height * 0.5  # only spaces
    return (max(xs) - min(0, min(xs))) * cap_height / (FULL * CAP)


def _open_root(text: str) -> bool:
    """True when a sqrt( in text has not been closed yet."""
    stack, i = [], 0
    while i < len(text):
        if text[i:i + 5].lower() == "sqrt(":
            stack.append("root")
            i += 5
            continue
        if text[i] == "(":
            stack.append("paren")
        elif text[i] == ")" and stack:
            stack.pop()
        i += 1
    return "root" in stack


def _bare_integral(word: str) -> bool:
    """True when word ends with an integral sign (and its limits) and nothing after it."""
    heads = list(_INT_HEAD.finditer(word))
    return bool(heads) and heads[-1].end() == len(word)


def _words(paragraph: str) -> list[str]:
    """Words to wrap on. Everything under one radical stays together so its bar is never split,
    and an integral sign stays on the line with the first word after it."""
    words = []
    for word in paragraph.split():
        if words and (_open_root(words[-1]) or _bare_integral(words[-1])):
            words[-1] += " " + word
        else:
            words.append(word)
    return words


def wrap(text: str, style: str, cap_height: float, max_width: float) -> list[str]:
    text = math_notation(text)
    lines = []
    for paragraph in text.splitlines() or [""]:
        line = ""
        for word in _words(paragraph):
            trial = f"{line} {word}".strip()
            if not line or measure(trial, style, cap_height) <= max_width:
                line = trial
            else:
                lines.append(line)
                line = word
        lines.append(line)
    return lines


def _printable(text: str) -> str:
    text = math_notation(text)
    return "".join(ch if 32 <= ord(ch) < 127 or ch in "\n" + PM + INT else "?" for ch in text)


def line_glyphs(line: str, x: float, y_top: float, cap_height: float, style: str = "print"):
    """One line of text on the board as groups of strokes, one group per mark, in reading order."""
    scale = cap_height / (FULL * CAP)
    groups = _glyphs(_parse(_printable(line)), style, 0.0)[0]
    return [[[[round(x + px * scale, 1), round(y_top + (FULL - py) * scale, 1)] for px, py in s]
             for s in g] for g in groups]


def text_strokes(text: str, x: float, y_top: float, cap_height: float, max_width: float,
                 style: str = "print", max_y: float | None = None):
    """Returns (strokes, lines_written, lines_dropped). y_top is the top of the capitals."""
    text = _printable(text)
    scale = cap_height / (FULL * CAP)
    lines = wrap(text, style, cap_height, max_width)
    strokes, written = [], 0
    for i, line in enumerate(lines):
        top = y_top + i * cap_height * LINE_GAP
        # the font's y axis points up; the board's points down, so flip it
        drawn = [[[round(x + px * scale, 1), round(top + (FULL - py) * scale, 1)] for px, py in s]
                 for s in _line(line, style)]
        bottom = max([top + cap_height * 1.35] + [p[1] for s in drawn for p in s])
        if max_y is not None and bottom > max_y:
            break  # no room left on the board
        strokes += drawn
        written += 1
    return strokes, written, len(lines) - written
