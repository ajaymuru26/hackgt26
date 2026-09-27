"""
Send the whiteboard picture to Claude and get back structured JSON.

Before sending, we draw a faint labeled grid over the image. Vision models are
much better at giving accurate coordinates when they can read them off a grid.
"""
import base64
import io
import json
import os
import re

from PIL import Image, ImageDraw

# The board buttons pick OpenAI or Gemini. Claude is still available if you pass provider "claude".
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-5")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o")
# Handwriting reads use a smaller model. Drawings still use OPENAI_MODEL.
OPENAI_MATH_MODEL = os.environ.get("OPENAI_MATH_MODEL", "gpt-4o-mini")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.8-flash")
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
  (e.g. "1+1"), or null if they truly cannot be read as math. When math_reading is not null,
  also include "math_answer" (the solution you computed) and "math_kind"
  ("evaluate", "solve", "blank", or "check").

1) MATH: arithmetic or an equation (e.g. "5+4=", "12x3=", "5+_=9", "2x+3=7")
{
  "mode": "math",
  "description": "short phrase describing what you see",
  "reasoning": "one or two sentences: how you read the handwriting and any characters you were unsure about",
  "problems": [
    {"expression": "...", "answer": "...", "kind": "evaluate", "bbox": [x1, y1, x2, y2], "blank_bbox": [x1, y1, x2, y2] or null}
  ]
}
- The board may have SEVERAL separate problems (different lines or spots). Give each one its own
  entry in "problems", top to bottom. Never join problems together with ";" or newlines.
- "expression" is a TRANSCRIPTION of exactly what is written, without your solution.
  If the board shows "5+4=" the expression is "5+4=", not "5+4=9".
- YOU solve every problem. The answer you compute goes in "answer". Nothing else checks the maths.
  Arithmetic: "19". Several roots: "x=2 or x=3", never "x=2,3" (that looks like one number).
  A blank: the missing number only, such as "4".
  A polynomial with no equals sign is an equation set to 0. Solve x^2-5x+6 as "x=2 or x=3".
- "kind" is "evaluate" (write the result after the problem), "solve" (an equation, written underneath),
  "blank" (the missing number goes in the blank), or "check" (both sides are already filled in;
  answer is "✓" if they match and "✗" if they do not).
- If a line ends with "=" and nothing after it, keep the trailing "=".
- If the person left a blank (an underscore, an empty box, or a gap) replace it with _ ,
  e.g. "5+_=9" or "_x4=20", and give its location in "blank_bbox". Otherwise "blank_bbox" is null.
- A question mark is punctuation, not a blank. It is a curve with its own dot underneath.
  Keep that dot as part of the ?. Do not drop it and do not read it as a separate period.
- A period is a dot on the baseline. Keep it. 3.14 is not 314, and "Yes." keeps the period.
  Words with a ? or a period are a QUESTION, not math and not a drawing.
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
  "answer": "a short answer to write under the question, max about 15 words, plain ASCII"
}
- If the question is about maths, solve it yourself and put the finished result in "answer"
  (for example "x = -1, 3"). Do not leave a blank or a placeholder for some other program to fill in.
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
  {"type": "mirror", "strokes": ["S0", "S3"] or "all", "axis": "vertical"}
      -> copies those exact strokes, flipped, and places the copy BESIDE them.
         The fold is the right edge of those strokes, never the middle. The copy is placed
         on the right of that edge, so a left half of a face gains its right side beside it.
         Do not pass "at" and do not redraw the
         other side yourself. List only the strokes that should be copied (one side of a face,
         one wing). Those strokes are highlighted in green on the board.

Rules for drawings:
- You are also given the person's strokes as exact coordinates (S0, S1, ...) with their corners
  named K0, K1, ... Trust these over the picture.
- ANCHORS: anywhere a point [x, y] is expected you may write an anchor name instead:
  "K7" (a named corner), or "S2.start", "S2.end", "S2.top", "S2.bottom", "S2.left", "S2.right", "S2.center".
  Whenever a new line should touch the existing drawing, USE AN ANCHOR: it is exact, a guess is not.
  Example roof on a box whose top corners are K0 and K3: {"type": "polyline", "points": ["K0", ["CX", "T-0.5*W"], "K3"]}
