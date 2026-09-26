// ---------- setup ----------
// The board always uses the same logical size (1200 x 700), no matter how big
// it looks on screen. The backend and the robot use these same coordinates.
const W = 1200, H = 700;
const COLORS = { user: "#1c2230", robot: "#1f57c3" };
const LINE_WIDTH = 5;

// Robot simulator. Speeds match the real plotter at "Real" speed:
// G-code draws at F3000 = 50 mm/s, and the board is 800 mm wide = 1200 px (1.5 px per mm).
const BOARD_MM = { w: 800, h: 500 };
const DRAW_RATE = 75;     // px/s with the pen down (real plotter speed)
const TRAVEL_RATE = 180;  // px/s with the pen up
const PEN_TIME = 0.15;    // seconds to lift or lower the pen (G4 P0.15)
const HOME = [0, H];      // machine origin X0 Y0 = bottom-left of the board

const canvas = document.getElementById("board");
const ctx = canvas.getContext("2d");
const $ = (id) => document.getElementById(id);

let strokes = [];       // { owner: "user" | "robot", points: [[x, y], ...] }
let history = [];       // snapshots for undo
let current = null;     // stroke being drawn
let tool = "pen";
let busy = false;
let lastRobotStrokes = [];
let robot = null;       // live simulator state while the robot is drawing
let lastRobotObjs = [];  // stroke objects the robot added last time (for replay)
let background = null;  // uploaded picture under the sketch and the answer: { img, x, y, w, h }
let timedFrom = 0;      // start of an image run, so the status can show total time

function resize() {
  const dpr = window.devicePixelRatio || 1;
  canvas.width = W * dpr;
  canvas.height = H * dpr;
  ctx.setTransform(canvas.width / W, 0, 0, canvas.height / H, 0, 0);
  render();
}

// ---------- drawing ----------
function traceStroke(c, pts) {
  c.beginPath();
  if (pts.length === 1) {
    c.arc(pts[0][0], pts[0][1], LINE_WIDTH / 2, 0, Math.PI * 2);
    return "fill";
  }
  c.moveTo(pts[0][0], pts[0][1]);
  for (let i = 1; i < pts.length; i++) c.lineTo(pts[i][0], pts[i][1]);
  return "stroke";
}

function drawStroke(c, s) {
  const pts = s.points;
  if (!pts.length) return;
  c.lineCap = "round";
  c.lineJoin = "round";
  // White edge so the sketch and the answer stay readable on the photo
  if (background && (s.owner === "robot" || s.owner === "user")) {
    c.strokeStyle = "#ffffff";
    c.fillStyle = "#ffffff";
    c.lineWidth = LINE_WIDTH + 7;
    if (traceStroke(c, pts) === "fill") c.fill();
    else c.stroke();
  }
  c.strokeStyle = COLORS[s.owner];
  c.fillStyle = COLORS[s.owner];
  c.lineWidth = LINE_WIDTH;
  if (traceStroke(c, pts) === "fill") c.fill();
  else c.stroke();
}

function render() {
  ctx.clearRect(0, 0, W, H);
  if (background) {
    ctx.save();
    ctx.drawImage(background.img, background.x, background.y, background.w, background.h);
    ctx.restore();
  }
  strokes.forEach((s) => drawStroke(ctx, s));
  if (current) drawStroke(ctx, current);
  if (robot) drawRobot(ctx);
  $("empty").hidden = strokes.length > 0 || !!current || !!background;
}

function toBoard(e) {
  const r = canvas.getBoundingClientRect();
  return [((e.clientX - r.left) / r.width) * W, ((e.clientY - r.top) / r.height) * H];
}

function saveHistory() {
  history.push(JSON.stringify(strokes));
  if (history.length > 100) history.shift();
}

function eraseAt(p) {
  const radius = 14;
  const before = strokes.length;
  strokes = strokes.filter((s) => !s.points.some((q) => Math.hypot(q[0] - p[0], q[1] - p[1]) < radius));
  if (strokes.length !== before) render();
}

