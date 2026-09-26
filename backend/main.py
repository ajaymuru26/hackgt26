"""
Whiteboard finisher backend.

Run:  uvicorn main:app --reload     (from the backend/ folder)
Then open http://localhost:8000
"""
import base64
import os
import re
import math
import time
from pathlib import Path

from dotenv import load_dotenv

# Load API keys from backend/.env before anything reads them (this file is git-ignored)
load_dotenv(Path(__file__).resolve().parent / ".env")

from fastapi import FastAPI, HTTPException
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import boards
import cv_finish
import gcode
import handwriting
import math_solver
import memory as student_memory
import placement
import robot as robot_link
import shapes
import speech
import text_writer
import tracer
import vision

app = FastAPI(title="Whiteboard Finisher")

Stroke = list[list[float]]


class CompleteRequest(BaseModel):
    image: str               # PNG as a data URL or plain base64
    width: int
    height: int
    strokes: list[Stroke] = []  # what the person drew, in canvas pixels
    mode: str = "auto"          # auto | math | fill | drawing (chosen in the UI)
    action: str = ""            # answer | hint | check | drawing (which button was pressed)
    image_box: list[float] | None = None  # where an uploaded picture sits on the board [x1, y1, x2, y2]
    review: bool = True         # let the AI look at its drawing once and fix mistakes
    provider: str = "openai"    # openai | gemini, chosen by the buttons on the board
    instruction: str = ""       # what the person typed for Finish drawing


class WriteRequest(BaseModel):
    text: str
    x: float                   # where the text starts (top-left of the first line)
    y: float
    size: float = 50           # height of capital letters, in board pixels
    style: str = "print"       # print | cursive
    width: int = 1200
    height: int = 700


class AskRequest(BaseModel):
    prompt: str
    image: str                 # board snapshot, like /api/complete
    strokes: list[Stroke] = []
    x: float                   # where the answer goes (where the person clicked)
    y: float
    size: float = 50
    style: str = "print"
    width: int = 1200
    height: int = 700
    provider: str = "openai"    # openai | gemini


class TraceRequest(BaseModel):
    image: str                 # the uploaded picture, already sized to where it sits on the board
    x: float
    y: float
    w: float
    h: float
    detail: str = "medium"     # low | medium | high | marker
    width: int = 1200
    height: int = 700


class CropRequest(BaseModel):
    image: str                 # the original photo, before it is stretched onto the board


class TranscribeRequest(BaseModel):
    audio: str                 # a clip so far, as a data URL or plain base64
    mime: str = "audio/webm"


class SpeakRequest(BaseModel):
    audio: str                 # recording as a data URL or plain base64
    mime: str = "audio/webm"
    text: str = ""             # words already shown while they were talking
    image: str
    strokes: list[Stroke] = []
    width: int = 1200
    height: int = 700
    provider: str = "openai"
    size: float = 48
    style: str = "print"


def _open_box(strokes, w, h):
    """A free rectangle for a spoken drawing, away from ink already on the board."""
    box = content_bbox(strokes)
    if not box:
        return [70.0, 50.0, float(w - 50), float(h - 40)]
    if w - box[2] >= 300:
        return [box[2] + 28, 40.0, float(w - 30), float(h - 30)]
    if h - box[3] >= 180:
        return [max(20.0, box[0]), box[3] + 28, float(w - 30), float(h - 30)]
    return [70.0, 50.0, float(w - 50), float(h - 40)]


def _from_prompt(ai, prompt, x, y, size, style, width, height, steps, prompt_strokes, ink=()):
    """Turn an ask/speak model reply into strokes the robot can draw.
    `ink` is what's already on the board, which the answer keeps clear of."""
    if ai.get("kind") == "drawing":
        if ai.get("description"):
            steps.append({"title": "Decided to draw", "detail": ai["description"]})
        log = []
        strokes = clean_strokes(shapes.build_strokes({"shapes": ai.get("shapes")}, [], log), width, height)
        for note in log:
            steps.append({"title": "Made the drawing", "detail": note})
        if not strokes:
            raise HTTPException(422, "The AI couldn't work out how to draw that. Try describing it differently.")
        steps.append({"title": "Planned the robot", "detail": robot_summary(strokes, width, height)})
        return {"mode": "drawing", "description": ai.get("description", "a drawing"), "answer": "",
                "say": explain_drawing(ai.get("description", prompt)),
                "strokes": strokes, "prompt_strokes": prompt_strokes, "steps": steps,
                "category": {"type": "Drawing", "detail": ai.get("description", prompt)}}

    answer = str(ai.get("answer", "")).strip()
    if ai.get("reasoning"):
        steps.append({"title": "How it answered", "detail": ai["reasoning"]})
    if not answer:
        raise HTTPException(422, "The AI didn't come up with an answer. Try saying it again.")
    board = placement.Board(width, height, list(ink) + list(prompt_strokes))
    pbox = placement.bounds(prompt_strokes) if prompt_strokes else None

    def wrap_w(s):
        return max(width - x - 20, s * 3)

    def spots(s, bw, bh):
        gap = placement.GAP
        if not pbox:
            return [(x, y, "where you asked")]
        return [(x, max(y, pbox[3] + gap), "under your prompt"),
                (pbox[2] + max(s, gap), pbox[1], "to the right of your prompt"),
                (x, pbox[1] - bh - max(s * 0.35, gap), "above your prompt")]

    got = placement.place(board, lambda s: text_writer.text_strokes(answer, 0, 0, s, wrap_w(s), style)[0],
                          spots, placement.shrinking(size))
    strokes = got.strokes
    if not strokes:
        raise HTTPException(422, "There's no room to write there. Clear some space and try again.")
    written = len(text_writer.wrap(answer, style, got.size, wrap_w(got.size)))
    steps.append({"title": "Wrote the answer", "detail": f"\"{answer}\" {got.where}, in {written} line(s), "
                                                         "using a single-stroke pen font."})
    steps.append({"title": "Planned the robot", "detail": robot_summary(strokes, width, height)})
    return {"mode": "answer", "description": prompt, "answer": answer,
            "say": explain_answer(prompt, answer, ai.get("reasoning")),
            "strokes": strokes, "prompt_strokes": prompt_strokes, "steps": steps,
            "category": {"type": "Answer", "detail": prompt}}


class GcodeRequest(BaseModel):
    strokes: list[Stroke]
    width: int
    height: int
    board_width_mm: float = 800
    board_height_mm: float = 500


# ---------- helpers ----------

def stroke_bbox(s: Stroke):
    xs, ys = [p[0] for p in s], [p[1] for p in s]
    return min(xs), min(ys), max(xs), max(ys)


def refine_bbox(ai_bbox, user_strokes, pad=30):
    """The AI's box is approximate; snap it to the real strokes it covers."""
    x1, y1, x2, y2 = ai_bbox
    hits = []
    for s in user_strokes:
        if not s:
            continue
        a, b, c, d = stroke_bbox(s)
        if c >= x1 - pad and a <= x2 + pad and d >= y1 - pad and b <= y2 + pad:
            hits.append((a, b, c, d))
    if not hits:
        return ai_bbox
    return (min(h[0] for h in hits), min(h[1] for h in hits),
            max(h[2] for h in hits), max(h[3] for h in hits))


def clean_strokes(raw, w, h) -> list[Stroke]:
    cleaned = []
    for s in raw or []:
        try:
            pts = [[min(max(float(p[0]), 0), w), min(max(float(p[1]), 0), h)] for p in s]
        except (TypeError, ValueError, IndexError):
            continue  # skip anything malformed
        if len(pts) >= 2:  # (no smoothing: it would round off sharp corners like a roof peak)
            cleaned.append([[round(x, 1), round(y, 1)] for x, y in pts])
    return cleaned


def _loose_box(box, w, h) -> bool:
    """A box around the whole board is not a box around the problem."""
    if not box or len(box) < 4:
        return True
    return (float(box[2]) - float(box[0])) >= w * 0.55 or (float(box[3]) - float(box[1])) >= h * 0.55


def answer_shape(text, size) -> list[Stroke]:
    """Answer text drawn at the origin, in the maths font when it has every character."""
    if all(ch in handwriting.GLYPHS for ch in text):
        return handwriting.text_to_strokes(text, 0, 0, size)
    # full pen font for anything the small maths font can't draw (like i)
    strokes, _, _ = text_writer.text_strokes(text, 0, 0, size * 0.8, 10 ** 6, "print")
    return strokes