- COORDINATES ARE COMPUTED, NOT GUESSED. A MEASURED FRAME is included with the strokes:
  L T R B are the left, top, right, bottom of the ink; W H its width and height; CX CY its centre.
  Corner names: TL TR BL BR. Edge midpoints: TC BC LC RC. Centre: C.
  Use expressions of those names for every coordinate and every radius. Examples:
  {"type": "roof"}  ends exactly on the top corners, peak exactly 0.5*W above the top edge
  {"type": "door"}  width exactly W/3, height exactly H/2, centred, bottom flush with B
  {"type": "window"}  side exactly W/5, centred in the upper part of the box
  {"type": "circle", "center": ["CX+0.18*W", "T+0.38*H"], "r": "0.07*W"}  a second eye
  Add "on": "S0" to measure from stroke S0 instead of the whole drawing.
  A point looks like ["CX", "T+0.4*H"] or "L+W/3". "r", "rx", and "ry" can be expressions too.
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


def _ask_openai(images: list[tuple[str, str]], user_text: str, system: str = None, *,
                model: str = None, detail: str = "high", max_tokens: int = None, mime: str = "image/png") -> str:
    """images = [(caption, base64), ...]"""
    from openai import OpenAI
    client = OpenAI()  # reads OPENAI_API_KEY
    content = []
    for caption, b64 in images:
        content.append({"type": "text", "text": caption})
        # "high" stops OpenAI from shrinking the picture, which blurs small handwriting
        content.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}", "detail": detail}})
    content.append({"type": "text", "text": user_text})
    kwargs = {}
    if max_tokens:
        kwargs["max_tokens"] = max_tokens
    response = client.chat.completions.create(
        model=model or OPENAI_MODEL,
        response_format={"type": "json_object"},
        temperature=0,
        messages=[{"role": "system", "content": system or SYSTEM_PROMPT}, {"role": "user", "content": content}],
        **kwargs,
    )
    return response.choices[0].message.content


def _gemini_models() -> list[str]:
    """Preferred model first, then others to try when Google says it is too busy."""
    fallbacks = ["gemini-3.7-flash", "gemini-3.6-flash", "gemini-3.1-flash-lite"]
    models = [GEMINI_MODEL]
    for name in fallbacks:
        if name not in models:
            models.append(name)
    return models


def _ask_gemini(images: list[tuple[str, str]], user_text: str, system: str = None, *,
                max_tokens: int = None, mime: str = "image/png", **_ignored) -> str:
    """images = [(caption, base64), ...]. Uses the Gemini REST API, no extra package."""
    import ssl
    import time
    import urllib.error
    import urllib.request

    import certifi

    key = os.environ.get("GEMINI_API_KEY", "")
    parts = []
    for caption, b64 in images:
        parts.append({"text": caption})
        parts.append({"inlineData": {"mimeType": mime, "data": b64}})
    parts.append({"text": user_text})
    generation = {"temperature": 0, "responseMimeType": "application/json"}
    if max_tokens:
        generation["maxOutputTokens"] = max_tokens
    body = json.dumps({
        "systemInstruction": {"parts": [{"text": system or SYSTEM_PROMPT}]},
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": generation,
    }).encode()
    context = ssl.create_default_context(cafile=certifi.where())
    last_error = None
    for model in _gemini_models():
        request = urllib.request.Request(
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
            data=body,
            headers={"Content-Type": "application/json", "x-goog-api-key": key},
        )
        for attempt in range(2):
            try:
                with urllib.request.urlopen(request, timeout=90, context=context) as response:
                    payload = json.loads(response.read().decode())
            except urllib.error.HTTPError as e:
                detail = e.read().decode(errors="replace")[:400]
                last_error = RuntimeError(f"Gemini request failed ({e.code}): {detail}")
                if e.code in (429, 503) and attempt == 0:
                    time.sleep(1.5)
                    continue
                if e.code in (429, 503):
                    break
                raise last_error from e
            else:
                _ask_gemini.last_model = model
                candidates = payload.get("candidates") or []
                if not candidates:
                    reason = (payload.get("promptFeedback") or {}).get("blockReason") or "no response"
                    raise RuntimeError(f"Gemini returned nothing ({reason}).")
                texts = [p.get("text", "") for p in candidates[0].get("content", {}).get("parts", []) if p.get("text")]
                if not texts:
                    raise RuntimeError("Gemini returned no text.")
                return "".join(texts)
    raise last_error or RuntimeError("Gemini is busy. Try again in a moment, or switch to OpenAI.")


