"""
Whiteboard finisher backend.

Run:  uvicorn main:app --reload     (from the backend/ folder)
Then open http://localhost:8000
"""
import base64
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

import gcode
import handwriting
import math_solver
import placement
import shapes
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


class TraceRequest(BaseModel):
    image: str                 # the uploaded picture, already sized to where it sits on the board
    x: float
    y: float
    w: float
    h: float
    detail: str = "medium"     # low | medium | high
    width: int = 1200
    height: int = 700


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
                problems.append({"expression": parts[0], "bbox": prob.get("bbox") or everything,
                                 "blank_bbox": prob.get("blank_bbox")})
            continue
        # Several problems in one string: match each to a line of writing, top to bottom
        rows = stroke_rows(user_strokes)
        for i, part in enumerate(parts):
            box = rows[i] if len(rows) == len(parts) else (prob.get("bbox") or everything)
            problems.append({"expression": part, "bbox": box, "blank_bbox": None})
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


@app.post("/api/complete")
def complete(req: CompleteRequest):
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
    step("Looked at the board", f"Sent a picture of {what} with a coordinate grid "
                                f"and each stroke's exact points to {vision.provider_name()}.")
    if req.action == "drawing":
        req.mode = "drawing"
    started = time.perf_counter()
    try:
        ai = vision.analyze_board(png, shapes.summarize_strokes(req.strokes), req.strokes, req.mode, req.action,
                                  req.image_box)
    except RuntimeError as e:
        raise HTTPException(500, str(e))
    except Exception as e:
        raise HTTPException(502, f"AI request failed: {e}")
    steps[-1]["detail"] += f" It answered in {time.perf_counter() - started:.1f} s."

    mode = ai.get("mode", "drawing")
    description = ai.get("description", "")
    names = {"math": "math", "fill": "pattern", "drawing": "drawing", "answer": "a question",
             "hint": "a hint", "check": "checking work"}
    buttons = {"answer": "Answer", "hint": "Hint", "check": "Check my work", "drawing": "Finish drawing"}
    if req.action in ("hint", "check"):
        step("Decided what to do", f"You pressed {buttons[req.action]}.")
        mode = req.action
    elif req.mode in ("math", "fill", "drawing"):
        if mode != req.mode:
            # The person chose the mode, so that wins over the AI's guess
            if req.mode == "math" and not ai.get("expression"):
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
        # If its math reading is real, solvable math, math wins.
        reading = str(ai.get("math_reading") or "").strip()
        if mode != "math" and reading and reading.lower() not in ("null", "none"):
            try:
                math_solver.solve(reading)
                ok = any(ch.isdigit() for ch in reading)
            except Exception:
                ok = False
            if ok:
                step("Changed its mind", f"The strokes also read as  {reading}  which is valid math. "
                                        "Handwritten math often looks like letters (1+1 looks like H), "
                                        "so treating it as math instead.")
                ai.setdefault("problems", [{"expression": reading}])
                mode = "math"
        if req.action == "answer" and mode == "drawing":
            raise HTTPException(422, "I don't see a problem or question to answer. For pictures, use Finish drawing.")
    step("What it sees", description)

    if mode == "math":
        step("AI's notes", ai.get("reasoning"))
        problems = collect_problems(ai, layout)
        kinds = {"evaluate": "evaluated", "solve": "solved for the unknown",
                 "blank": "solved for the blank", "check": "checked both sides"}
        strokes, answers, expressions, failed, types = [], [], [], [], []
        pad = 30 if len(problems) == 1 else 12  # tighter when problems sit close together
        for n, prob in enumerate(problems, 1):
            expression = prob["expression"]
            label = f"Problem {n}" if len(problems) > 1 else "The problem"
            try:
                result = math_solver.solve(expression)
            except Exception as e:
                failed.append(expression)
                step(f"{label}: couldn't solve", f"Read it as  {expression}  but SymPy couldn't make sense of it ({e}). Skipping it.")
                continue
            bbox = locate_math_box(png, prob.get("bbox"), req.strokes, req.width, req.height,
                                   len(problems) == 1, pad)
            blank = prob.get("blank_bbox")
            if result["kind"] == "blank" and not blank:
                blank = find_blank(expression, bbox, req.strokes)
            ai_box = prob.get("bbox")
            near_y = ((float(ai_box[1]) + float(ai_box[3])) / 2
                      if ai_box and not _loose_box(ai_box, req.width, req.height) else None)
            new, where = place_answer(expression, result, bbox, req.width, req.height, board, blank,
                                      req.strokes, near_y)
            strokes += new
            try:
                types.append(math_solver.classify(expression, result))
            except Exception:
                types.append(("Math", "equation"))
            answers.append(result["answer"])
            expressions.append(expression)
            step(f"{label}", f"Read  {expression}  then {kinds.get(result['kind'], 'solved it')} with SymPy "
                             f"(exact maths, not an AI guess): {result['answer']}. Writing it {where}.")
        if not strokes:
            raise HTTPException(422, f"Read the math as {', '.join(failed) or 'nothing'} but couldn't solve it. "
                                     "Try writing it more clearly.")
        kinds_found = sorted({t for t, _ in types})
        if len(types) == 1:
            category = {"type": types[0][0], "detail": types[0][1]}
        elif len(kinds_found) == 1:
            category = {"type": kinds_found[0], "detail": f"{len(types)} problems"}
        else:
            category = {"type": "Mixed math", "detail": ", ".join(f"{d} ({t.lower()})" for t, d in types)}
        identify(category, "Figured out from how SymPy solved it: "
                 + "; ".join(f"{e} is {t.lower()} ({d})" for e, (t, d) in zip(expressions, types)) + ".")
        step("Planned the robot", robot_summary(strokes, req.width, req.height))
        return {"mode": "math", "description": description, "expression": ";  ".join(expressions),
                "answer": ", ".join(answers), "strokes": strokes, "steps": steps, "category": category}

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
        step("Chose what to write", f"Writing {text} next in the sequence {got.where}, {got.size:.0f} px tall to match.")
        numeric = any(ch.isdigit() for ch in text)
        category = {"type": "Number pattern" if numeric else "Pattern", "detail": description or "a sequence to continue"}
        identify(category, "It's a sequence, so the next items follow a rule instead of an equation.")
        step("Planned the robot", robot_summary(strokes, req.width, req.height))
        return {"mode": "fill", "description": description, "answer": text, "strokes": strokes,
                "steps": steps, "category": category}

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
        return {"mode": "hint", "description": description, "answer": text, "strokes": strokes,
                "steps": steps, "category": category}

    if mode == "check":
        step("AI's notes", ai.get("reasoning"))
        problems = [p for p in (ai.get("problems") or []) if isinstance(p, dict)]
        if not problems:
            raise HTTPException(422, "I couldn't find any problems with answers to check.")
        everything = content_bbox(layout) or (40, 40, req.width - 40, 120)
        strokes, right, wrong, empty = [], 0, 0, 0
        pad = 30 if len(problems) == 1 else 12
        for n, prob in enumerate(problems, 1):
            label = f"Problem {n}" if len(problems) > 1 else "Your problem"
            shown = prob.get("expression") or f"{prob.get('equation')}  ->  {prob.get('student_answer') or '(no answer)'}"
            try:
                result = math_solver.check_work(prob.get("expression"), prob.get("equation"), prob.get("student_answer"))
            except Exception as e:
                step(f"{label}: couldn't check", f"Read it as  {shown}  but couldn't check it ({e}).")
                continue
            x1, y1, x2, y2 = refine_bbox(prob.get("bbox") or everything, req.strokes, pad)
            size = min(max((y2 - y1) * 0.85, 28), 120)
            if result["correct"] is None:
                empty += 1
                step(label, f"Read  {shown}  but there's no answer written yet, so nothing to mark.")
                continue
            fix = result.get("correct_answer")

            def mark(s, correct=result["correct"], fix=fix):
                """A check, or an X with the right answer beside it, drawn at the origin."""
                if correct:
                    return handwriting.text_to_strokes("✓", 0, 0, s)
                cap = s * 0.55
                return (handwriting.text_to_strokes("✗", 0, 0, s)
                        + text_writer.text_strokes(fix, s * 0.95, (s - cap) / 2, cap, 10 ** 6, "print")[0])

            def mark_spots(s, bw, bh, x1=x1, y1=y1, x2=x2, y2=y2):
                gap = placement.GAP
                return [(x2 + max(s * 0.4, gap), y1 + (y2 - y1 - s) / 2, "beside it", 0.6 * s),
                        (x2 - bw, y2 + max(0.2 * s, gap), "just under the end of it"),
                        (x1 - bw - max(s * 0.4, gap), y1 + (y2 - y1 - s) / 2, "to the left of it"),
                        (x1, y1 - bh - max(0.3 * s, gap), "above it")]

            got = placement.place(board, mark, mark_spots, placement.shrinking(size, steps=3, factor=0.8),
                                  spot_first=True)
            strokes += got.strokes
            if result["correct"]:
                right += 1
                step(label, f"Read  {shown}  and checked it with SymPy: correct. Marked it with a check {got.where}.")
            else:
                wrong += 1
                step(label, f"Read  {shown}  and checked it with SymPy: not quite. The right answer is {fix}, "
                            f"so it gets an X with the correct answer, {got.where}.")
        total = right + wrong
        if not total:
            raise HTTPException(422, "I didn't find any finished answers to check. Write your answer after the = sign.")
        category = {"type": "Check", "detail": f"{right} of {total} correct" + (f", {empty} not answered yet" if empty else "")}
        identify(category, "You asked to check your work, so each answer was verified with exact maths.")
        step("Planned the robot", robot_summary(strokes, req.width, req.height))
        return {"mode": "check", "description": description, "answer": category["detail"], "strokes": strokes,
                "steps": steps, "category": category}

    if mode == "answer":
        question = str(ai.get("question") or description or "your question")
        step("Read the question", f"\"{question}\"")
        step("How it answered", ai.get("reasoning"))
        answer = str(ai.get("answer", "")).strip()
        calc = ai.get("calculation")
        if calc and str(calc).lower() not in ("null", "none"):
            try:
                solved = math_solver.solve(str(calc))
                result = solved["answer"]
                note = " There are no real solutions, so these are complex numbers (i = square root of -1)." if solved.get("complex") else ""
                step("Solved it exactly", f"Computed {calc} with SymPy (exact, not an AI guess): {result}.{note}")
                if "{calc}" in answer and "=" in result and answer.rstrip().endswith("= {calc}"):
                    result = result.split("=", 1)[1]  # "x = {calc}" + "x=2" -> "x = 2"
            except Exception:
                result = str(ai.get("calc_guess", "?"))
            answer = answer.replace("{calc}", result) if "{calc}" in answer else f"{answer} {result}".strip()
        answer = answer.replace("{calc}", str(ai.get("calc_guess", "")))
        if not answer:
            raise HTTPException(422, "The AI didn't come up with an answer. Try writing the question more clearly.")
        ink = vision.ink_bbox(png, None) if not req.strokes else None
        if ink and re.fullmatch(r"=?\s*[\d./+\-xXi]+", answer.replace(" ", "")):
            shown = answer[1:].strip() if answer.startswith("=") else answer
            strokes, where = place_answer("problem", {"kind": "evaluate", "answer": shown}, ink,
                                          req.width, req.height, board)
            if strokes:
                step("Wrote the answer", f"\"{answer}\" {where}.")
                category = {"type": "Question", "detail": question}
                identify(category, "The answer is written on the same line as the problem in the picture.")
                step("Planned the robot", robot_summary(strokes, req.width, req.height))
                return {"mode": "answer", "description": question, "answer": answer, "strokes": strokes,
                        "steps": steps, "category": category}
        strokes, size, written, where = write_below(answer, layout, req.width, req.height, board)
        if not strokes:
            raise HTTPException(422, "There's no room left on the board for the answer.")
        step("Wrote the answer", f"\"{answer}\" {where.replace('your work', 'your question')}, "
                                 f"{size:.0f} px tall, in {written} line(s).")
        category = {"type": "Question", "detail": question}
        identify(category, "It's written words asking something, so the robot answers it instead of solving or drawing.")
        step("Planned the robot", robot_summary(strokes, req.width, req.height))
        return {"mode": "answer", "description": question, "answer": answer, "strokes": strokes,
                "steps": steps, "category": category}

    step("Made a plan", ai.get("plan"))
    log = []
    strokes = clean_strokes(shapes.build_strokes(ai, req.strokes, log), req.width, req.height)
    if req.review and strokes:
        started = time.perf_counter()
        try:
            summary = shapes.summarize_strokes(req.strokes)
            fix = vision.review_drawing(png, req.strokes, strokes, summary, ai)
            took = f"{time.perf_counter() - started:.1f} s"
            if fix.get("ok") is False and (fix.get("shapes") or fix.get("strokes")):
                fixed_log = []
                fixed = clean_strokes(shapes.build_strokes(fix, req.strokes, fixed_log), req.width, req.height)
                if fixed:
                    strokes, log = fixed, fixed_log
                    step("Double-checked the drawing", f"Looked at its own result and fixed it ({took}): "
                                                       f"{fix.get('review') or 'adjusted the shapes'}")
                    if fix.get("plan"):
                        step("Updated the plan", fix["plan"])
                else:
                    step("Double-checked the drawing", f"Tried a fix but it came out empty, so kept the first version ({took}).")
            else:
                step("Double-checked the drawing", f"Looked at its own result and it looked right ({took}). "
                                                   f"{fix.get('review') or ''}".strip())
        except Exception as e:
            step("Double-checked the drawing", f"Skipped the double-check ({e}).")
    for note in log:
        step("Cleaned up the drawing", note)
    if not strokes:
        raise HTTPException(422, "The AI couldn't figure out how to finish this drawing. Try adding a bit more.")
    category = {"type": "Drawing", "detail": description or "a picture to finish"}
    identify(category, "It's a picture rather than numbers or symbols, so it gets finished with shapes.")
    step("Planned the robot", robot_summary(strokes, req.width, req.height))
    return {"mode": "drawing", "description": description, "plan": ai.get("plan", ""),
            "strokes": strokes, "steps": steps, "category": category}