canvas.addEventListener("pointerdown", (e) => {
  if (busy) return;
  if (tool === "text") { e.preventDefault(); openTextBox(toBoard(e)); return; }
  canvas.setPointerCapture(e.pointerId);
  saveHistory();
  const p = toBoard(e);
  if (tool === "eraser") { eraseAt(p); return; }
  current = { owner: "user", points: [p] };
  render();
});

canvas.addEventListener("pointermove", (e) => {
  if (busy || !canvas.hasPointerCapture(e.pointerId)) return;
  const p = toBoard(e);
  if (tool === "eraser") { eraseAt(p); return; }
  if (!current) return;
  const last = current.points[current.points.length - 1];
  if (Math.hypot(p[0] - last[0], p[1] - last[1]) > 2) {  // skip tiny jitter
    current.points.push(p);
    render();
  }
});

function endStroke() {
  if (current) {
    current.points = current.points.map(([x, y]) => [Math.round(x * 10) / 10, Math.round(y * 10) / 10]);
    strokes.push(current);
    current = null;
    render();
  }
}
canvas.addEventListener("pointerup", endStroke);
canvas.addEventListener("pointercancel", endStroke);

// ---------- tools ----------
function setTool(t) {
  tool = t;
  $("penBtn").classList.toggle("is-on", t === "pen");
  $("eraserBtn").classList.toggle("is-on", t === "eraser");
  $("penBtn").setAttribute("aria-pressed", t === "pen");
  $("eraserBtn").setAttribute("aria-pressed", t === "eraser");
  $("textBtn").classList.toggle("is-on", t === "text");
  $("textBtn").setAttribute("aria-pressed", t === "text");
  canvas.classList.toggle("erasing", t === "eraser");
  canvas.classList.toggle("texting", t === "text");
  if (t !== "text") closeTextBox(true);
}

function undo() {
  if (busy || !history.length) return;
  strokes = JSON.parse(history.pop());
  render();
}

$("penBtn").onclick = () => setTool("pen");
$("eraserBtn").onclick = () => setTool("eraser");
$("textBtn").onclick = () => setTool("text");
$("undoBtn").onclick = undo;
$("clearBtn").onclick = () => {
  if (busy || (!strokes.length && !background)) return;
  saveHistory();
  strokes = [];
  lastRobotStrokes = [];
  lastRobotObjs = [];
  background = null;
  $("gcodeBtn").disabled = true;
  $("replayBtn").disabled = true;
  setStatus("");
  render();
};

document.addEventListener("keydown", (e) => {
  if (e.target.matches("input, select, textarea")) return; // typing text shouldn't trigger shortcuts
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "z") { e.preventDefault(); undo(); }
  else if (e.key.toLowerCase() === "p" && !e.ctrlKey && !e.metaKey) setTool("pen");
  else if (e.key.toLowerCase() === "e" && !e.ctrlKey && !e.metaKey) setTool("eraser");
  else if (e.key.toLowerCase() === "t" && !e.ctrlKey && !e.metaKey) setTool("text");
});

function setStatus(text, kind = "") {
  const el = $("status");
  el.textContent = text;
  el.className = "status" + (kind ? " is-" + kind : "");
}

function setBusy(on) {
  busy = on;
  document.querySelectorAll(".action").forEach((b) => {
    b.disabled = on;
    if (!on) b.classList.remove("is-busy");
  });
  ["undoBtn", "clearBtn", "textBtn", "imageBtn"].forEach((id) => ($(id).disabled = on));
  ["gcodeBtn", "replayBtn"].forEach((id) => ($(id).disabled = on || !lastRobotStrokes.length));
}

// ---------- snapshot + submit ----------
function snapshot() {
  // Render at logical size on a white background (a transparent PNG confuses the AI)
  const off = document.createElement("canvas");
  off.width = W; off.height = H;
  const c = off.getContext("2d");
  c.fillStyle = "#ffffff";
  c.fillRect(0, 0, W, H);
  // Once the picture has been traced, the model sees the sketch on white, same as a drawing.
  // The photo stays on screen underneath.
  const sketched = strokes.some((s) => s.owner === "user");
  if (background && !sketched) c.drawImage(background.img, background.x, background.y, background.w, background.h);
  strokes.forEach((s) => drawStroke(c, { ...s, owner: "user" }));
  return off.toDataURL("image/png");
}

