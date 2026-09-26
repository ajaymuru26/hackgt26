"""
Send the whiteboard picture to Claude and get back structured JSON.

Before sending, we draw a faint labeled grid over the image. Vision models are
much better at giving accurate coordinates when they can read them off a grid.
"""
import base64
import io
import json
import os

from PIL import Image, ImageDraw

# Uses OpenAI if OPENAI_API_KEY is set, otherwise Claude (ANTHROPIC_API_KEY).
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-5")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o")
GRID_STEP = 100

SYSTEM_PROMPT = """You are the brain of a robot that fills in the blanks on a whiteboard.
Someone started writing or drawing; your job is to figure out the next best thing to write.
You receive a picture of the board. A light grey grid with pixel coordinates is drawn over it
(x increases to the right, y increases downward, origin at top-left). The grid is NOT part of the drawing.

Respond with ONLY a JSON object, no markdown, no commentary. Pick ONE of these four modes.

Reading handwriting carefully:
- Read the close-up image character by character, left to right, before deciding anything.
- Commonly confused digits: 1 vs 7 (7 has a flat top), 4 vs 9 (4 is open at the top), 5 vs 6,
  3 vs 8, 0 vs 6, 2 vs 7, 1 vs the letter l, and + vs x. Look at the actual stroke shapes.
- The number of digits matters: "12" is not "1 2" and not "2". Check you have every digit.

How to pick the mode:
- If the strokes can be read as digits and math symbols, it is MATH, even with no "=" sign.
  Handwritten math often looks like letters: "1+1" looks like the letter H, "1" looks like I or l,
  "+" looks like t or a cross, "7" looks like T, "0" looks like O. Read these as math, not letters.
- Only choose DRAWING for pictures of objects or shapes (a house, a face, a star, a car).
  Letters and words are never drawings to finish.
- If the person wrote WORDS as a question or instruction ("What color is the sky?", "name a
  planet", "spell cat"), it is QUESTION: answer it. It is not a pattern and not a drawing.
- PATTERN (fill) is only for sequences of items to continue, like "2, 4, 6," or "A, B, C,".
- Every response, in every mode, must also include "math_reading": the strokes read as math
  (e.g. "1+1"), or null if they truly cannot be read as math.

1) MATH: arithmetic or an equation (e.g. "5+4=", "12x3=", "5+_=9", "2x+3=7")
{
  "mode": "math",
  "description": "short phrase describing what you see",
  "reasoning": "one or two sentences: how you read the handwriting and any characters you were unsure about",
  "problems": [
    {"expression": "...", "bbox": [x1, y1, x2, y2], "blank_bbox": [x1, y1, x2, y2] or null}
  ]
}
- The board may have SEVERAL separate problems (different lines or spots). Give each one its own
  entry in "problems", top to bottom. Never join problems together with ";" or newlines.
- "expression" is a TRANSCRIPTION of exactly what is written. NEVER add the answer yourself;
  a separate solver computes it. If the board shows "5+4=" the expression is "5+4=", not "5+4=9".
- If a line ends with "=" and nothing after it, keep the trailing "=".
- If the person left a blank (an underscore, an empty box, a "?" or a gap) replace it with _ ,
  e.g. "5+_=9" or "_x4=20", and give its location in "blank_bbox". Otherwise "blank_bbox" is null.
- Use * for multiply, / for divide, ^ for powers.
- Handwritten digits are easily mistaken for letters. Assume a character is a DIGIT unless it is
  clearly an algebra variable in an equation: S/s -> 5, Z/z -> 2, l/I/| -> 1, O/o -> 0, g/q -> 9,
  b/G -> 6, B -> 8, T -> 7, t -> +. A letter "x" between two numbers means multiply.
  Plain arithmetic like "5+4=" never contains letters.
- "bbox" is a tight box around that one problem's handwriting only.

2) FILL: a pattern or sequence to continue (e.g. "2, 4, 6, 8," or "1 3 5 _" or "A, B, C,")
{
  "mode": "fill",
  "description": "short phrase, e.g. 'counting by 2s'",
  "reasoning": "one or two sentences: the rule you found and how you applied it",
  "text": "what to write next (digits, letters, spaces and punctuation are all fine)",
  "position": [x, y_top, height]
}
- "position" is where the new text starts: to the right of the last item (or in the blank),
  top edge aligned with the existing writing, height matching the existing digits.

4) QUESTION: handwritten words asking something or giving an instruction
{
  "mode": "answer",
  "description": "short phrase, e.g. 'a question about the sky'",
  "question": "the question as you read it",
  "reasoning": "one or two sentences on how you answered",
  "answer": "a short answer to write under the question, max about 15 words, plain ASCII",
  "calculation": "SymPy-readable maths if the answer depends on a calculation, else null",
  "calc_guess": "your own result of that calculation"
}
- If the question is about maths on the board ("what is x?", "solve it", "what's the answer?"),
  copy that maths into "calculation" (e.g. "x^2+2*x+2=0") and write the answer as "x = {calc}".
  A solver fills in the exact result, including complex answers. Actually answer the question.
- Only give a hint instead of the answer when the person explicitly asks for a hint.

3) DRAWING: a partial drawing or connect-the-dots
{
  "mode": "drawing",
  "description": "short phrase: what it is and what is missing, e.g. 'a house missing its roof and door'",
  "plan": "think first: what is already drawn, what exactly is missing, and where each missing part goes (which existing stroke ends it connects to)",
  "shapes": [ ... ]
}
Build the missing parts from these shapes (coordinates in grid pixels; angles in degrees with
0 = right, 90 = down, 180 = left, 270 = up, increasing clockwise on screen):
  {"type": "line", "from": [x, y], "to": [x, y]}
  {"type": "polyline", "points": [[x, y], ...], "closed": false}
  {"type": "rect", "x": left, "y": top, "w": width, "h": height}
  {"type": "circle", "center": [x, y], "r": radius}
  {"type": "ellipse", "center": [x, y], "rx": rx, "ry": ry}
  {"type": "arc", "center": [x, y], "r": radius, "start": deg, "end": deg}   (or "rx"/"ry" instead of "r")
  {"type": "curve", "points": [start, control, end]}  or [start, control1, control2, end]  (Bezier)
  {"type": "mirror", "strokes": ["S0", "S3"] or "all", "axis": "vertical", "at": x}
      -> copies the person's own strokes, flipped across the line x = at ("horizontal" axis flips across y = at).
         Use this when a drawing is symmetric and one side is missing (half a face, one wing,
         one side of a house). It is pixel-perfect, so prefer it over redrawing that side.
         List ONLY the strokes on the drawn side (red S labels in the picture show which is which).
         Do not list strokes in the middle that cross the mirror line (eyes, nose, a centred mouth);
         they are already there. "at" is the drawing's centre line.

Rules for drawings:
- You are also given the person's strokes as exact coordinates (S0, S1, ...) with their corners
  named K0, K1, ... Trust these over the picture.
- ANCHORS: anywhere a point [x, y] is expected you may write an anchor name instead:
  "K7" (a named corner), or "S2.start", "S2.end", "S2.top", "S2.bottom", "S2.left", "S2.right", "S2.center".
  Whenever a new line should touch the existing drawing, USE AN ANCHOR: it is exact, a guess is not.
  Example roof on a box whose top corners are K0 and K3: {"type": "polyline", "points": ["K0", [600, 200], "K3"]}
- Work out sizes from the existing parts (e.g. a door about 1/3 of the house width, centred on it).
- Only add the NEW parts needed to finish it. Do not redraw existing lines.
- Match the existing size, style and position, and stay inside the image.
- Prefer the simplest shapes: a circle, not a polyline of 30 points. Keep it to about 30 shapes or fewer."""