def _ask_claude(images: list[tuple[str, str]], user_text: str, system: str = None, *,
                max_tokens: int = None, mime: str = "image/png", **_ignored) -> str:
    import anthropic
    client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY
    content = []
    for caption, b64 in images:
        content.append({"type": "text", "text": caption})
        content.append({"type": "image", "source": {"type": "base64", "media_type": mime, "data": b64}})
    content.append({"type": "text", "text": user_text})
    response = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=max_tokens or 4096,
        system=system or SYSTEM_PROMPT,
        messages=[{"role": "user", "content": content}],
    )
    return "".join(block.text for block in response.content if block.type == "text")


def ink_bbox(png_bytes: bytes, region=None):
    """Tight box around the writing in a picture, in board pixels.

    Vision models often return a box around the whole photo. The answer has to
    sit just after the actual problem, so this finds the ink itself.
    """
    img = Image.open(io.BytesIO(png_bytes)).convert("L")
    w, h = img.size
    if region and len(region) >= 4:
        x1 = max(0, int(float(region[0])))
        y1 = max(0, int(float(region[1])))
        x2 = min(w, int(float(region[2])))
        y2 = min(h, int(float(region[3])))
    else:
        x1, y1, x2, y2 = 0, 0, w, h
    if x2 - x1 < 8 or y2 - y1 < 8:
        return None
    crop = img.crop((x1, y1, x2, y2))
    cw, ch = crop.size
    step_x = max(1, cw // 40)
    step_y = max(1, ch // 40)
    samples = [crop.getpixel((x, 0)) for x in range(0, cw, step_x)]
    samples += [crop.getpixel((x, ch - 1)) for x in range(0, cw, step_x)]
    samples += [crop.getpixel((0, y)) for y in range(0, ch, step_y)]
    samples += [crop.getpixel((cw - 1, y)) for y in range(0, ch, step_y)]
    samples.sort()
    bg = samples[len(samples) // 2]
    mask = crop.point([255 if abs(i - bg) > 36 else 0 for i in range(256)])
    raw = bytearray(mask.tobytes())
    for y in range(ch):
        if sum(1 for v in raw[y * cw:(y + 1) * cw] if v) > 0.82 * cw:
            raw[y * cw:(y + 1) * cw] = b"\x00" * cw
    for x in range(cw):
        if sum(1 for y in range(ch) if raw[y * cw + x]) > 0.82 * ch:
            for y in range(ch):
                raw[y * cw + x] = 0
    rows = [sum(raw[y * cw:(y + 1) * cw]) for y in range(ch)]
    bands, start = [], None
    for y, ink in enumerate(rows + [0]):
        if ink and start is None:
            start = y
        elif not ink and start is not None:
            if y - start >= 8:
                bands.append((start, y, sum(rows[start:y])))
            start = None
    if not bands:
        return None
    y0, y1b, _ = max(bands, key=lambda b: b[2])
    xs = [x for y in range(y0, y1b) for x in range(cw) if raw[y * cw + x]]
    if not xs:
        return None
    return (float(x1 + min(xs)), float(y1 + y0), float(x1 + max(xs) + 1), float(y1 + y1b))


def close_up(png_bytes: bytes, strokes, pad=40, target=1100, resample=Image.LANCZOS) -> bytes | None:
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
        crop = crop.resize((int(crop.width * scale), int(crop.height * scale)), resample)
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


def provider_name(provider: str = "openai") -> str:
    name = (provider or "openai").lower()
    if name == "gemini":
        return f"Gemini {GEMINI_MODEL}"
    if name == "claude":
        return f"Claude {CLAUDE_MODEL}"
    return f"OpenAI {OPENAI_MODEL}"


def _dispatch(images, user_text, system, provider: str, **call):
    """Send the board to whichever model the buttons selected."""
    name = (provider or "openai").lower()
    if name == "gemini":
        if not os.environ.get("GEMINI_API_KEY"):
            raise RuntimeError("No Gemini key. Add GEMINI_API_KEY=... to backend/.env, save, and restart the server.")
        return _parse_json(_ask_gemini(images, user_text, system, **call))
    if name == "claude":
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise RuntimeError("No Anthropic key. Add ANTHROPIC_API_KEY to backend/.env")
        return _parse_json(_ask_claude(images, user_text, system, **call))
    if not os.environ.get("OPENAI_API_KEY"):
        raise RuntimeError("No OpenAI key. Add OPENAI_API_KEY to backend/.env")
    return _parse_json(_ask_openai(images, user_text, system, **call))


ACTION_TEXT = {
    "answer": "The person pressed ANSWER: they want the answer. Pick math, fill or answer (question) mode. "
              "Do NOT pick drawing, even if the strokes look like letters or shapes.",
    "work": """The person pressed ANSWER BUT SHOW WORK. They want the answer and the steps written on the board.
Pick math, fill, or answer (question) mode. Do NOT pick drawing.
For math, each problem still has expression, answer, kind, and bbox, and also:
"work": "the steps, plain ASCII, one step per line, at most 5 lines. The last line is the final answer.
Use * for multiply and ^ for powers. No markdown.
Example:
(x-2)(x-3)=0
x=2 or x=3"
For a written question, put those same steps in top-level "work" and the short final result in "answer".
For a pattern, "work" is one line naming the rule, and "text" is still what to write next.""",
    "hint": """The person pressed HINT. Do not give the final answer. Respond with:
{"mode": "hint", "description": "what is on the board", "reasoning": "what the next step is and why",
 "hint": "ONE short hint, max 15 words, that helps them take the next step without giving away the answer"}
This works for maths, questions, patterns and drawings (e.g. "What do houses have on top?").""",
    "check": """The person pressed CHECK MY WORK. Read THEIR problem, THEIR steps, and THEIR answer. Respond with:
{"mode": "check", "description": "what is on the board", "reasoning": "anything you were unsure reading",
 "problems": [
   {"expression": "5+4=", "work": "5+4=10", "student_answer": "10",
    "correct": false, "correct_answer": "9", "missing": "", "bbox": [x1, y1, x2, y2]},
   {"equation": "2*x+3=7", "work": "2*x=4", "student_answer": null,
    "correct": null, "correct_answer": "x=2", "missing": "They stopped before dividing by 2.",
    "bbox": [x1, y1, x2, y2]}
 ]}
- Copy every line they wrote, even when it is wrong or unfinished. Never replace their work with the right work.
- "expression" or "equation" is the problem. "work" is their steps, one per line. "student_answer" is only their final result, or null if they did not finish.
- "correct" is true, false, or null when no final answer is there. "correct_answer" is the right result.
- "missing" says what they left out. Leave it empty when the work is complete.
- "bbox" covers that problem and the work under it. One entry per problem, top to bottom.""",
}


FAST_PROMPT = """You read handwriting for a classroom whiteboard robot. Reply with ONLY a JSON object.

Read left to right. These are usually digits, not letters: s/S=5, z/Z=2, l/I/|=1, o/O=0, g/q=9, b=6, B=8, T=7, t=+.
An x between two numbers means multiply. Use * for multiply, / for divide, and ^ for powers.
A blank is only an underscore or an empty box. Write it as _. A question mark is not a blank.

Keep every period and every question mark.
- A period is a dot sitting on the baseline. 3.14 is not 314. "Done." keeps the period.
- A question mark is a curve with a separate dot underneath. Read both parts as one ?.
  Do not drop that dot, and do not read it as its own period.
- A line of words, with or without ? or ., is mode answer. It is not math and not a drawing.
- Never use mode drawing. A photo of writing is still writing.

{"mode":"math","description":"short","problems":[{"expression":"12+7="}]}
Transcription only. Do not solve. Keep a trailing =. One object per problem, top to bottom. Never join problems.

{"mode":"fill","description":"short","text":"what to write next","reasoning":"the rule in one line"}

{"mode":"answer","question":"the words you read, keeping . and ?","answer":"short plain ASCII, max 15 words","reasoning":"one sentence"}

{"mode":"hint","description":"short","hint":"one nudge, max 12 words, no final answer","reasoning":"one sentence"}

{"mode":"check","description":"short","problems":[{"expression":"5+4=","work":"5+4=10","student_answer":"10","correct":false,"correct_answer":"9","missing":""}]}
Read their problem, every step, and their final answer exactly, even if it is wrong or unfinished.
student_answer is null when they did not finish. missing says what they left out, or "" when nothing is missing.
correct is true, false, or null. correct_answer is the right result, such as 9 or x=2.
"""

PHOTO_READ = (
    "This is a photo of a whiteboard, not a picture to finish. Read the handwriting. "
    "Never respond with mode drawing. Keep every period (.) and question mark (?)."
)

FAST_ACTION = {
    "answer": "They pressed Answer. Math or a number pattern: mode math or fill, and do not compute the result. "
              "Written words, including any . or ?: mode answer. Never mode drawing.",
    "work": "They pressed Show work. Transcribe math or a pattern and do not compute it. Written words: mode answer.",
    "hint": "They pressed Hint. Respond with mode hint only.",
    "check": "They pressed Check my work. Respond with mode check only. Copy the problem, every step, and their final answer exactly, even if it is wrong or unfinished. Say what is missing.",
    "recommend": """They pressed Recommended action. Choose the one button that fits, then transcribe the board for that button.
Pick exactly one recommend value:
- check: they already wrote an answer or a finished result, even if it is wrong (2+2=5, x=3)
- hint: they started steps and stopped before a final answer
- work: the problem needs more than one step and they have not started (algebra, several operations, a long multiply)
- answer: a short unfinished problem, a pattern, or a written question
- drawing: a picture, not writing. Mode drawing is allowed here.
Reply with {"recommend":"check","why":"one short sentence","mode":"check", ...the fields that mode needs}.
For math, always include problems with expression, work, and student_answer copied exactly. student_answer is null if they did not finish.
For a question, include question and a short answer. For a hint, include hint. For a picture, mode drawing and a short description only.""",
}


def fast_provider_name(provider: str = "openai") -> str:
    name = (provider or "openai").lower()
    if name == "gemini":
        return f"Gemini {GEMINI_MODEL}"
    if name == "claude":
        return f"Claude {CLAUDE_MODEL}"
    return f"OpenAI {OPENAI_MATH_MODEL}"


def _jpeg_b64(png_bytes: bytes, max_edge: int) -> tuple[str, float]:
    """Shrink a board photo to a JPEG. Returns (base64, scale applied to pixel coordinates)."""
    img = Image.open(io.BytesIO(png_bytes)).convert("RGB")
    scale = 1.0
    edge = max(img.size)
    if edge > max_edge:
        scale = max_edge / edge
        img = img.resize((max(1, int(img.width * scale)), max(1, int(img.height * scale))), Image.BILINEAR)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=80)
    return base64.b64encode(buf.getvalue()).decode(), scale


def _scale_boxes(ai: dict, factor: float) -> None:
    if factor == 1:
        return
    for prob in ai.get("problems") or []:
        if not isinstance(prob, dict):
            continue
        for key in ("bbox", "blank_bbox"):
            box = prob.get(key)
            if isinstance(box, list) and len(box) >= 4:
                prob[key] = [float(v) / factor for v in box[:4]]


def read_fast(png_bytes: bytes, strokes=None, action: str = "", image_box=None, provider: str = "openai",
              note: str = "") -> dict:
    """One picture, a short prompt, and no solving. SymPy does the maths afterwards.

    An uploaded photo is sent whole. A close-up was cutting the problem off.
    """
    if strokes and not image_box:
        zoom = close_up(png_bytes, strokes, target=800, resample=Image.BILINEAR)
        b64, _scale = _jpeg_b64(zoom or png_bytes, 900)
        caption = "Close-up of the handwriting. Read the characters. Do not return coordinates."
        scale = None
    else:
        b64, scale = _jpeg_b64(png_bytes, 1024)
        caption = ("The whiteboard. Read each problem from top to bottom and include "
                   "bbox [x1, y1, x2, y2] in these pixels.")
    user = FAST_ACTION.get(action, FAST_ACTION["answer"])
    if image_box:
        user += "\n\n" + PHOTO_READ
    if note and note.strip():
        user += "\n\n" + note.strip()[:800]
    call = {"max_tokens": 1200 if action in ("check", "recommend") else 700, "mime": "image/jpeg", "detail": "high"}
    if (provider or "openai").lower() not in ("gemini", "claude"):
        call["model"] = OPENAI_MATH_MODEL
    ai = _dispatch([(caption, b64)], user, FAST_PROMPT, provider, **call)
    if scale is not None:
        _scale_boxes(ai, scale)
    return ai


IMAGE_TEXT = ("The whiteboard IS an uploaded picture filling x {0:.0f}-{2:.0f}, y {1:.0f}-{3:.0f}. "
              "There is no blank margin around it. Treat what is in the picture exactly like writing on the board: "
              "solve, answer, hint or check the problems shown in it. Give every bbox in board pixels, tight around "
              "that one problem where it appears in the picture. Answers are written ON TOP of the picture, beside "
              "each problem, so do not use the outer edge of the picture as a bbox. "
              "If the picture has several problems, list each one.")


def analyze_board(png_bytes: bytes, stroke_summary: str = "", strokes=None, mode: str = "auto",
                  action: str = "", image_box=None, provider: str = "openai", instruction: str = "") -> dict:
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
    if instruction and instruction.strip():
        note = instruction.strip()[:400]
        user_text += ("\n\nThe person typed this instruction for how to change the drawing. "
                      "Follow it. Change only what they asked for, and still respond with mode \"drawing\". "
                      "Place every new part with roof/door/window or with expressions of the MEASURED FRAME, "
                      "not guessed pixels:\n" + note)
        if re.search(r"reflect|mirror|both sides|other side", note, re.I):
            user_text += ("\n\nThis is a reflection. Return one mirror shape and list only the strokes "
                          "to copy. Do not draw the other side yourself and do not use the centre as the "
                          "fold. The program highlights those strokes in green and places the flipped copy "
                          "across the right edge, so the copy sits on the right of the original.")
    if stroke_summary:
        user_text += ("\n\nThe person's strokes, as exact coordinates "
                      "(start/end points, bounding box, and a sampled path):\n" + stroke_summary)

    return _dispatch(images, user_text, None, provider)


ASK_PROMPT = """You are a robot that writes on a real whiteboard with a marker.
The person typed a prompt. Answer it so the robot can write the answer on the board.
You also get a picture of the board: use it when the prompt refers to it ("give me a hint",
"is this right?", "what shape is this?"). Otherwise just answer the prompt.

Respond with ONLY a JSON object, no markdown. One of:

{
  "kind": "text",
  "reasoning": "one or two sentences on how you answered",
  "answer": "the text to write: short, max about 20 words, plain ASCII, no emoji or markdown"
}
- If the prompt needs arithmetic or algebra, solve it yourself and put the finished numbers in "answer".
  Do not leave a placeholder. Example: "234 x 12 = 2808" or "x = 2".
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


SPEAK_PROMPT = """You are a robot that draws on a real whiteboard with a marker.
The person spoke out loud. Draw what they described.

Respond with ONLY a JSON object, no markdown.

If they described a picture, object, diagram, or said "draw":
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
Draw it inside the box you are given, simply, with at most about 30 shapes, and avoid existing ink.
A spoken request is a drawing unless they clearly asked a question or for a calculation.

If they asked a question or for maths ("1 + 1", "do 1 + 1", "what is 12 plus 7"), respond with:
{
  "kind": "text",
  "reasoning": "one or two sentences",
  "problem": "the problem restated as it should be written on the board, plain ASCII, no markdown. Arithmetic ends with =, e.g. \\"1 + 1 =\\". A question is the question itself.",
  "answer": "only the result to write on the next line, short plain ASCII, not a repeat of the problem"
}
Numbers and sums are text, not drawings."""


def ask(prompt: str, png_bytes: bytes, strokes, at, box, provider: str = "openai", system: str = None) -> dict:
    gridded, w, h = add_grid(png_bytes, strokes)
    images = []
    zoom = close_up(png_bytes, strokes)
    if zoom:
        images.append(("A close-up of what is already written on the board (for reading it):",
                       base64.b64encode(zoom).decode()))
    images.append((f"The full board ({w} x {h} px) with a coordinate grid:", base64.b64encode(gridded).decode()))
    text = (f"PROMPT: {prompt}\n\nThe answer will be written starting at ({at[0]:.0f}, {at[1]:.0f}). "
            f"If you draw, stay inside the box x {box[0]:.0f}-{box[2]:.0f}, y {box[1]:.0f}-{box[3]:.0f}.")
    return _dispatch(images, text, system or ASK_PROMPT, provider)


def transcribe(audio: bytes, mime: str = "audio/webm") -> str:
    """Turn a short recording into text with ElevenLabs speech-to-text."""
    import ssl
    import urllib.error
    import urllib.request

    import certifi

    key = os.environ.get("ELEVENLABS_API_KEY", "")
    if not key:
        raise RuntimeError("No ElevenLabs key. Add ELEVENLABS_API_KEY to backend/.env and restart the server.")
    kind = (mime or "audio/webm").split(";", 1)[0].strip().lower()
    ext = {"audio/webm": "webm", "audio/mp4": "mp4", "audio/mpeg": "mp3", "audio/wav": "wav", "audio/ogg": "ogg"}.get(kind, "webm")
    model = os.environ.get("ELEVENLABS_STT_MODEL", "scribe_v2")
    boundary = "----whiteboard-speech"
    head = (
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"model_id\"\r\n\r\n{model}\r\n"
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"speech.{ext}\"\r\n"
        f"Content-Type: {kind}\r\n\r\n"
    ).encode()
    body = head + audio + f"\r\n--{boundary}--\r\n".encode()
    request = urllib.request.Request(
        "https://api.elevenlabs.io/v1/speech-to-text",
        data=body,
        headers={"xi-api-key": key, "Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    context = ssl.create_default_context(cafile=certifi.where())
    try:
        with urllib.request.urlopen(request, timeout=60, context=context) as response:
            payload = json.loads(response.read().decode())
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:300]
        if e.code in (401, 403):
            raise RuntimeError("ElevenLabs rejected the key. Check ELEVENLABS_API_KEY in backend/.env.") from e
        raise RuntimeError(f"ElevenLabs couldn't transcribe that ({e.code}): {detail}") from e
    text = str(payload.get("text") or "").strip()
    if not text:
        raise RuntimeError("I didn't catch any words. Try speaking a bit louder and closer.")
    return text[:500]


REVIEW_TEXT = """The drawing is finished. Image 1 shows it: YOUR additions are BLUE, the person's ink is black.
If you see no blue ink, the first attempt added nothing. That is not ok.
This is check {check} of {limit}. Your previous answer was:
{previous}

Go through this checklist in order. Answer every item. "pass" is true only when that item is fully true.
1. Missing parts — every part named in the plan is drawn in blue.
2. Placement — each new part is on the correct side of the drawing, not through the middle of it.
3. Size — each new part matches the scale of the ink already there.
4. Connection — new parts meet the existing lines where they should, and do not float.
5. No duplicate — blue ink does not trace over what the person already drew.

"ok" is true only when all five pass and blue ink is on the board. Otherwise "ok" is false and "shapes" is the full corrected drawing.
Respond with ONLY JSON:
{{"mode": "drawing", "ok": false, "review": "one sentence", "checklist": [
  {{"item": "Missing parts", "pass": false, "note": "what you see"}},
  {{"item": "Placement", "pass": true, "note": "what you see"}},
  {{"item": "Size", "pass": true, "note": "what you see"}},
  {{"item": "Connection", "pass": true, "note": "what you see"}},
  {{"item": "No duplicate", "pass": true, "note": "what you see"}}
], "plan": "what you will change", "shapes": [ ...the FULL corrected list, or [] if ok is true... ]}}
Use the same shape format and anchors as before.
Judge it against the plan. A copy of the whole drawing is only ok when the plan was to reflect one side."""


def review_drawing(png_bytes: bytes, user_strokes, new_strokes, stroke_summary: str, previous: dict,
                   provider: str = "openai", check: int = 1, limit: int = 3, problems: str = "") -> dict:
    """Show the AI its own result so it can accept it or say what to change."""
    img = Image.open(io.BytesIO(png_bytes)).convert("RGB")
    d = ImageDraw.Draw(img)
    for s in new_strokes:
        if len(s) >= 2:
            d.line([tuple(p) for p in s], fill=(31, 87, 195), width=5, joint="curve")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    gridded, w, h = add_grid(buf.getvalue(), user_strokes)
    keep = {k: previous.get(k) for k in ("description", "plan", "shapes") if previous.get(k) is not None}
    text = (REVIEW_TEXT.format(previous=json.dumps(keep)[:6000], check=check, limit=limit)
            + "\n\nThe person's strokes, as exact coordinates with named corners:\n" + stroke_summary)
    if problems:  # measured in code (shapes.conflict_note), so trust it over the picture
        text += "\n\n" + problems
    images = [(f"IMAGE 1: the board ({w} x {h} px) with your additions in blue, grid and stroke labels.",
               base64.b64encode(gridded).decode())]
    return _dispatch(images, text, None, provider)