// ---------- thought process panel ----------
function clearThoughts() {
  $("thoughtList").innerHTML = "";
  $("thoughtsEmpty").hidden = true;
  $("problemType").hidden = true;
}

function showProblemType(category) {
  const el = $("problemType");
  if (!category) return;
  el.dataset.type = category.type;
  el.innerHTML = "";
  const strong = document.createElement("strong");
  strong.textContent = category.type;
  const span = document.createElement("span");
  span.textContent = category.detail;
  el.append(strong, span);
  el.hidden = false;
}

function addThought(title, detail, kind = "") {
  const li = document.createElement("li");
  li.className = "thought" + (kind ? " is-" + kind : "");
  const t = document.createElement("p");
  t.className = "thought-title";
  t.textContent = title;
  const d = document.createElement("p");
  d.className = "thought-detail";
  d.textContent = detail || "";
  li.append(t, d);
  $("thoughtList").append(li);
  li.scrollIntoView({ block: "nearest" });
  return li;
}

const wait = (ms) => new Promise((r) => setTimeout(r, ms));

async function showThoughts(steps) {
  const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  for (const s of steps || []) {
    addThought(s.title, s.detail);
    if (!reduce) await wait(350);
  }
}

const ACTION_WORDS = { answer: "Working out the answer", hint: "Thinking of a hint",
                       check: "Checking your work", drawing: "Looking at your drawing" };

async function runAction(action, button) {
  if (busy) return;
  if (!strokes.length && !background) { setStatus("The board is empty. Write, draw, or add an image first.", "error"); return; }

  setBusy(true);
  button.classList.add("is-busy");
  setStatus(`${ACTION_WORDS[action] || "Looking at the board"}…`);
  clearThoughts();
  const live = addThought("Thinking", "Sending the board to the AI… 0.0 s", "live");
  const t0 = timedFrom || performance.now();
  timedFrom = 0;
  const ticker = setInterval(() => {
    live.querySelector(".thought-detail").textContent =
      `Sending the board to the AI… ${((performance.now() - t0) / 1000).toFixed(1)} s`;
  }, 100);
  try {
    const res = await fetch("/api/complete", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ image: snapshot(), width: W, height: H, action, review: $("reviewToggle").checked,
                             image_box: strokes.some((s) => s.owner === "user") ? null : imageBox(),
                             strokes: strokes.map((s) => s.points) }),
    });
    const data = await res.json().catch(() => ({}));
    clearInterval(ticker);
    live.remove();
    if (!res.ok) throw new Error(data.detail || `Server error ${res.status}`);
    showProblemType(data.category);
    await showThoughts(data.steps);

    const label = {
      math: `Read ${data.expression} and wrote ${data.answer}`,
      fill: `${data.description || "Next up"}: wrote ${data.answer}`,
      answer: `Answered: ${data.answer}`,
      hint: data.answer,
      check: `Checked your work: ${data.answer}`,
    }[data.mode] || `Finishing ${data.description || "the drawing"}`;
    const runtime = ((performance.now() - t0) / 1000).toFixed(1);
    setStatus(`${label} · ${runtime} s`, "robot");
    addThought("Runtime", `${runtime} s`);

    saveHistory();
    lastRobotStrokes = data.strokes;
    const drawing = addThought("Drawing", "The robot is drawing it on the board now.", "live");
    await animateRobot(data.strokes);
    drawing.classList.remove("is-live");
    const total = ((performance.now() - t0) / 1000).toFixed(1);
    drawing.querySelector(".thought-title").textContent = "Done";
    drawing.querySelector(".thought-detail").textContent = `Finished in ${total} s. Press Replay to watch again.`;
    const runtimeThought = [...document.querySelectorAll(".thought")].find((el) => el.querySelector(".thought-title")?.textContent === "Runtime");
    if (runtimeThought) runtimeThought.querySelector(".thought-detail").textContent = `${total} s`;
    const finished = data.mode !== "drawing" ? label : `Finished ${data.description || "the drawing"}`;
    setStatus(`${finished} · ${total} s`, "robot");
  } catch (err) {
    clearInterval(ticker);
    live.remove();
    setStatus(err.message, "error");
    addThought("Something went wrong", err.message, "error");
  } finally {
    setBusy(false);
  }
};