def add_grid(png_bytes: bytes, strokes=None) -> tuple[bytes, int, int]:
    img = Image.open(io.BytesIO(png_bytes)).convert("RGB")
    w, h = img.size
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(overlay)
    for x in range(0, w, GRID_STEP):
        d.line([(x, 0), (x, h)], fill=(120, 140, 170, 70), width=1)
        d.text((x + 3, 3), str(x), fill=(90, 110, 150, 200))
    for y in range(0, h, GRID_STEP):
        d.line([(0, y), (w, y)], fill=(120, 140, 170, 70), width=1)
        if y:
            d.text((3, y + 3), str(y), fill=(90, 110, 150, 200))
    # small red stroke labels (S0, S1, ...) at each stroke's start, matching the coordinate list
    for i, stroke in enumerate((strokes or [])[:60]):
        if stroke:
            x, y = stroke[0]
            d.text((x + 6, y - 14), f"S{i}", fill=(210, 40, 40, 230))
    out = Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")
    buf = io.BytesIO()
    out.save(buf, format="PNG")
    return buf.getvalue(), w, h


def _parse_json(text: str) -> dict:
    text = text.replace("```json", "").replace("```", "").strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("AI did not return JSON")
    return json.loads(text[start:end + 1])


def _ask_openai(images: list[tuple[str, str]], user_text: str, system: str = None) -> str:
    """images = [(caption, base64_png), ...]"""
    from openai import OpenAI
    client = OpenAI()  # reads OPENAI_API_KEY
    content = []
    for caption, b64 in images:
        content.append({"type": "text", "text": caption})
        # "high" stops OpenAI from shrinking the picture, which blurs small handwriting
        content.append({"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}", "detail": "high"}})
    content.append({"type": "text", "text": user_text})
    response = client.chat.completions.create(
        model=OPENAI_MODEL,
        response_format={"type": "json_object"},
        temperature=0,
        messages=[{"role": "system", "content": system or SYSTEM_PROMPT}, {"role": "user", "content": content}],
    )
    return response.choices[0].message.content