def problem_core(bbox, ink, near_y=None):
    """The problem's own line of characters, and their typical height.

    The problem's box can grow to take in things that aren't the problem: a long line
    crossing it, or notes on the next line. Sizing and lining up the answer from that box
    gives a giant answer, or one floating between two lines."""
    x1, y1, x2, y2 = bbox
    inside = [stroke_bbox(s) for s in ink if s]
    inside = [b for b in inside if b[2] >= x1 - 2 and b[0] <= x2 + 2 and b[3] >= y1 - 2 and b[1] <= y2 + 2]
    heights = sorted(b[3] - b[1] for b in inside if b[3] - b[1] >= 10)
    if not heights:
        return bbox, y2 - y1
    char_h = heights[len(heights) // 2]
    # drop long lines through the problem (much taller or wider than a character)
    core = [b for b in inside if b[3] - b[1] <= 1.8 * char_h and b[2] - b[0] <= 6 * char_h]
    rows = []
    for b in sorted(core, key=lambda b: (b[1] + b[3]) / 2):
        if rows and (b[1] + b[3]) / 2 <= rows[-1][3] + 0.25 * char_h:
            r = rows[-1]
            rows[-1] = [min(r[0], b[0]), min(r[1], b[1]), max(r[2], b[2]), max(r[3], b[3])]
        else:
            rows.append(list(b))
    if not rows:
        return bbox, char_h
    # the line the AI pointed at (or the top one), not whatever else got merged in
    row = min(rows, key=lambda r: abs((r[1] + r[3]) / 2 - near_y)) if near_y is not None else rows[0]
    return tuple(row), char_h


def place_answer(expression, result, bbox, w, h, board, blank_bbox=None, ink=(), near_y=None) -> list[Stroke]:
    bbox, _ = problem_core(bbox, ink, near_y)
    x1, y1, x2, y2 = bbox
    eq_h = max(y2 - y1, 1)
    size = min(max(eq_h * 0.92, 28), 160)
    text = result["answer"]

    if result["kind"] == "blank" and re.search(r"=[_?]$", str(expression).replace(" ", "")):
        # "10-8=_" is just "10-8=". A long lower bar on an equals sign is easy to misread
        # as a blank line, and writing "in" it would put the answer on top of the "=".
        expression = str(expression).replace(" ", "")[:-1]
        result, blank_bbox = {**result, "kind": "evaluate"}, None

    if result["kind"] == "blank" and blank_bbox:
        bx1, _, bx2, _ = blank_bbox
        cx = (bx1 + bx2) / 2
        # The blank's underline may be written on; nothing else on the board may.
        lines = []
        for s in ink:
            if not s:
                continue
            a, b, c, d = stroke_bbox(s)
            flat = (c - a) > 20 and (d - b) < 0.3 * (c - a)
            in_blank = min(c, bx2 + 10) - max(a, bx1 - 10) >= 0.6 * (c - a)  # not the bar of a "+" beside it
            low = b >= y1 + 0.45 * eq_h and d <= y2 + 0.5 * eq_h  # underlines sit on the baseline
            if flat and in_blank and low:
                lines.append(s)
        line_top = min(stroke_bbox(s)[1] for s in lines) if lines else None
        clear = board.without(lines) if lines else board

        def blank_spots(s, bw, bh):
            if line_top is not None:
                on = (cx - bw / 2, line_top - s - 4, "on the blank line")
            else:
                on = (cx - bw / 2, y1 + (eq_h - s) / 2, "inside the blank")
            return [on, (cx - bw / 2, y2 + max(0.35 * s, placement.GAP), "just under the blank")]

        got = placement.place(clear, lambda s: handwriting.text_to_strokes(text, 0, 0, s), blank_spots,
                              placement.shrinking(size, steps=3, factor=0.8), spot_first=True)
        if clear is not board:
            board.add(got.strokes)
        return got.strokes, f"{got.where}, {got.size:.0f} px tall to match your writing"

    if result["kind"] == "evaluate" and not str(expression).strip().endswith("="):
        text = "=" + text  # person wrote "12+7" with no equals sign

    def spots(s, bw, bh):
        gap = placement.GAP
        right = (x2 + max(0.28 * s, gap), y1 + (eq_h - s) / 2, "right after the problem, on the same line",
                 0.6 * s)
        under_end = (x2 - bw, y2 + max(0.2 * s, gap), "just under the end of the problem")
        under = (x1, y2 + max(0.35 * s, gap), "underneath the equation")
        above = (x1, y1 - bh - max(0.35 * s, gap), "above the problem")
        left = (x1 - bw - max(0.4 * s, gap), y1 + (eq_h - s) / 2, "to the left of the problem")
        if result["kind"] == "solve":
            return [under, right, under_end, above, left]
        return [right, under_end, under, above, left]

    # Shrink a little to stay on the problem's line before moving somewhere else.
    got = placement.place(board, lambda s: answer_shape(text, s), spots,
                          placement.shrinking(size, steps=3, factor=0.8), spot_first=True)
    return got.strokes, f"{got.where}, {got.size:.0f} px tall to match your writing"


def clean_work(text) -> str:
    """A few plain lines of working. The robot writes these under the problem."""
    lines = []
    for line in str(text or "").replace("\\n", "\n").splitlines():
        line = " ".join(line.strip().split())
        if line:
            lines.append(line[:140])
    return "\n".join(lines[:5])


def with_final(work: str, answer: str) -> str:
    answer = str(answer or "").strip()
    if not work:
        return answer
    if not answer or answer in work:
        return work
    return f"{work}\n{answer}"


def write_steps(text, bbox, w, h):
    """Write every line of working under the problem, shrunk so none are cut off."""
    x1, _, _, y2 = bbox
    x = max(10, min(float(x1), w - 40))
    width = max(w - x - 12, 160)
    bottom = h - 8
    gap = text_writer.LINE_GAP
    count = max(len(text_writer.wrap(text, "print", 18, width)), 1)
    size = min(28, max(14, (bottom - 12) / (count * gap)))
    block = size * gap * count
    under = float(y2) + size * 0.35
    y = under if under + block <= bottom else max(8, bottom - block)
    strokes, written, dropped = text_writer.text_strokes(
        text, x, y, size, width, "print", bottom + 4)
    where = f"under the problem, {written} line(s) of working, {size:.0f} px tall"
    if dropped:
        where += f" ({dropped} line(s) didn't fit)"
    return strokes, where


def content_bbox(strokes):
    pts = [p for s in strokes for p in s]
    return stroke_bbox(pts) if pts else None


def locate_math_box(png, ai_bbox, strokes, w, h, single, pad):
    """Where the problem actually is, so the answer starts just after it."""
    if strokes and ai_bbox and not _loose_box(ai_bbox, w, h):
        refined = refine_bbox(ai_bbox, strokes, pad)
        if refined is not ai_bbox:
            return refined
    if strokes and single:
        drawn = content_bbox(strokes)
        if drawn:
            return drawn
    # One problem in a picture and nothing drawn on top: the ink on the picture is the problem.
    if not strokes and single:
        ink = vision.ink_bbox(png, None)
        if ink:
            return ink
    region = None
    if ai_bbox and not _loose_box(ai_bbox, w, h):
        x1, y1, x2, y2 = (float(v) for v in ai_bbox[:4])
        region = (x1 - pad, y1 - pad, x2 + pad, y2 + pad)
    ink = vision.ink_bbox(png, region)
    if single and ink is None:
        ink = vision.ink_bbox(png, None)
    if ink:
        return ink
    if strokes:
        drawn = content_bbox(strokes)
        if drawn:
            return drawn
    if ai_bbox and len(ai_bbox) >= 4:
        return tuple(float(v) for v in ai_bbox[:4])
    return (40.0, 40.0, 200.0, 100.0)


def image_covers_board(box, w, h) -> bool:
    """True when an uploaded picture already fills the whiteboard, so answers go on top of it."""
    if not box or len(box) < 4:
        return False
    x1, y1, x2, y2 = box
    return (x2 - x1) >= w * 0.9 and (y2 - y1) >= h * 0.9


def write_below(text, user_strokes, w, h, board, style="print", max_size=60):
    """Write text under the person's writing, shrinking it until it fits somewhere clear.
    When the picture already fills the board, write across the picture instead.
    Returns (strokes, size, lines written, where)."""
    box = content_bbox(user_strokes)
    rows = stroke_rows(user_strokes)
    line_h = min((r[3] - r[1]) for r in rows) if rows else 48
    size = min(max(line_h * 0.8, 28), max_size)
    x = 36 if box is None else max(10, box[0])

    def wrap_width(s):
        return max(w - x - 20, s * 3)

    def render(s):
        return text_writer.text_strokes(text, 0, 0, s, wrap_width(s), style)[0]

    def spots(s, bw, bh):
        if box is None:
            return [(x, max(10, h * 0.62), "across the picture")]
        gap = max(s * 0.6, placement.GAP)
        return [(x, box[3] + gap, "under your work"),
                (x, box[1] - bh - gap, "above your work"),
                (box[2] + max(s, placement.GAP), box[1], "to the right of your work")]

    got = placement.place(board, render, spots, placement.shrinking(size))
    written = len(text_writer.wrap(text, style, got.size, wrap_width(got.size))) if got.strokes else 0
    return got.strokes, got.size, written, got.where


def stroke_rows(user_strokes, gap=10):
    """Group strokes into lines of writing, top to bottom."""
    boxes = sorted((stroke_bbox(s) for s in user_strokes if s), key=lambda b: (b[1] + b[3]) / 2)
    rows = []
    for b in boxes:
        if rows and b[1] <= rows[-1][3] + gap and (b[1] + b[3]) / 2 <= rows[-1][3] + gap:
            r = rows[-1]
            rows[-1] = [min(r[0], b[0]), min(r[1], b[1]), max(r[2], b[2]), max(r[3], b[3])]
        else:
            rows.append(list(b))
    return rows


def picked_mode(ai, action: str) -> str:
    """What to do with a read. A missing mode is not a drawing, and a question is not a picture."""
    mode = str(ai.get("mode") or "").strip().lower()
    question = str(ai.get("question") or "").strip()
    if mode == "drawing" and action in ("answer", "work") and any(ch.isalpha() for ch in question):
        return "answer"
    if mode in ("math", "fill", "drawing", "answer", "hint", "check"):
        return mode
    if ai.get("problems") or str(ai.get("expression") or "").strip():
        return "math"
    if question or (action in ("answer", "work") and str(ai.get("answer") or "").strip()):
        return "answer"
    if str(ai.get("text") or "").strip():
        return "fill"
    return "drawing"


def find_blank(expression, bbox, user_strokes):
    """Where is the blank? Look for an underscore-like stroke near where "_" sits in the text."""
    x1, y1, x2, y2 = bbox
    text = expression.replace(" ", "")
    if "_" not in text and "?" not in text:
        return None
    idx = text.find("_") if "_" in text else text.find("?")
    guess_x = x1 + (idx + 0.5) / max(len(text), 1) * (x2 - x1)
    flat = []
    for s in user_strokes:
        if not s:
            continue
        a, b, c, d = stroke_bbox(s)
        inside = a >= x1 - 5 and c <= x2 + 5 and b >= y1 - 5 and d <= y2 + 5
        if inside and (c - a) > 20 and (d - b) < 0.3 * (c - a):
            flat.append((a, b, c, d))
    if flat:  # the underline closest to where the blank should be
        a, b, c, d = min(flat, key=lambda f: abs((f[0] + f[2]) / 2 - guess_x))
        return [a, y1, c, y2]
    half = (y2 - y1) * 0.35  # no underline drawn: aim for the gap itself
    return [guess_x - half, y1, guess_x + half, y2]


def _separate_roots(answer: str) -> str:
    """x=-1,3 is two roots. Written that way it looks like the single number -13."""
    match = re.fullmatch(r"\s*([A-Za-z])\s*=\s*(.+?)\s*", answer)
    if not match:
        return answer
    var, rest = match.group(1), match.group(2)
    parts = [p.strip() for p in re.split(r"\s*,\s*|\s*;\s*|\s+\bor\b\s+", rest) if p.strip()]
    if len(parts) < 2 or any("=" in p for p in parts):
        return answer
    return " or ".join(f"{var}={p}" for p in parts)


def solved_math(prob, expression) -> dict:
    """SymPy first. The model's own answer is only a fallback when the transcription will not parse."""
    try:
        result = math_solver.solve(expression)
    except Exception:
        return math_from_ai(prob, expression)
    result["answer"] = _separate_roots(result["answer"])
    result["local"] = True
    return result


def local_check(prob) -> dict | None:
    """Mark the student's line with SymPy. None means the transcription could not be checked."""
    expression = str(prob.get("expression") or "").strip()
    equation = str(prob.get("equation") or "").strip()
    student = str(prob.get("student_answer") or "").strip()
    if student.lower() in ("null", "none"):
        student = ""
    if not expression and not equation:
        return None
    try:
        return math_solver.check_work(
            expression=expression or None,
            equation=equation or None,
            student_answer=student or None,
        )
    except Exception:
        return None


def math_from_ai(prob, expression) -> dict:
    """The solution the model computed. Kind only decides where the robot writes it."""
    answer = str(prob.get("answer") or "").strip()
    if not answer or answer.lower() in ("null", "none"):
        raise ValueError("no answer")
    kind = str(prob.get("kind") or "").strip().lower()
    if kind not in ("evaluate", "solve", "blank", "check"):
        if prob.get("blank_bbox") or "_" in expression:
            kind = "blank"
        elif re.search(r"[a-zA-Z]", re.sub(r"(\d)\s*[xX]\s*(\d)", r"\1*\2", expression)):
            kind = "solve"
        else:
            kind = "evaluate"
    return {"kind": kind, "answer": _separate_roots(answer)}


def math_category(expression, kind) -> tuple[str, str]:
    if kind == "solve":
        body = expression.lower().replace(" ", "")
        if "^2" in body or "**2" in body or "²" in body:
            detail = "quadratic equation"
        elif "^3" in body or "**3" in body or "³" in body:
            detail = "cubic equation"
        else:
            detail = "equation"
        return "Algebra", detail
    if kind == "blank":
        return "Fill in the blank", "a missing number"
    if kind == "check":
        return "Arithmetic", "checking a finished equation"
    if "/" in expression:
        return "Arithmetic", "fractions"
    return "Arithmetic", "a calculation"


def collect_problems(ai, user_strokes):
    """One entry per separate problem, even if the AI squashed several into one string."""
    everything = content_bbox(user_strokes) or (40, 40, 400, 120)
    problems = []
    raw = ai.get("problems") or [{"expression": ai.get("expression", ""),
                                  "bbox": ai.get("bbox"), "blank_bbox": ai.get("blank_bbox")}]
    for prob in raw:
        if not isinstance(prob, dict):
            continue
        parts = [p.strip() for p in re.split(r"[;\n]+", str(prob.get("expression", ""))) if p.strip()]
        if len(parts) <= 1:
            if parts:
                problems.append({"expression": parts[0], "answer": prob.get("answer"), "kind": prob.get("kind"),
                                 "work": prob.get("work"),
                                 "bbox": prob.get("bbox") or everything, "blank_bbox": prob.get("blank_bbox")})
            continue
        # Several problems in one string: match each to a line of writing, top to bottom
        rows = stroke_rows(user_strokes)
        for i, part in enumerate(parts):
            box = rows[i] if len(rows) == len(parts) else (prob.get("bbox") or everything)
            problems.append({"expression": part, "answer": prob.get("answer") if len(parts) == 1 else None,
                             "kind": prob.get("kind"), "work": prob.get("work") if i == 0 else None,
                             "bbox": box, "blank_bbox": None})
    return problems


# ---------- API ----------

def robot_summary(strokes, w, h) -> str:
    """Estimate what the real plotter will do (same maths as gcode.py and the simulator)."""
    mm = 800 / w  # board is 800 mm wide
    ordered = gcode.order_strokes(strokes)
    ink = sum(math.dist(a, b) for s in ordered for a, b in zip(s, s[1:])) * mm
    travel, pos = 0.0, (0.0, float(h))
    for s in ordered:
        travel += math.dist(pos, s[0]) * mm
        pos = s[-1]
    travel += math.dist(pos, (0.0, float(h))) * mm
    seconds = ink / 50 + travel / 120 + len(ordered) * 0.3
    return (f"{len(ordered)} stroke(s), ordered nearest-first to cut travel. About {ink:.0f} mm with the pen down "
            f"and {travel:.0f} mm of pen-up travel, roughly {seconds:.0f} s on the real robot.")


_REFLECT = re.compile(
    r"reflect|mirror|both side|other side|other half|flip|right side|left side|\bface\b",
    re.I,
)


def _reflected(req: CompleteRequest):
    """Flip the ink across its right edge, so the copy sits on the right."""
    log = []
    raw = shapes.build_strokes(
        {"shapes": [{"type": "mirror", "strokes": "all", "axis": "vertical"}]},
        req.strokes, log, (req.width, req.height))
    return clean_strokes(raw, req.width, req.height), shapes.highlight_ids(), log


def _plain(text) -> str:
    """Math symbols as words, so the voice reads the working instead of the signs."""
    t = " ".join(str(text or "").split())
    for src, dst in (
        ("**", " to the power of "),
        ("^", " to the power of "),
        ("×", " times "),
        ("÷", " divided by "),
        ("*", " times "),
        ("/", " divided by "),
        ("+", " plus "),
        ("=", " equals "),
        ("−", " minus "),
        ("–", " minus "),
    ):
        t = t.replace(src, dst)
    t = re.sub(r"(?<=\w)\s+x\s+(?=\w)", " times ", t, flags=re.I)
    t = re.sub(r"(?<=\d)\s*-\s*(?=\d)", " minus ", t)
    return re.sub(r"\s+", " ", t).strip(" .")


def explain_math(expression, answer, work="") -> str:
    """A short spoken walkthrough: the problem, the steps, then the answer."""
    problem = _plain(expression)
    final = _plain(answer)
    bits = [f"The problem is {problem}."] if problem else []
    steps = []
    seen = {problem.lower(), final.lower(), f"{problem} equals {final}".lower()}
    for line in str(work or "").splitlines():
        said = _plain(line)
        if said and said.lower() not in seen:
            seen.add(said.lower())
            steps.append(said)
    if steps:
        bits.append("Here is how to get there. " + ". ".join(steps) + ".")
    if final:
        bits.append(f"The answer is {final}.")
    return " ".join(bits)


def explain_answer(question, answer, reasoning="") -> str:
    q = " ".join(str(question or "").split())
    a = " ".join(str(answer or "").split())
    why = " ".join(str(reasoning or "").split())
    bits = []
    if q:
        bits.append(q if q.endswith((".", "?", "!")) else f"{q}.")
    if why and why.lower().rstrip(".") not in a.lower():
        bits.append(why if why.endswith((".", "?", "!")) else f"{why}.")
    if a:
        bits.append(f"The answer is {a}.")
    return " ".join(bits)


def explain_drawing(description, plan="") -> str:
    desc = " ".join(str(description or "this").split()) or "this"
    idea = " ".join(str(plan or "").split())
    line = f"I'm drawing {desc}."
    if idea and idea.lower() not in desc.lower():
        line += " " + (idea if idea.endswith((".", "?", "!")) else f"{idea}.")
    return line


def _say(result: dict) -> str:
    """The script ElevenLabs reads while the robot draws."""
    custom = " ".join(str(result.get("say") or "").split())
    if custom:
        return custom[:800]
    mode = str(result.get("mode") or "")
    if mode == "drawing":
        return explain_drawing(result.get("description"), result.get("plan"))[:800]
    if mode in ("text", "trace", ""):
        return ""
    answer = " ".join(str(result.get("answer") or "").split())
    if not answer:
        return ""
    if mode == "math":
        return explain_math(result.get("expression"), answer)[:800]
    if mode == "hint" and not answer.lower().startswith("hint"):
        return f"Hint. {answer}"[:800]
    if mode == "answer":
        return explain_answer(result.get("description"), answer)[:800]
    return answer[:800]


def _with_voice(result: dict) -> dict:
    line = _say(result)
    if not line:
        return result
    result["transcript"] = line
    audio = speech.synthesize(line)
    if audio:
        result["speech"] = "data:audio/mpeg;base64," + base64.b64encode(audio).decode()
    result.setdefault("steps", []).append({
        "title": "Explaining the answer",
        "detail": line,
    })
    return result


@app.post("/api/complete")
def complete(req: CompleteRequest):
    started = time.perf_counter()
    result = _complete(req)
    boards.remember(result, source="complete", action=req.action, provider=req.provider,
                    user_strokes=req.strokes, seconds=time.perf_counter() - started)
    return _with_voice(result)


def _complete(req: CompleteRequest):
    data = req.image.split(",", 1)[1] if req.image.startswith("data:") else req.image
    try:
        png = base64.b64decode(data)
    except Exception:
        raise HTTPException(400, "Image is not valid base64.")
    if not req.strokes and not req.image_box:
        raise HTTPException(400, "The board is empty. Draw something or add an image first.")
    # Layout (where answers go) uses the ink AND the uploaded picture, as if the picture were writing
    layout = list(req.strokes)
    # A picture that fills the board is the board. Don't treat its outline as writing,
    # or answers get pushed into a margin that isn't there. They are drawn on the picture.
    covers = bool(req.image_box) and image_covers_board(req.image_box, req.width, req.height)
    if req.image_box and not covers:
        bx1, by1, bx2, by2 = req.image_box
        layout.append([[bx1, by1], [bx2, by1], [bx2, by2], [bx1, by2]])
    # Where ink already is, so nothing the robot writes lands on top of it
    board = placement.Board(req.width, req.height, req.strokes, png, req.image_box, image_is_board=covers)

    steps = []  # the robot's thought process, shown in the frontend

    def step(title, detail):
        if detail:
            steps.append({"title": title, "detail": str(detail)})

    def identify(category, why):
        """Put the problem type right after the first step, where it's easy to see."""
        plain = category["type"] in ("Question", "Drawing", "Pattern", "Number pattern", "Text", "Answer", "Hint", "Check")
        steps.insert(1, {"title": f"Identified: {category['type']}{'' if plain else ' problem'}",
                         "detail": f"{category['detail'][:1].upper()}{category['detail'][1:]}. {why}"})

    what = f"your {len(req.strokes)} stroke(s)" + (" and your uploaded image" if req.image_box else "")
    if not req.strokes:
        what = "your uploaded image"
    if req.action == "drawing":
        req.mode = "drawing"
    note = req.instruction.strip()[:400]
    if req.action == "cv":
        if not req.strokes:
            raise HTTPException(422, "Draw something first. Computer vision finishes lines that are already on the board.")
        step("Looked at the board", f"Measured your {len(req.strokes)} stroke(s) with OpenCV. This mode does not call the AI.")
        started_cv = time.perf_counter()
        raw, cv_notes = cv_finish.finish(req.strokes, req.width, req.height)
        strokes = clean_strokes(raw, req.width, req.height)
        step("Computer vision", f"Finished the geometry pass in {time.perf_counter() - started_cv:.2f} s.")
        for line in cv_notes:
            step("What CV found", line)
        if not strokes:
            raise HTTPException(422, "Computer vision didn't find a part to finish. Draw more of the outline, like most of a circle or three sides of a box.")
        category = {"type": "Drawing", "detail": "the unfinished part of your drawing"}
        identify(category, "Computer vision completed the missing geometry. No model was asked.")
        step("Planned the robot", robot_summary(strokes, req.width, req.height))
        return {"mode": "drawing", "description": "the unfinished part of your drawing",
                "plan": "Complete partial curves, close gaps, and draw a missing side.",
                "strokes": strokes, "highlight": [],
                "checks": 0, "steps": steps, "category": category}
    if req.action == "drawing" and _REFLECT.search(note):
        if not req.strokes:
            raise HTTPException(422, "Draw the side you want reflected first.")
        step("Looked at the board", f"Measured your {len(req.strokes)} stroke(s). "
                                    "The reflection is computed from those coordinates.")
        step("Your instruction", f"\"{note}\"")
        strokes, highlight, log = _reflected(req)
        if not strokes:
            raise HTTPException(422, "Those lines are too small to reflect. Draw the side a bit larger.")
        for line in log:
            step("Reflected it", line)
        description = "a reflection of your drawing"
        category = {"type": "Drawing", "detail": description}
        identify(category, "You asked for a reflection, so the green strokes are copied from their right edge out to the right.")
        step("Planned the robot", robot_summary(strokes, req.width, req.height))
        return {"mode": "drawing", "description": description,
                "plan": "Reflect the ink across its right edge, onto the right.",
                "strokes": strokes, "highlight": highlight, "steps": steps, "category": category}

    fast = req.action in ("answer", "work", "hint", "check")
    remembered = student_memory.hint_note() if req.action == "hint" else ""
    if fast:
        step("Looked at the board", f"Sent one close-up of {what} to {vision.fast_provider_name(req.provider)}. "
                                    "The model only reads the handwriting. Math is solved on this computer.")
    else:
        step("Looked at the board", f"Sent a picture of {what} with a coordinate grid "
                                    f"and each stroke's exact points to {vision.provider_name(req.provider)}.")
    looked = steps[-1]
    if req.action == "drawing" and note:
        step("Your instruction", f"\"{note}\"")
    started = time.perf_counter()
    try:
        if fast:
            ai = vision.read_fast(png, req.strokes, req.action, req.image_box, req.provider, remembered)
        else:
            ai = vision.analyze_board(png, shapes.summarize_strokes(req.strokes), req.strokes, req.mode, req.action,
                                      req.image_box, req.provider, note if req.action == "drawing" else "")
    except RuntimeError as e:
        raise HTTPException(500, str(e))
    except Exception as e:
        raise HTTPException(502, f"AI request failed: {e}")
    looked["detail"] += f" It answered in {time.perf_counter() - started:.1f} s."
    if remembered:
        step("Remembered this student", remembered)

    mode = picked_mode(ai, req.action)
    description = ai.get("description", "")
    names = {"math": "math", "fill": "pattern", "drawing": "drawing", "answer": "a question",
             "hint": "a hint", "check": "checking work"}
    buttons = {"answer": "Answer", "work": "Answer but show work", "hint": "Hint",
               "check": "Check my work", "drawing": "Finish drawing"}
    if req.action in ("hint", "check"):
        step("Decided what to do", f"You pressed {buttons[req.action]}.")
        mode = req.action
    elif req.mode in ("math", "fill", "drawing"):
        if mode != req.mode:
            # The person chose the mode, so that wins over the AI's guess
            if req.mode == "math" and not (ai.get("problems") or ai.get("expression")):
                raise HTTPException(422, "Couldn't read a math problem there. Try writing it bigger and clearer.")
            if req.mode == "fill" and not ai.get("text"):
                raise HTTPException(422, "Couldn't find a pattern to continue. Try adding another item or two.")
            step("Decided what to do", f"You asked for {names[req.mode]}. The AI thought it was "
                                       f"{names.get(mode, mode)}, but your choice wins.")
            mode = req.mode
        else:
            step("Decided what to do", f"You pressed {buttons.get(req.action, names[mode])}, and the AI agreed "
                                       f"it's {names[mode]}.")
    else:
        pressed = f"You pressed {buttons[req.action]}. " if req.action in buttons else "Auto mode: "
        step("Decided what to do", f"{pressed}{'The' if pressed.startswith('You') else 'the'} AI classified this as {names.get(mode, mode)}.")
        # Safety net: "1+1" looks like the letter H, so the AI may call math a drawing.
        # If it also read the strokes as math, math wins, and its own solution is what gets written.
        reading = str(ai.get("math_reading") or "").strip()
        if (mode != "math" and not (mode == "drawing" and req.image_box)
                and reading and reading.lower() not in ("null", "none") and any(ch.isdigit() for ch in reading)):
            step("Changed its mind", f"The strokes also read as  {reading}  which is math. "
                                    "Handwritten math often looks like letters (1+1 looks like H), "
                                    "so treating it as math instead.")
            ai.setdefault("problems", [{"expression": reading, "answer": ai.get("math_answer"),
                                        "kind": ai.get("math_kind") or "evaluate"}])
            mode = "math"
        if req.action in ("answer", "work") and mode == "drawing" and req.image_box:
            step("Read it again", "The first pass called the photo a drawing. "
                                  "Reading the writing again, and keeping periods and question marks.")
            started_retry = time.perf_counter()
            try:
                ai = vision.read_fast(
                    png, req.strokes, req.action, req.image_box, req.provider,
                    note="The previous read was wrong. This is handwriting. Keep every period and the dot "
                         "under each question mark. Respond with mode math or mode answer, never drawing.")
            except RuntimeError as e:
                raise HTTPException(500, str(e))
            except Exception as e:
                raise HTTPException(502, f"AI request failed: {e}")
            steps[-1]["detail"] += f" The second read answered in {time.perf_counter() - started_retry:.1f} s."
            mode = picked_mode(ai, req.action)
            description = ai.get("description", description)
        if req.action in ("answer", "work") and mode == "drawing":
            raise HTTPException(422, "I couldn't read a problem or question there. "
                                     "For a photo, press CV finish so only the whiteboard shows, then press Answer.")
    step("What it sees", description)

    if mode == "math":
        step("AI's notes", ai.get("reasoning"))
        problems = collect_problems(ai, layout)
        strokes, answers, expressions, failed, types, scripts = [], [], [], [], [], []
        rows = stroke_rows(req.strokes) if req.strokes else []
        pad = 30 if len(problems) == 1 else 12  # tighter when problems sit close together
        for n, prob in enumerate(problems, 1):
            expression = prob["expression"]
            if len(rows) == len(problems):
                prob["bbox"] = rows[n - 1]
            label = f"Problem {n}" if len(problems) > 1 else "The problem"
            try:
                result = solved_math(prob, expression)
            except Exception:
                failed.append(expression)
                step(f"{label}: couldn't solve", f"Read it as  {expression}  but the AI did not give a solution. Skipping it.")
                continue
            bbox = locate_math_box(png, prob.get("bbox"), req.strokes, req.width, req.height,
                                   len(problems) == 1, pad)
            blank = prob.get("blank_bbox")
            if result["kind"] == "blank" and not blank:
                blank = find_blank(expression, bbox, req.strokes)
            ai_box = prob.get("bbox")
            near_y = ((float(ai_box[1]) + float(ai_box[3])) / 2
                      if ai_box and not _loose_box(ai_box, req.width, req.height) else None)
            if req.action == "work":
                local = ""
                try:
                    local = math_solver.show_work(expression)
                except Exception:
                    local = ""
                shown = with_final(local or clean_work(prob.get("work")), result["answer"])
                new, where = write_steps(shown, bbox, req.width, req.height)
                if new:
                    board.add(new)
                else:
                    new, where = place_answer(expression, result, bbox, req.width, req.height, board, blank,
                                              req.strokes, near_y)
                who = "solved it here" if result.get("local") else "the AI solved it"
                detail = f"Read  {expression}  and {who}: {result['answer']}. Writing the working {where}."
                if shown:
                    detail += "\n" + shown
            else:
                new, where = place_answer(expression, result, bbox, req.width, req.height, board, blank,
                                          req.strokes, near_y)
                who = "solved it here" if result.get("local") else "the AI solved it"
                detail = f"Read  {expression}  and {who}: {result['answer']}. Writing it {where}."
            strokes += new
            types.append(math_category(expression, result["kind"]))
            answers.append(result["answer"])
            expressions.append(expression)
            spoken_work = ""
            try:
                spoken_work = math_solver.show_work(expression)
            except Exception:
                spoken_work = ""
            scripts.append(explain_math(expression, result["answer"], spoken_work))
            step(f"{label}", detail)
        if not strokes:
            raise HTTPException(422, f"Read the math as {', '.join(failed) or 'nothing'} but the AI didn't give a solution. "
                                     "Try writing it more clearly.")
        kinds_found = sorted({t for t, _ in types})
        if len(types) == 1:
            category = {"type": types[0][0], "detail": types[0][1]}
        elif len(kinds_found) == 1:
            category = {"type": kinds_found[0], "detail": f"{len(types)} problems"}
        else:
            category = {"type": "Mixed math", "detail": ", ".join(f"{d} ({t.lower()})" for t, d in types)}
        identify(category, "Solved here: "
                 + "; ".join(f"{e} → {a}" for e, a in zip(expressions, answers)) + ".")
        step("Planned the robot", robot_summary(strokes, req.width, req.height))
        return {"mode": "math", "description": description, "expression": ";  ".join(expressions),
                "answer": ", ".join(answers), "say": " ".join(scripts),
                "strokes": strokes, "steps": steps, "category": category}

    if mode == "fill":
        text = str(ai.get("text", "")).strip()
        if not text:
            raise HTTPException(422, "The AI couldn't figure out what comes next.")
        box = content_bbox(layout) or (40, 40, 200, 90)
        try:
            x, y, size = (float(v) for v in ai.get("position", [])[:3])
        except (TypeError, ValueError):
            x, y, size = box[2] + 20, box[1], max(box[3] - box[1], 28)
        step("Found the rule", ai.get("reasoning"))
        size = min(max(size * 0.85, 28), 160)

        def fill_spots(s, bw, bh):
            gap = placement.GAP
            return [(x, y, "where the next item goes"),
                    (box[2] + max(0.4 * s, gap), box[1], "right after the last item"),
                    (box[0], box[3] + max(0.5 * s, gap), "on the next line")]

        got = placement.place(board, lambda s: text_writer.text_strokes(text, 0, 0, s, 10 ** 6, "print")[0],
                              fill_spots, placement.shrinking(size, steps=3, factor=0.8), spot_first=True)
        strokes = got.strokes
        if req.action == "work":
            rule = clean_work(ai.get("work"))
            if rule:
                extra, _, _, _ = write_below(rule, layout, req.width, req.height, board)
                strokes += extra
                step("Showed the rule", rule)
        step("Chose what to write", f"Writing {text} next in the sequence {got.where}, {got.size:.0f} px tall to match.")
        numeric = any(ch.isdigit() for ch in text)
        category = {"type": "Number pattern" if numeric else "Pattern", "detail": description or "a sequence to continue"}
        identify(category, "It's a sequence, so the next items follow a rule instead of an equation.")
        step("Planned the robot", robot_summary(strokes, req.width, req.height))
        why = " ".join(str(ai.get("reasoning") or "").split())
        said = f"{why.rstrip('.')}. So the next part is {_plain(text)}." if why else f"The sequence continues with {_plain(text)}."
        return {"mode": "fill", "description": description, "answer": text, "say": said,
                "strokes": strokes, "steps": steps, "category": category}

    if mode == "hint":
        hint = str(ai.get("hint") or ai.get("answer") or "").strip()
        if not hint:
            raise HTTPException(422, "Couldn't think of a hint for this. Try writing a bit more.")
        step("Thought about the next step", ai.get("reasoning"))
        text = hint if hint.lower().startswith("hint") else f"Hint: {hint}"
        strokes, size, written, where = write_below(text, layout, req.width, req.height, board, "cursive")
        if not strokes:
            raise HTTPException(422, "There's no room left on the board for a hint.")
        step("Wrote a hint", f"\"{text}\" {where} in cursive, without giving away the answer.")
        category = {"type": "Hint", "detail": description or "a nudge toward the next step"}
        identify(category, "You asked for a hint, so it points you to the next step instead of solving it.")
        step("Planned the robot", robot_summary(strokes, req.width, req.height))
        why = " ".join(str(ai.get("reasoning") or "").split())
        said = f"Hint. {why.rstrip('.')}. {hint}." if why else f"Hint. {hint}."
        return {"mode": "hint", "description": description, "answer": text, "say": said,
                "strokes": strokes, "steps": steps, "category": category}

    if mode == "check":
        step("AI's notes", ai.get("reasoning"))
        problems = [p for p in (ai.get("problems") or []) if isinstance(p, dict)]
        if not problems:
            raise HTTPException(422, "I couldn't find any problems with answers to check.")
        everything = content_bbox(layout) or (40, 40, req.width - 40, 120)
        strokes, right, wrong, empty = [], 0, 0, 0
        said, notes = [], []
        rows = stroke_rows(req.strokes) if req.strokes else []
        pad = 30 if len(problems) == 1 else 12
        for n, prob in enumerate(problems, 1):
            if len(rows) == len(problems):
                prob["bbox"] = rows[n - 1]
            label = f"Problem {n}" if len(problems) > 1 else "Your problem"
            shown = prob.get("expression") or f"{prob.get('equation')}  ->  {prob.get('student_answer') or '(no answer)'}"
            judged = local_check(prob)
            if judged:
                correct, fix = judged["correct"], str(judged.get("correct_answer") or "").strip()
            else:
                correct = prob.get("correct")
                if isinstance(correct, str):
                    correct = {"true": True, "yes": True, "false": False, "no": False}.get(correct.strip().lower())
                if correct is None and str(prob.get("correct") or "").strip().lower() in ("null", "none", ""):
                    correct = None
                fix = str(prob.get("correct_answer") or "").strip()
            x1, y1, x2, y2 = refine_bbox(prob.get("bbox") or everything, req.strokes, pad)
            size = min(max((y2 - y1) * 0.85, 28), 120)
            if correct is None:
                empty += 1
                step(label, f"Read  {shown}  but there's no answer written yet, so nothing to mark.")
                continue
            has_fix = bool(fix) and fix.lower() not in ("null", "none")

            def mark(s, correct=correct, fix=fix, has_fix=has_fix):
                """A check, or an X with the right answer beside it, drawn at the origin."""
                if correct:
                    return handwriting.text_to_strokes("✓", 0, 0, s)
                cross = handwriting.text_to_strokes("✗", 0, 0, s)
                if not has_fix:
                    return cross
                cap = s * 0.55
                return cross + text_writer.text_strokes(fix, s * 0.95, (s - cap) / 2, cap, 10 ** 6, "print")[0]

            def mark_spots(s, bw, bh, x1=x1, y1=y1, x2=x2, y2=y2):
                gap = placement.GAP
                return [(x2 + max(s * 0.4, gap), y1 + (y2 - y1 - s) / 2, "beside it", 0.6 * s),
                        (x2 - bw, y2 + max(0.2 * s, gap), "just under the end of it"),
                        (x1 - bw - max(s * 0.4, gap), y1 + (y2 - y1 - s) / 2, "to the left of it"),
                        (x1, y1 - bh - max(0.3 * s, gap), "above it")]

            got = placement.place(board, mark, mark_spots, placement.shrinking(size, steps=3, factor=0.8),
                                  spot_first=True)
            strokes += got.strokes
            if correct:
                right += 1
                said.append(f"{shown} is correct.")
                step(label, f"Read  {shown}  and checked it here: correct. Marked it with a check {got.where}.")
            else:
                wrong += 1
                bug = math_solver.explain_mistake(
                    expression=str(prob.get("expression") or "").strip() or None,
                    equation=str(prob.get("equation") or "").strip() or None,
                    student_answer=str(prob.get("student_answer") or "").strip() or None,
                    correct_answer=fix,
                )
                notes.append(bug["note"])
                fact = f"The student wrote {shown} and it was wrong. {bug['detail']}"
                if student_memory.remember(fact):
                    step("Remembered the mistake", "Stored it in Backboard so the next hint can bring it up.")
                step(label, f"{bug['detail']} Marked it with an X{' and the correct answer' if has_fix else ''}, {got.where}.")
                said.append(bug["detail"])
        total = right + wrong
        if not total:
            raise HTTPException(422, "I didn't find any finished answers to check. Write your answer after the = sign.")
        if notes:
            extra, _, _, where = write_below("\n".join(notes), layout, req.width, req.height, board)
            strokes += extra
            step("Pointed out the error", f"Wrote the mistake {where}:\n" + "\n".join(notes))
        category = {"type": "Check", "detail": f"{right} of {total} correct" + (f", {empty} not answered yet" if empty else "")}
        identify(category, "You asked to check your work, so each wrong answer gets the mistake and how to fix it.")
        step("Planned the robot", robot_summary(strokes, req.width, req.height))
        return {"mode": "check", "description": description, "answer": category["detail"],
                "say": " ".join(said)[:800], "strokes": strokes,
                "steps": steps, "category": category}

    if mode == "answer":
        question = str(ai.get("question") or description or "your question")
        step("Read the question", f"\"{question}\"")
        step("How it answered", ai.get("reasoning"))
        answer = str(ai.get("answer", "")).strip()
        if not answer:
            raise HTTPException(422, "The AI didn't come up with an answer. Try writing the question more clearly.")
        if req.action == "work":
            local = ""
            try:
                local = math_solver.show_work(question)
            except Exception:
                local = ""
            answer_text = with_final(local or clean_work(ai.get("work")), answer)
        else:
            answer_text = answer
        ink = vision.ink_bbox(png, None) if not req.strokes and req.action != "work" else None
        if ink and re.fullmatch(r"=?\s*[\d./+\-xXi]+", answer.replace(" ", "")):
            shown = answer[1:].strip() if answer.startswith("=") else answer
            strokes, where = place_answer("problem", {"kind": "evaluate", "answer": shown}, ink,
                                          req.width, req.height, board)
            if strokes:
                step("Wrote the answer", f"\"{answer}\" {where}.")
                category = {"type": "Question", "detail": question}
                identify(category, "The answer is written on the same line as the problem in the picture.")
                step("Planned the robot", robot_summary(strokes, req.width, req.height))
                return {"mode": "answer", "description": question, "answer": answer,
                        "say": explain_answer(question, answer, ai.get("reasoning")),
                        "strokes": strokes, "steps": steps, "category": category}
        strokes, size, written, where = write_below(answer_text, layout, req.width, req.height, board)
        if not strokes:
            raise HTTPException(422, "There's no room left on the board for the answer.")
        wrote = "the working" if req.action == "work" else f"\"{answer}\""
        step("Wrote the answer", f"{wrote} {where.replace('your work', 'your question')}, "
                                 f"{size:.0f} px tall, in {written} line(s).")
        category = {"type": "Question", "detail": question}
        identify(category, "It's written words asking something, so the robot answers it instead of solving or drawing.")
        step("Planned the robot", robot_summary(strokes, req.width, req.height))
        return {"mode": "answer", "description": question, "answer": answer,
                "say": explain_answer(question, answer_text if req.action == "work" else answer, ai.get("reasoning")),
                "strokes": strokes, "steps": steps, "category": category}

    step("Made a plan", ai.get("plan"))
    log = []
    board = (req.width, req.height)
    strokes = clean_strokes(shapes.build_strokes(ai, req.strokes, log, board), req.width, req.height)
    highlight = shapes.highlight_ids()
    checks = 0
    # Two checklist passes. A yes on the first one does not skip the second.
    check_limit = 2
    if not strokes and req.strokes:
        step("First version", "The first shapes produced no lines"
             + (": " + " ".join(log) if log else ".")
             + " Sending the board back and asking again.")
    if req.review and (strokes or req.strokes):
        summary = shapes.summarize_strokes(req.strokes)
        previous = ai
        sure = False
        for n in range(1, check_limit + 1):
            checks = n
            started = time.perf_counter()
            try:
                fix = vision.review_drawing(png, req.strokes, strokes, summary, previous, req.provider,
                                            n, check_limit)
            except Exception as e:
                step(f"Check {n} of {check_limit}", f"This check failed ({e}). Asking again.")
                continue
            took = f"{time.perf_counter() - started:.1f} s"
            verdict = (fix.get("review") or "").strip()
            rows = fix.get("checklist") if isinstance(fix.get("checklist"), list) else []
            listed = []
            for row in rows:
                if not isinstance(row, dict):
                    continue
                flag = row.get("pass")
                passed = flag is True or (isinstance(flag, str) and flag.strip().lower() in ("true", "yes", "pass"))
                item = str(row.get("item") or "Item").strip()
                note_text = str(row.get("note") or "").strip()
                listed.append(f"{'PASS' if passed else 'FAIL'} — {item}" + (f": {note_text}" if note_text else ""))
            if not listed:
                listed.append("FAIL — Checklist: the model did not answer the five items.")
            ok = fix.get("ok")
            sure = (ok is True or (isinstance(ok, str) and ok.strip().lower() in ("true", "yes"))) and strokes
            sure = sure and listed and all(line.startswith("PASS") for line in listed)
            report = "\n".join(listed)
            if verdict:
                report += f"\n{verdict}"
            if sure:
                step(f"Check {n} of {check_limit}", f"Yes ({took}).\n{report}")
                continue
            sure = False
            fixed_log = []
            fixed = clean_strokes(shapes.build_strokes(fix, req.strokes, fixed_log, board), req.width, req.height)
            if fixed:
                strokes, log = fixed, fixed_log
                highlight = shapes.highlight_ids()
                previous = fix
                step(f"Check {n} of {check_limit}", f"No ({took}). Applied the changes.\n{report}")
            else:
                step(f"Check {n} of {check_limit}", f"No ({took}). Kept this version.\n{report}")
        step("Check count", f"{checks} checks. " + ("The last checklist passed." if sure else "The last checklist did not pass."))
    elif not strokes and req.strokes and _REFLECT.search(note):
        strokes, highlight, reflect_log = _reflected(req)
        log = reflect_log or log
    for line in log:
        step("Cleaned up the drawing", line)
    if not strokes:
        why = " ".join(log) if log else "The shapes did not turn into any lines."
        raise HTTPException(422, f"The AI couldn't figure out how to finish this drawing. {why}")
    category = {"type": "Drawing", "detail": description or "a picture to finish"}
    identify(category, "It's a picture rather than numbers or symbols, so it gets finished with shapes.")
    step("Planned the robot", robot_summary(strokes, req.width, req.height))
    return {"mode": "drawing", "description": description, "plan": ai.get("plan", ""),
            "say": explain_drawing(description, ai.get("plan", "")),
            "strokes": strokes, "highlight": highlight, "checks": checks, "steps": steps, "category": category}


@app.post("/api/write")
def write_text(req: WriteRequest):
    started = time.perf_counter()
    result = _write_text(req)
    boards.remember(result, source="write", seconds=time.perf_counter() - started)
    return result


def _write_text(req: WriteRequest):
    text = req.text.strip()[:400]
    if not text:
        raise HTTPException(400, "Type something for the robot to write first.")
    size = min(max(req.size, 16), 200)
    x = min(max(req.x, 10), req.width - size)
    y = min(max(req.y, 10), req.height - size * 1.4)
    max_width = max(req.width - x - 20, size * 3)
    style = req.style if req.style in text_writer.STYLES else "print"
    strokes, written, dropped = text_writer.text_strokes(text, x, y, size, max_width, style, req.height - 10)
    if not strokes:
        raise HTTPException(422, "There's no room to write there. Try clicking higher up on the board.")
    steps = [
        {"title": "Got your text", "detail": f"{len(text)} characters to write: \"{text[:80]}{'…' if len(text) > 80 else ''}\""},
        {"title": "Chose a pen font", "detail": f"{'Cursive' if style == 'cursive' else 'Print'} single-stroke plotter font, so every letter "
                                                f"is drawn with pen lines instead of filled shapes. Capitals {size:.0f} px tall."},
        {"title": "Laid out the lines", "detail": f"Wrapped into {written} line(s) to fit the board width"
                                                  + (f"; {dropped} line(s) didn't fit and were left off" if dropped else "") + "."},
        {"title": "Planned the robot", "detail": robot_summary(strokes, req.width, req.height)},
    ]
    return {"mode": "text", "description": "your text", "answer": text, "strokes": strokes, "steps": steps,
            "category": {"type": "Text", "detail": f"{style} writing, {written} line(s)"}}


@app.post("/api/ask")
def ask(req: AskRequest):
    started = time.perf_counter()
    result = _ask(req)
    boards.remember(result, source="ask", provider=req.provider, user_strokes=req.strokes,
                    heard=req.prompt, seconds=time.perf_counter() - started)
    return _with_voice(result)


def _ask(req: AskRequest):
    prompt = req.prompt.strip()[:500]
    if not prompt:
        raise HTTPException(400, "Type a question or instruction first.")
    data = req.image.split(",", 1)[1] if req.image.startswith("data:") else req.image
    png = base64.b64decode(data)
    size = min(max(req.size, 16), 200)
    x = min(max(req.x, 10), req.width - size)
    y = min(max(req.y, 10), req.height - size * 1.4)
    style = req.style if req.style in text_writer.STYLES else "print"
    wrap_w = max(req.width - x - 20, size * 3)

    # Keep the person's prompt on the board (in their ink), and put the answer underneath it
    prompt_strokes, prompt_lines, _ = text_writer.text_strokes(prompt, x, y, size, wrap_w, "print", req.height - 10)
    ay = y + prompt_lines * size * text_writer.LINE_GAP + size * 0.35 if prompt_lines else y
    ay = min(ay, req.height - size * 1.4)
    box = [x, ay, min(req.width - 10, x + 420), min(req.height - 10, ay + 300)]
    steps = [{"title": "Your prompt", "detail": f"\"{prompt}\" (kept on the board in your ink)"}]

    started = time.perf_counter()
    try:
        ai = vision.ask(prompt, png, req.strokes, (x, ay), box, req.provider)
    except RuntimeError as e:
        raise HTTPException(500, str(e))
    except Exception as e:
        raise HTTPException(502, f"AI request failed: {e}")
    steps.append({"title": "Asked the AI", "detail": f"Sent your prompt and a picture of the board to {vision.provider_name(req.provider)}. "
                                                    f"It answered in {time.perf_counter() - started:.1f} s."})
    return _from_prompt(ai, prompt, x, ay, size, style, req.width, req.height, steps, prompt_strokes, req.strokes)


@app.post("/api/transcribe")
def transcribe_clip(req: TranscribeRequest):
    """Live caption while the person is still talking. Empty audio is not an error."""
    raw = req.audio.split(",", 1)[1] if req.audio.startswith("data:") else req.audio
    try:
        audio = base64.b64decode(raw)
    except Exception:
        raise HTTPException(400, "That recording isn't valid audio.")
    if len(audio) < 400:
        return {"text": ""}
    try:
        heard = vision.transcribe(audio, req.mime)
    except RuntimeError as e:
        if "didn't catch" in str(e):
            return {"text": ""}
        raise HTTPException(500, str(e))
    except Exception as e:
        raise HTTPException(502, f"ElevenLabs request failed: {e}")
    return {"text": heard}


@app.post("/api/speak")
def speak(req: SpeakRequest):
    started = time.perf_counter()
    result = _speak(req)
    boards.remember(result, source="speak", provider=req.provider, user_strokes=req.strokes,
                    seconds=time.perf_counter() - started)
    return _with_voice(result)


def _speak(req: SpeakRequest):
    raw = req.audio.split(",", 1)[1] if req.audio.startswith("data:") else req.audio
    try:
        audio = base64.b64decode(raw)
    except Exception:
        raise HTTPException(400, "That recording isn't valid audio.")
    if len(audio) < 800:
        raise HTTPException(400, "That recording was too short. Hold Talk and say what to draw.")
    if len(audio) > 8_000_000:
        raise HTTPException(400, "That recording is too long. Keep it under about 20 seconds.")
    data = req.image.split(",", 1)[1] if req.image.startswith("data:") else req.image
    png = base64.b64decode(data)
    heard = req.text.strip()[:500]
    if heard:
        steps = [{"title": "Heard you", "detail": f"\"{heard}\""}]
    else:
        started = time.perf_counter()
        try:
            heard = vision.transcribe(audio, req.mime)
        except RuntimeError as e:
            raise HTTPException(500, str(e))
        except Exception as e:
            raise HTTPException(502, f"ElevenLabs request failed: {e}")
        steps = [{"title": "Heard you", "detail": f"ElevenLabs transcribed: \"{heard}\" in {time.perf_counter() - started:.1f} s."}]
    box = _open_box(req.strokes, req.width, req.height)
    x, y = box[0], box[1]
    size = min(max(req.size, 16), 80)
    style = req.style if req.style in text_writer.STYLES else "print"
    started = time.perf_counter()
    try:
        ai = vision.ask(heard, png, req.strokes, (x, y), box, req.provider, vision.SPEAK_PROMPT)
    except RuntimeError as e:
        raise HTTPException(500, str(e))
    except Exception as e:
        raise HTTPException(502, f"AI request failed: {e}")
    steps.append({"title": "Turned it into a drawing",
                  "detail": f"Sent \"{heard}\" to {vision.provider_name(req.provider)}. "
                            f"It answered in {time.perf_counter() - started:.1f} s."})
    problem_strokes = []
    if ai.get("kind") != "drawing":
        problem = str(ai.get("problem") or heard).strip()
        problem = "".join(ch if 32 <= ord(ch) < 127 else " " for ch in problem)
        problem = re.sub(r"\s+", " ", problem).strip()[:120]
        expr = problem.rstrip("=").strip()
        solved_value = None
        try:
            solved = math_solver.solve(expr)
            if solved.get("kind") == "evaluate" and solved.get("answer"):
                solved_value = solved["answer"]
                ai["answer"] = solved_value
                if not problem.endswith("="):
                    problem = f"{problem} ="
        except Exception:
            pass
        if problem:
            wrap_w = max(req.width - x - 20, size * 3)
            problem_strokes, lines, _ = text_writer.text_strokes(
                problem, x, y, size, wrap_w, "print", req.height - 10)
            if problem_strokes and lines:
                y = min(y + lines * size * text_writer.LINE_GAP + size * 0.35, req.height - size * 1.4)
                steps.append({"title": "Restated the problem", "detail": f"Writing \"{problem}\" on the board, then the answer under it."})
                ai["problem"] = problem
    result = _from_prompt(ai, heard, x, y, size, style, req.width, req.height, steps, [],
                          list(req.strokes) + problem_strokes)
    if problem_strokes:
        result["strokes"] = problem_strokes + result["strokes"]
        if solved_value and result.get("answer"):
            result["answer"] = f"{expr} = {solved_value}"
            try:
                result["say"] = explain_math(expr, solved_value, math_solver.show_work(expr))
            except Exception:
                result["say"] = explain_math(expr, solved_value)
    result["_heard"] = heard
    return result


@app.post("/api/crop")
def crop_board(req: CropRequest):
    data = req.image.split(",", 1)[1] if req.image.startswith("data:") else req.image
    try:
        raw = base64.b64decode(data)
        png = tracer.crop_whiteboard(raw)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(422, f"Couldn't crop that image ({e}).")
    if png is None:
        return {"cropped": False}
    return {"cropped": True, "image": "data:image/png;base64," + base64.b64encode(png).decode()}


@app.post("/api/trace")
def trace_image(req: TraceRequest):
    data = req.image.split(",", 1)[1] if req.image.startswith("data:") else req.image
    try:
        raw = base64.b64decode(data)
        strokes, info = tracer.trace(raw, req.x, req.y, req.w, req.h, req.detail)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(422, f"Couldn't trace that image ({e}).")
    strokes = clean_strokes(strokes, req.width, req.height)
    if not strokes:
        raise HTTPException(422, "Couldn't find any clear lines in that image. Try High detail or a higher-contrast picture.")
    why = {
        "centre lines": "It looks like line art on a light background, so it traces down the middle of each line "
                        "(each line drawn once) and outlines any solid filled areas.",
        "marker": "Kept the dry-erase marker and left the photograph out. The board behind the ink is not drawn.",
    }.get(info["method"], "It looks like a photo, so it traces the edges where light areas meet dark ones (Canny edge detection).")
    steps = [
        {"title": "Loaded your image", "detail": f"Placed it on the board at {req.w:.0f} x {req.h:.0f} px and shaded it in the background."},
        {"title": "Chose how to trace it", "detail": why},
        {"title": "Found the lines", "detail": f"OpenCV found {info['found']} line(s); keeping the {info['kept']} longest at "
                                              f"{req.detail} detail so the robot draws the shape, not the noise."},
        {"title": "Planned the robot", "detail": robot_summary(strokes, req.width, req.height)},
    ]
    return {"mode": "trace", "description": "your image", "answer": "", "strokes": strokes, "steps": steps,
            "category": {"type": "Image trace", "detail": f"{info['method']}, {req.detail} detail"}}


@app.get("/api/boards")
def recent_boards():
    return {"configured": boards.configured(), "boards": boards.list_boards()}


@app.get("/api/boards/{board_id}")
def one_board(board_id: str):
    if not boards.configured():
        raise HTTPException(503, "Add MONGODB_URI to backend/.env, save the file, and try again.")
    doc = boards.get_board(board_id)
    if not doc:
        raise HTTPException(404, "That saved board is gone.")
    return doc


@app.post("/api/gcode", response_class=PlainTextResponse)
def make_gcode(req: GcodeRequest):
    return gcode.strokes_to_gcode(req.strokes, req.width, req.height,
                                  req.board_width_mm, req.board_height_mm)


# ---------- the real robot ----------

# The plotter's drawing area. Override in backend/.env once the frame is built.
BOARD_W_MM = float(os.environ.get("BOARD_WIDTH_MM", 800))
BOARD_H_MM = float(os.environ.get("BOARD_HEIGHT_MM", 500))


class RobotConnectRequest(BaseModel):
    port: str                  # "COM3", or "sim" for the simulated robot


class RobotDrawRequest(BaseModel):
    strokes: list[Stroke]
    width: int
    height: int


class RobotCommandRequest(BaseModel):
    line: str                  # one G-code or GRBL "$" command


class RobotPenRequest(BaseModel):
    down: bool


def robot_call(fn, *args):
    try:
        fn(*args)
    except robot_link.RobotError as e:
        raise HTTPException(409, str(e))
    return robot_link.robot.status()


@app.get("/api/robot/ports")
def robot_ports():
    return {"ports": robot_link.list_ports(), "board_mm": [BOARD_W_MM, BOARD_H_MM]}


@app.get("/api/robot/status")
def robot_status():
    return robot_link.robot.status()


@app.post("/api/robot/connect")
def robot_connect(req: RobotConnectRequest):
    return robot_call(robot_link.robot.connect, req.port)


@app.post("/api/robot/disconnect")
def robot_disconnect():
    return robot_call(robot_link.robot.disconnect)


@app.post("/api/robot/draw")
def robot_draw(req: RobotDrawRequest):
    if not any(len(s) >= 2 for s in req.strokes):
        raise HTTPException(400, "There's nothing for the robot to draw yet.")
    code = gcode.strokes_to_gcode(req.strokes, req.width, req.height, BOARD_W_MM, BOARD_H_MM)
    return robot_call(robot_link.robot.draw, code)


@app.post("/api/robot/pause")
def robot_pause():
    return robot_call(robot_link.robot.pause)


@app.post("/api/robot/resume")
def robot_resume():
    return robot_call(robot_link.robot.resume)


@app.post("/api/robot/stop")
def robot_stop():
    return robot_call(robot_link.robot.stop, gcode.PEN_UP)


@app.post("/api/robot/pen")
def robot_pen(req: RobotPenRequest):
    return robot_call(robot_link.robot.command, gcode.PEN_DOWN if req.down else gcode.PEN_UP)


@app.post("/api/robot/command")
def robot_command(req: RobotCommandRequest):
    try:
        reply = robot_link.robot.command(req.line)
    except robot_link.RobotError as e:
        raise HTTPException(409, str(e))
    return {**robot_link.robot.status(), "reply": reply}


@app.get("/api/host")
def host_info():
    """Where this copy is meant to be opened, once it is on Vultr behind a .tech name."""
    return {
        "public_url": os.environ.get("PUBLIC_URL", "").strip(),
        "vultr": bool(os.environ.get("VULTR_API_KEY", "").strip()),
        "backboard": student_memory.configured(),
        "voice": speech.configured(),
    }


# Serve the frontend from the same server (must come after the API routes)
FRONTEND = Path(__file__).resolve().parent.parent / "frontend"
app.mount("/", StaticFiles(directory=FRONTEND, html=True), name="frontend")