document.querySelectorAll(".action").forEach((btn) => {
  btn.addEventListener("click", () => runAction(btn.dataset.action, btn));
});

// ---------- Text tool: click the board and type ----------
let textBox = null; // the live input on the board

function boardToCss([x, y]) {
  const c = canvas.getBoundingClientRect();
  const f = canvas.parentElement.getBoundingClientRect();
  return [c.left - f.left + (x / W) * c.width, c.top - f.top + (y / H) * c.height, c.width / W];
}

function openTextBox(point) {
  if (textBox) { closeTextBox(true); return; } // clicking away finishes the current box first
  const size = Number($("writeSize").value);
  const style = $("writeStyle").value;
  const at = [point[0], Math.max(10, point[1] - size / 2)]; // centre the text on the click
  const [left, top, scale] = boardToCss(at);
  const input = document.createElement("input");
  input.type = "text";
  input.maxLength = 400;
  input.className = "board-input";
  input.setAttribute("aria-label", "Prompt for the robot");
  input.placeholder = $("askMode").value === "answer" ? "Ask anything, then Enter" : "Type, then Enter";
  input.style.left = `${left}px`;
  input.style.top = `${top - size * scale * 0.25}px`;
  input.style.fontSize = `${Math.max(14, size * scale * 1.35)}px`;
  if (style === "cursive") input.style.fontStyle = "italic";
  canvas.parentElement.append(input);
  textBox = { input, at, size, style };

  input.addEventListener("keydown", (e) => {
    e.stopPropagation();
    if (e.key === "Enter") { e.preventDefault(); closeTextBox(true); }
    if (e.key === "Escape") { e.preventDefault(); closeTextBox(false); }
  });
  input.addEventListener("blur", () => setTimeout(() => {
    if (textBox && textBox.input === input) closeTextBox(true);
  }, 0));
  requestAnimationFrame(() => input.focus());
  setStatus("Type your prompt, then press Enter (Esc to cancel).", "robot");
}

function closeTextBox(submit) {
  if (!textBox) return;
  const { input, at, size, style } = textBox;
  textBox = null;
  const text = input.value.trim();
  const askMode = $("askMode").value;
  if (submit && text && askMode === "answer") {
    // keep the prompt visible while the AI thinks; it becomes ink when the answer arrives
    input.readOnly = true;
    input.classList.add("is-waiting");
    writeAt(at, { text, size, style, askMode, input });
    return;
  }
  input.remove();
  if (submit && text) writeAt(at, { text, size, style, askMode });
  else setStatus("");
}