def _ask_claude(images: list[tuple[str, str]], user_text: str, system: str = None) -> str:
    import anthropic
    client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY
    content = []
    for caption, b64 in images:
        content.append({"type": "text", "text": caption})
        content.append({"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": b64}})
    content.append({"type": "text", "text": user_text})
    response = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=4096,
        system=system or SYSTEM_PROMPT,
        messages=[{"role": "user", "content": content}],
    )
    return "".join(block.text for block in response.content if block.type == "text")


def close_up(png_bytes: bytes, strokes, pad=40, target=1100) -> bytes | None:
    """Crop to just the ink (no grid, no labels) and enlarge it, so digits are big and clean."""
    pts = [p for s in (strokes or []) for p in s]
    if not pts:
        return None
    img = Image.open(io.BytesIO(png_bytes)).convert("RGB")
    x1 = max(0, int(min(p[0] for p in pts)) - pad)
    y1 = max(0, int(min(p[1] for p in pts)) - pad)
    x2 = min(img.width, int(max(p[0] for p in pts)) + pad)
    y2 = min(img.height, int(max(p[1] for p in pts)) + pad)
    crop = img.crop((x1, y1, x2, y2))
    scale = min(3.0, target / max(crop.width, crop.height))
    if scale > 1.05:
        crop = crop.resize((int(crop.width * scale), int(crop.height * scale)), Image.LANCZOS)
    buf = io.BytesIO()
    crop.save(buf, format="PNG")
    return buf.getvalue()


FORCED_MODE_TEXT = {
    "math": "The person set the mode to MATH: this is definitely a math problem. "
            "Respond with mode \"math\" only. Read every character as a digit or math symbol "
            "unless it is clearly a variable like x in an equation.",
    "fill": "The person set the mode to PATTERN: this is a sequence to continue. "
            "Respond with mode \"fill\" only.",
    "drawing": "The person set the mode to DRAWING: this is a picture to finish. "
               "Respond with mode \"drawing\" only, even if parts look like letters or numbers.",
}


def provider_name() -> str:
    if os.environ.get("OPENAI_API_KEY"):
        return f"OpenAI {OPENAI_MODEL}"
    return f"Claude {CLAUDE_MODEL}"