@app.post("/api/write")
def write_text(req: WriteRequest):
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
        ai = vision.ask(prompt, png, req.strokes, (x, ay), box)
    except RuntimeError as e:
        raise HTTPException(500, str(e))
    except Exception as e:
        raise HTTPException(502, f"AI request failed: {e}")
    steps.append({"title": "Asked the AI", "detail": f"Sent your prompt and a picture of the board to {vision.provider_name()}. "
                                                    f"It answered in {time.perf_counter() - started:.1f} s."})

    if ai.get("kind") == "drawing":
        if ai.get("description"):
            steps.append({"title": "Decided to draw", "detail": ai["description"]})
        log = []
        strokes = clean_strokes(shapes.build_strokes({"shapes": ai.get("shapes")}, [], log), req.width, req.height)
        for note in log:
            steps.append({"title": "Made the drawing", "detail": note})
        if not strokes:
            raise HTTPException(422, "The AI couldn't work out how to draw that. Try describing it differently.")
        steps.append({"title": "Planned the robot", "detail": robot_summary(strokes, req.width, req.height)})
        return {"mode": "drawing", "description": ai.get("description", "a drawing"), "answer": "", "strokes": strokes,
                "prompt_strokes": prompt_strokes, "steps": steps,
                "category": {"type": "Drawing", "detail": ai.get("description", prompt)}}

    answer = str(ai.get("answer", "")).strip()
    if ai.get("reasoning"):
        steps.append({"title": "How it answered", "detail": ai["reasoning"]})
    calc = ai.get("calculation")
    if calc and str(calc).lower() not in ("null", "none"):
        try:
            solved = math_solver.solve(str(calc))
            result = solved["answer"]
            note = " No real solutions, so these are complex numbers (i = square root of -1)." if solved.get("complex") else ""
            steps.append({"title": "Double-checked the maths", "detail": f"Computed {calc} with SymPy (exact, not an AI guess): {result}.{note}"})
            if "=" in result and answer.rstrip().endswith("= {calc}"):
                result = result.split("=", 1)[1]
        except Exception:
            result = str(ai.get("calc_guess", "?"))
            steps.append({"title": "Double-checked the maths", "detail": f"SymPy couldn't parse {calc}, so using the AI's own result: {result}."})
        answer = answer.replace("{calc}", result) if "{calc}" in answer else answer
    answer = answer.replace("{calc}", str(ai.get("calc_guess", "")))
    if not answer:
        raise HTTPException(422, "The AI didn't come up with an answer. Try rephrasing your prompt.")

    board = placement.Board(req.width, req.height, req.strokes + prompt_strokes)
    pbox = placement.bounds(prompt_strokes) if prompt_strokes else (x, y, x, y)

    def ask_spots(s, bw, bh):
        gap = placement.GAP
        return [(x, max(ay, pbox[3] + gap), "under your prompt"),
                (pbox[2] + max(s, gap), pbox[1], "to the right of your prompt"),
                (x, pbox[1] - bh - max(s * 0.35, gap), "above your prompt")]

    got = placement.place(board, lambda s: text_writer.text_strokes(answer, 0, 0, s, max(req.width - x - 20, s * 3),
                                                                    style)[0],
                          ask_spots, placement.shrinking(size))
    strokes = got.strokes
    if not strokes:
        raise HTTPException(422, "There's no room to write there. Try clicking higher up on the board.")
    written = len(text_writer.wrap(answer, style, got.size, max(req.width - x - 20, got.size * 3)))
    steps.append({"title": "Wrote the answer", "detail": f"\"{answer}\" {got.where}, in {written} line(s), "
                                                         "using a single-stroke pen font."})
    steps.append({"title": "Planned the robot", "detail": robot_summary(strokes, req.width, req.height)})
    return {"mode": "answer", "description": prompt, "answer": answer, "strokes": strokes,
            "prompt_strokes": prompt_strokes, "steps": steps, "category": {"type": "Answer", "detail": prompt}}


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
    why = ("It looks like line art on a light background, so it traces down the middle of each line "
           "(each line drawn once) and outlines any solid filled areas."
           if info["method"] == "centre lines" else
           "It looks like a photo, so it traces the edges where light areas meet dark ones (Canny edge detection).")
    steps = [
        {"title": "Loaded your image", "detail": f"Placed it on the board at {req.w:.0f} x {req.h:.0f} px and shaded it in the background."},
        {"title": "Chose how to trace it", "detail": why},
        {"title": "Found the lines", "detail": f"OpenCV found {info['found']} line(s); keeping the {info['kept']} longest at "
                                              f"{req.detail} detail so the robot draws the shape, not the noise."},
        {"title": "Planned the robot", "detail": robot_summary(strokes, req.width, req.height)},
    ]
    return {"mode": "trace", "description": "your image", "answer": "", "strokes": strokes, "steps": steps,
            "category": {"type": "Image trace", "detail": f"{info['method']}, {req.detail} detail"}}


@app.post("/api/gcode", response_class=PlainTextResponse)
def make_gcode(req: GcodeRequest):
    return gcode.strokes_to_gcode(req.strokes, req.width, req.height,
                                  req.board_width_mm, req.board_height_mm)


# Serve the frontend from the same server (must come after the API routes)
FRONTEND = Path(__file__).resolve().parent.parent / "frontend"
app.mount("/", StaticFiles(directory=FRONTEND, html=True), name="frontend")