async function writeAt([x, y], job) {
  if (busy) return;
  setBusy(true);
  clearThoughts();
  const asking = job.askMode === "answer";
  setStatus(asking ? "Thinking about your prompt…" : "Laying out the text…");
  let live = null, ticker = null;
  if (asking) {
    live = addThought("Thinking", "Asking the AI… 0.0 s", "live");
    const t0 = performance.now();
    ticker = setInterval(() => {
      live.querySelector(".thought-detail").textContent = `Asking the AI… ${((performance.now() - t0) / 1000).toFixed(1)} s`;
    }, 100);
  }
  try {
    const body = asking
      ? { prompt: job.text, image: snapshot(), strokes: strokes.map((s) => s.points),
          x, y, size: job.size, style: job.style, width: W, height: H }
      : { text: job.text, x, y, size: job.size, style: job.style, width: W, height: H };
    const res = await fetch(asking ? "/api/ask" : "/api/write", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await res.json().catch(() => ({}));
    if (ticker) { clearInterval(ticker); live.remove(); }
    if (!res.ok) throw new Error(data.detail || `Server error ${res.status}`);
    saveHistory(); // one Undo removes the prompt and the answer together
    if (job.input) job.input.remove();
    (data.prompt_strokes || []).forEach((pts) => strokes.push({ owner: "user", points: pts }));
    render();
    showProblemType(data.category);
    await showThoughts(data.steps);
    lastRobotStrokes = data.strokes;
    setStatus(data.mode === "drawing" ? `Drawing ${data.description}` : data.answer ? `Writing: ${data.answer}` : "Writing", "robot");
    const drawing = addThought(data.mode === "drawing" ? "Drawing" : "Writing", "The robot is on the board now.", "live");
    await animateRobot(data.strokes);
    drawing.classList.remove("is-live");
    drawing.querySelector(".thought-title").textContent = "Done";
    drawing.querySelector(".thought-detail").textContent = "Finished writing. Press Replay to watch again.";
    setStatus(data.mode === "drawing" ? `Drew ${data.description}` : `Wrote: ${data.answer}`, "robot");
  } catch (err) {
    if (ticker) { clearInterval(ticker); live.remove(); }
    if (job.input) job.input.remove();
    setStatus(err.message, "error");
    addThought("Something went wrong", err.message, "error");
  } finally {
    setBusy(false);
  }
}

// ---------- Image: upload a picture, the robot draws over it ----------
function loadImage(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(new Error("Couldn't read that file."));
    reader.onload = () => {
      const img = new Image();
      img.onload = () => resolve(img);
      img.onerror = () => reject(new Error("That file isn't an image I can open."));
      img.src = reader.result;
    };
    reader.readAsDataURL(file);
  });
}

// Visible area of the uploaded picture, in board pixels. Cover can extend past the edges.
function imageBox() {
  if (!background) return null;
  return [
    Math.max(0, background.x),
    Math.max(0, background.y),
    Math.min(W, background.x + background.w),
    Math.min(H, background.y + background.h),
  ];
}

async function placeImage(file) {
  if (busy) return;
  const button = document.querySelector('.action[data-action="answer"]');
  timedFrom = performance.now();
  setBusy(true);
  setStatus("Turning the image into a sketch…");
  try {
    const img = await loadImage(file);
    // Cover the board, then trace the ink into pen strokes — the same input a drawing uses.
    const scale = Math.max(W / img.width, H / img.height);
    const w = img.width * scale, h = img.height * scale;
    const off = document.createElement("canvas");
    off.width = W;
    off.height = H;
    const c = off.getContext("2d");
    c.fillStyle = "#ffffff";
    c.fillRect(0, 0, W, H);
    c.drawImage(img, (W - w) / 2, (H - h) / 2, w, h);
    const res = await fetch("/api/trace", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        image: off.toDataURL("image/png"), x: 0, y: 0, w: W, h: H, detail: "high", width: W, height: H,
      }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || `Server error ${res.status}`);
    // Drop a frame around the whole photo so it isn't treated as part of the problem.
    const sketched = (data.strokes || []).filter((pts) => {
      const xs = pts.map((p) => p[0]), ys = pts.map((p) => p[1]);
      return (Math.max(...xs) - Math.min(...xs)) < W * 0.9 || (Math.max(...ys) - Math.min(...ys)) < H * 0.9;
    });
    if (!sketched.length) throw new Error("Couldn't find any writing in that image.");
    saveHistory();
    background = { img, x: (W - w) / 2, y: (H - h) / 2, w, h };
    sketched.forEach((pts) => strokes.push({ owner: "user", points: pts }));
    render();
    setBusy(false);
    runAction("answer", button);
  } catch (err) {
    timedFrom = 0;
    setBusy(false);
    setStatus(err.message, "error");
  }
}

$("imageBtn").onclick = () => $("imageInput").click();
$("imageInput").addEventListener("change", (e) => {
  const file = e.target.files[0];
  e.target.value = ""; // lets you pick the same file again
  if (file) placeImage(file);
});