ACTION_TEXT = {
    "answer": "The person pressed ANSWER: they want the answer. Pick math, fill or answer (question) mode. "
              "Do NOT pick drawing, even if the strokes look like letters or shapes.",
    "hint": """The person pressed HINT. Do not give the final answer. Respond with:
{"mode": "hint", "description": "what is on the board", "reasoning": "what the next step is and why",
 "hint": "ONE short hint, max 15 words, that helps them take the next step without giving away the answer"}
This works for maths, questions, patterns and drawings (e.g. "What do houses have on top?").""",
    "check": """The person pressed CHECK MY WORK: they wrote problems AND their own answers. Respond with:
{"mode": "check", "description": "what is on the board", "reasoning": "anything you were unsure reading",
 "problems": [
   {"expression": "5+4=10", "bbox": [x1, y1, x2, y2]},
   {"equation": "2*x+3=7", "student_answer": "x=3", "bbox": [x1, y1, x2, y2]}
 ]}
- Arithmetic: "expression" is the whole line exactly as written, INCLUDING the student's answer.
- Algebra: "equation" is the equation, "student_answer" is what they wrote as the solution (or null).
- TRANSCRIBE EXACTLY what they wrote, even if it is wrong. Never fix their answer; a solver checks it.
- "bbox" covers the problem and the student's answer. One entry per problem, top to bottom.""",
}


IMAGE_TEXT = ("The board also has an UPLOADED PICTURE (a worksheet, screenshot or photo) inside the box "
              "x {0:.0f}-{2:.0f}, y {1:.0f}-{3:.0f}. Treat what is in the picture exactly like writing on the board: "
              "solve, answer, hint or check the problems shown in it. Give every bbox in board pixels, around "
              "where that problem appears inside the picture. If the picture has several problems, list each one.")


def analyze_board(png_bytes: bytes, stroke_summary: str = "", strokes=None, mode: str = "auto",
                  action: str = "", image_box=None) -> dict:
    gridded, w, h = add_grid(png_bytes, strokes)
    images = []
    focus = list(strokes or [])
    if image_box:
        focus.append([[image_box[0], image_box[1]], [image_box[2], image_box[3]]])
    zoom = close_up(png_bytes, focus)
    if zoom:
        images.append(("IMAGE 1: an enlarged close-up of just the handwriting, with no grid. "
                       "Use this one to READ the characters and digits.", base64.b64encode(zoom).decode()))
    images.append((f"IMAGE {len(images) + 1}: the full board ({w} x {h} px) with a coordinate grid and red stroke "
                   "labels. Use this one ONLY for positions. All coordinates you return must be in this "
                   "image's pixel space.", base64.b64encode(gridded).decode()))
    user_text = f"The board is {w} x {h} pixels. Finish it."
    if mode in FORCED_MODE_TEXT:
        user_text += "\n\n" + FORCED_MODE_TEXT[mode]
    if action in ACTION_TEXT:
        user_text += "\n\n" + ACTION_TEXT[action]
    if image_box:
        user_text += "\n\n" + IMAGE_TEXT.format(*image_box)
    if stroke_summary:
        user_text += ("\n\nThe person's strokes, as exact coordinates "
                      "(start/end points, bounding box, and a sampled path):\n" + stroke_summary)

    if os.environ.get("OPENAI_API_KEY"):
        return _parse_json(_ask_openai(images, user_text))
    if os.environ.get("ANTHROPIC_API_KEY"):
        return _parse_json(_ask_claude(images, user_text))
    raise RuntimeError("No AI key set. Run: export OPENAI_API_KEY=... (or ANTHROPIC_API_KEY=...)")