// ---------- robot simulator ----------
// Same greedy ordering as backend/gcode.py, so the simulation matches the real robot's path.
function orderStrokes(list) {
  const remaining = list.filter((s) => s.length >= 2).map((s) => s.slice());
  const ordered = [];
  let pos = HOME;
  while (remaining.length) {
    let best = 0, bestD = Infinity, flip = false;
    remaining.forEach((s, i) => {
      const dStart = Math.hypot(pos[0] - s[0][0], pos[1] - s[0][1]);
      const dEnd = Math.hypot(pos[0] - s[s.length - 1][0], pos[1] - s[s.length - 1][1]);
      if (dStart < bestD) { best = i; bestD = dStart; flip = false; }
      if (dEnd < bestD) { best = i; bestD = dEnd; flip = true; }
    });
    let s = remaining.splice(best, 1)[0];
    if (flip) s = s.reverse();
    ordered.push(s);
    pos = s[s.length - 1];
  }
  return ordered;
}

// A plan is the list of machine actions, like the G-code: travel, pen down, draw, pen up.
function buildPlan(list) {
  const plan = [];
  for (const s of orderStrokes(list)) {
    plan.push({ kind: "move", to: s[0], draw: false });
    plan.push({ kind: "pen", down: true });
    for (const p of s.slice(1)) plan.push({ kind: "move", to: p, draw: true });
    plan.push({ kind: "pen", down: false });
  }
  plan.push({ kind: "move", to: HOME, draw: false });
  return plan;
}

function speedMultiplier() {
  return Number($("speedSelect").value) || 4;
}

function toMM([x, y]) {
  return [(x * BOARD_MM.w) / W, ((H - y) * BOARD_MM.h) / H];
}

function drawRobot(c) {
  const [x, y] = robot.pos;
  const ink = COLORS.robot;
  c.save();

  // side rails
  c.fillStyle = "rgba(60, 72, 88, 0.22)";
  c.fillRect(0, 0, 8, H);
  c.fillRect(W - 8, 0, 8, H);

  // gantry beam spanning the board, sliding up and down on the rails
  c.fillStyle = "rgba(60, 72, 88, 0.16)";
  c.fillRect(0, y - 10, W, 20);
  c.strokeStyle = "rgba(60, 72, 88, 0.35)";
  c.lineWidth = 1;
  c.strokeRect(0.5, y - 10, W - 1, 20);

  // where the pen is headed next (pen-up travel)
  if (robot.target && !robot.penDown) {
    c.setLineDash([6, 8]);
    c.strokeStyle = "rgba(31, 87, 195, 0.35)";
    c.lineWidth = 2;
    c.beginPath();
    c.moveTo(x, y);
    c.lineTo(robot.target[0], robot.target[1]);
    c.stroke();
    c.setLineDash([]);
  }

  // live segment currently being drawn
  if (robot.penDown && robot.active) {
    const last = robot.active.points[robot.active.points.length - 1];
    c.lineCap = "round";
    c.beginPath();
    c.moveTo(last[0], last[1]);
    c.lineTo(x, y);
    if (background) {
      c.strokeStyle = "#ffffff";
      c.lineWidth = LINE_WIDTH + 7;
      c.stroke();
      c.beginPath();
      c.moveTo(last[0], last[1]);
      c.lineTo(x, y);
    }
    c.strokeStyle = ink;
    c.lineWidth = LINE_WIDTH;
    c.stroke();
  }

  // carriage riding on the beam
  c.fillStyle = "rgba(28, 34, 48, 0.12)";
  c.strokeStyle = "rgba(28, 34, 48, 0.55)";
  c.lineWidth = 1.5;
  c.beginPath();
  c.roundRect(x - 26, y - 20, 52, 40, 6);
  c.fill();
  c.stroke();

  // pen: solid when touching the board, hollow ring when lifted
  c.beginPath();
  if (robot.penDown) {
    c.fillStyle = ink;
    c.arc(x, y, 6, 0, Math.PI * 2);
    c.fill();
  } else {
    c.strokeStyle = ink;
    c.lineWidth = 2;
    c.arc(x, y, 9, 0, Math.PI * 2);
    c.stroke();
  }

  // readout like a machine controller
  const [mx, my] = toMM(robot.pos);
  const label = `${robot.penDown ? "PEN DOWN" : "PEN UP"}   X ${mx.toFixed(1)}  Y ${my.toFixed(1)} mm   ${robot.done}/${robot.total}`;
  c.font = '15px "Atkinson Hyperlegible", system-ui, sans-serif';
  const tw = c.measureText(label).width;
  c.fillStyle = "rgba(252, 253, 253, 0.9)";
  c.fillRect(W - tw - 36, 14, tw + 22, 28);
  c.fillStyle = robot.penDown ? ink : "#3b4652";
  c.fillText(label, W - tw - 25, 33);
  c.restore();
}

function animateRobot(newStrokes) {
  lastRobotObjs = [];
  const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  if (reduce) {
    newStrokes.forEach((pts) => {
      const obj = { owner: "robot", points: pts };
      strokes.push(obj);
      lastRobotObjs.push(obj);
    });
    render();
    return Promise.resolve();
  }

  const plan = buildPlan(newStrokes);
  robot = { pos: HOME.slice(), penDown: false, active: null, target: null,
            done: 0, total: newStrokes.filter((s) => s.length >= 2).length };
  let i = 0, penTimer = 0, lastTime = null;

  return new Promise((resolve) => {
    function frame(t) {
      if (lastTime === null) lastTime = t;
      let budget = Math.min((t - lastTime) / 1000, 0.1) * speedMultiplier(); // machine-seconds this frame
      lastTime = t;

      while (budget > 0 && i < plan.length) {
        const step = plan[i];
        if (step.kind === "pen") {
          penTimer += budget;
          if (penTimer < PEN_TIME) { budget = 0; break; }
          budget = penTimer - PEN_TIME;
          penTimer = 0;
          robot.penDown = step.down;
          if (step.down) {
            robot.active = { owner: "robot", points: [robot.pos.slice()] };
            strokes.push(robot.active);
            lastRobotObjs.push(robot.active);
          } else {
            robot.active = null;
            robot.done++;
          }
          i++;
          continue;
        }
        // move: travel or draw toward step.to at the right speed
        robot.target = step.to;
        const rate = step.draw ? DRAW_RATE : TRAVEL_RATE;
        const dx = step.to[0] - robot.pos[0], dy = step.to[1] - robot.pos[1];
        const dist = Math.hypot(dx, dy);
        const need = dist / rate;
        if (budget >= need) {
          robot.pos = step.to.slice();
          if (step.draw && robot.active) robot.active.points.push(step.to.slice());
          budget -= need;
          i++;
        } else {
          const f = (budget * rate) / dist;
          robot.pos = [robot.pos[0] + dx * f, robot.pos[1] + dy * f];
          budget = 0;
        }
      }

      render();
      if (i < plan.length) requestAnimationFrame(frame);
      else { robot = null; render(); resolve(); }
    }
    requestAnimationFrame(frame);
  });
}

async function replay() {
  if (busy || !lastRobotStrokes.length) return;
  strokes = strokes.filter((s) => !lastRobotObjs.includes(s));
  setBusy(true);
  setStatus("Replaying the robot…", "robot");
  await animateRobot(lastRobotStrokes);
  setStatus("Replay finished", "robot");
  setBusy(false);
}
$("replayBtn").onclick = replay;

// ---------- G-code for the robot ----------
$("gcodeBtn").onclick = async () => {
  if (!lastRobotStrokes.length) return;
  try {
    const res = await fetch("/api/gcode", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ strokes: lastRobotStrokes, width: W, height: H }),
    });
    if (!res.ok) throw new Error(`Server error ${res.status}`);
    const blob = new Blob([await res.text()], { type: "text/plain" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = "robot.gcode";
    a.click();
    URL.revokeObjectURL(a.href);
  } catch (err) {
    setStatus("Couldn't make G-code: " + err.message, "error");
  }
};