ASK_PROMPT = """You are a robot that writes on a real whiteboard with a marker.
The person typed a prompt. Answer it so the robot can write the answer on the board.
You also get a picture of the board: use it when the prompt refers to it ("give me a hint",
"is this right?", "what shape is this?"). Otherwise just answer the prompt.

Respond with ONLY a JSON object, no markdown. One of:

{
  "kind": "text",
  "reasoning": "one or two sentences on how you answered",
  "answer": "the text to write: short, max about 20 words, plain ASCII, no emoji or markdown",
  "calculation": "if the answer depends on arithmetic or algebra, the maths as SymPy-readable text (e.g. 234*12 or 2*x+3=7), otherwise null",
  "calc_guess": "your own result of that calculation"
}
- When there is a calculation, write the answer with {calc} where the result goes,
  e.g. "234 x 12 = {calc}" or "x = {calc}". A solver fills in the exact result.
  If the prompt asks about an equation on the board, copy that equation into "calculation".
- Answer directly. Only give a hint instead of the answer when the person asks for a hint.

or, if the prompt asks you to DRAW something:
{
  "kind": "drawing",
  "description": "what you are drawing",
  "shapes": [ ... ]
}
Shapes use board pixel coordinates (x right, y down), angles in degrees with 0 = right, 90 = down:
  {"type": "line", "from": [x, y], "to": [x, y]}
  {"type": "polyline", "points": [[x, y], ...], "closed": false}
  {"type": "rect", "x": left, "y": top, "w": width, "h": height}
  {"type": "circle", "center": [x, y], "r": radius}
  {"type": "ellipse", "center": [x, y], "rx": rx, "ry": ry}
  {"type": "arc", "center": [x, y], "r": radius, "start": deg, "end": deg}
  {"type": "curve", "points": [start, control, end]}
Draw it inside the box you are given, simply, with at most about 30 shapes, and avoid existing ink."""


def ask(prompt: str, png_bytes: bytes, strokes, at, box) -> dict:
    gridded, w, h = add_grid(png_bytes, strokes)
    images = []
    zoom = close_up(png_bytes, strokes)
    if zoom:
        images.append(("A close-up of what is already written on the board (for reading it):",
                       base64.b64encode(zoom).decode()))
    images.append((f"The full board ({w} x {h} px) with a coordinate grid:", base64.b64encode(gridded).decode()))
    text = (f"PROMPT: {prompt}\n\nThe answer will be written starting at ({at[0]:.0f}, {at[1]:.0f}). "
            f"If you draw, stay inside the box x {box[0]:.0f}-{box[2]:.0f}, y {box[1]:.0f}-{box[3]:.0f}.")
    if os.environ.get("OPENAI_API_KEY"):
        return _parse_json(_ask_openai(images, text, ASK_PROMPT))
    if os.environ.get("ANTHROPIC_API_KEY"):
        return _parse_json(_ask_claude(images, text, ASK_PROMPT))
    raise RuntimeError("No AI key set. Add OPENAI_API_KEY (or ANTHROPIC_API_KEY) to backend/.env")


REVIEW_TEXT = """REVIEW PASS. Image 1 is the board with YOUR additions drawn in BLUE (the person's ink is black).
Your previous answer was:
{previous}

Look carefully: do the blue parts connect to the black lines where they should, sit in the right place,
have sensible sizes, and make the drawing look finished? Respond with ONLY JSON:
- if it looks right: {{"mode": "drawing", "ok": true, "review": "one sentence"}}
- if anything is off: {{"mode": "drawing", "ok": false, "review": "what was wrong", "plan": "...", "shapes": [ ...the FULL corrected list of shapes, replacing the old one... ]}}
Use the same shape format and anchors as before."""


def review_drawing(png_bytes: bytes, user_strokes, new_strokes, stroke_summary: str, previous: dict) -> dict:
    """Show the AI its own result and let it fix mistakes once."""
    img = Image.open(io.BytesIO(png_bytes)).convert("RGB")
    d = ImageDraw.Draw(img)
    for s in new_strokes:
        if len(s) >= 2:
            d.line([tuple(p) for p in s], fill=(31, 87, 195), width=5, joint="curve")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    gridded, w, h = add_grid(buf.getvalue(), user_strokes)
    keep = {k: previous.get(k) for k in ("description", "plan", "shapes") if previous.get(k) is not None}
    text = (REVIEW_TEXT.format(previous=json.dumps(keep)[:6000])
            + "\n\nThe person's strokes, as exact coordinates with named corners:\n" + stroke_summary)
    images = [(f"IMAGE 1: the board ({w} x {h} px) with your additions in blue, grid and stroke labels.",
               base64.b64encode(gridded).decode())]
    if os.environ.get("OPENAI_API_KEY"):
        return _parse_json(_ask_openai(images, text))
    if os.environ.get("ANTHROPIC_API_KEY"):
        return _parse_json(_ask_claude(images, text))
    raise RuntimeError("No AI key set.")