// ---------- the real robot ----------
let plotter = { connected: false, state: "disconnected" };  // the real robot (`robot` is the on-screen one)
let robotPoll = null;
let penDown = false;

async function robotApi(path, body) {
  const res = await fetch("/api/robot/" + path, body === undefined ? {} : {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || `Server error ${res.status}`);
  return data;
}

async function loadRobotPorts() {
  try {
    const { ports } = await robotApi("ports");
    const select = $("robotPort");
    const keep = select.value;
    select.innerHTML = "";
    for (const p of ports) {
      const opt = document.createElement("option");
      opt.value = p.device;
      opt.textContent = p.device === "sim" ? "Simulator" : `${p.device} (${p.description})`;
      select.appendChild(opt);
    }
    // Prefer a real port when one is plugged in
    const real = ports.find((p) => p.device !== "sim");
    select.value = ports.some((p) => p.device === keep) ? keep : real ? real.device : "sim";
  } catch (err) {
    setRobotStatus("Couldn't list ports: " + err.message, true);
  }
}

function setRobotStatus(text, error = false) {
  const el = $("robotStatus");
  el.textContent = text;
  el.className = "robot-status" + (error ? " is-error" : "");
}

function showRobot(s) {
  plotter = s;
  const drawing = s.state === "drawing" || s.state === "paused";
  $("robotConnect").textContent = s.connected ? "Disconnect" : "Connect";
  $("robotPort").disabled = s.connected;
  $("robotSend").disabled = !s.connected || drawing || !lastRobotStrokes.length;
  $("robotPause").disabled = !drawing;
  $("robotPause").textContent = s.state === "paused" ? "Resume" : "Pause";
  $("robotStop").disabled = !drawing;
  $("robotPen").disabled = !s.connected || drawing;
  $("robotPen").textContent = penDown ? "Pen up" : "Pen down";
  const bar = $("robotProgress");
  bar.hidden = !drawing;
  if (s.total) { bar.max = s.total; bar.value = s.sent; }
  let text = s.error || s.message || (s.connected ? "Connected" : "Not connected");
  if (drawing) text = `${s.state === "paused" ? "Paused" : "Drawing"}: line ${s.sent} of ${s.total}, pen at `
                    + `${s.pos[0].toFixed(1)}, ${s.pos[1].toFixed(1)} mm` + (s.elapsed ? `, ${Math.round(s.elapsed)} s` : "");
  setRobotStatus(text, !!s.error);
  // Poll quickly while drawing, slowly otherwise, not at all when disconnected
  clearTimeout(robotPoll);
  if (s.connected) robotPoll = setTimeout(refreshRobot, drawing ? 400 : 2000);
}

async function refreshRobot() {
  try { showRobot(await robotApi("status")); }
  catch (err) { setRobotStatus("Lost contact with the server: " + err.message, true); }
}

async function robotAction(path, body, busyText) {
  if (busyText) setRobotStatus(busyText);
  try { showRobot(await robotApi(path, body)); return true; }
  catch (err) { setRobotStatus(err.message, true); refreshRobot(); return false; }
}

$("robotConnect").onclick = () => plotter.connected
  ? robotAction("disconnect", {})
  : robotAction("connect", { port: $("robotPort").value }, "Connecting (the Arduino restarts, about 2 s)...");
$("robotSend").onclick = () => {
  if (!lastRobotStrokes.length) return;
  robotAction("draw", { strokes: lastRobotStrokes, width: W, height: H }, "Sending...");
};
$("robotPause").onclick = () => robotAction(plotter.state === "paused" ? "resume" : "pause", {});
$("robotStop").onclick = () => robotAction("stop", {}, "Stopping...");
$("robotPen").onclick = async () => {
  if (await robotAction("pen", { down: !penDown })) penDown = !penDown;
  showRobot(plotter);
};
// "Send to robot" depends on there being robot lines, which change after every answer
new MutationObserver(() => showRobot(plotter)).observe($("gcodeBtn"), { attributes: true, attributeFilter: ["disabled"] });

loadRobotPorts().then(refreshRobot);

window.addEventListener("resize", resize);
resize();