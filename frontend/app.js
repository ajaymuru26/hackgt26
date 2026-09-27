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
let lastSpeech = "";
let robot = null;       // live simulator state while the robot is drawing
let lastRobotObjs = [];  // stroke objects the robot added last time (for replay)
const provider = "openai";

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
  const ink = s.highlight ? "#159447" : COLORS[s.owner];
  c.strokeStyle = ink;
  c.fillStyle = ink;
  c.lineWidth = LINE_WIDTH;
  if (traceStroke(c, pts) === "fill") c.fill();
  else c.stroke();
}

function render() {
  ctx.clearRect(0, 0, W, H);
  strokes.forEach((s) => drawStroke(ctx, s));
  if (current) drawStroke(ctx, current);
  if (robot) drawRobot(ctx);
  drawMachine();
  $("empty").hidden = strokes.length > 0 || !!current;
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
  canvas.classList.toggle("erasing", t === "eraser");
}

function undo() {
  if (busy || !history.length) return;
  strokes = JSON.parse(history.pop());
  render();
}

$("penBtn").onclick = () => setTool("pen");
$("eraserBtn").onclick = () => setTool("eraser");
$("undoBtn").onclick = undo;
$("clearBtn").onclick = () => {
  if (busy || !strokes.length) return;
  saveHistory();
  strokes = [];
  lastRobotStrokes = [];
  lastRobotObjs = [];
  lastSpeech = "";
  showTranscript("");
  voice.pause();
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
  ["undoBtn", "clearBtn"].forEach((id) => ($(id).disabled = on));
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
  strokes.forEach((s) => drawStroke(c, { ...s, owner: "user", highlight: false }));
  return off.toDataURL("image/png");
}

// ---------- thought process panel ----------
function clearThoughts() {
  $("thoughtList").innerHTML = "";
  $("thoughtsEmpty").hidden = true;
  $("problemType").hidden = true;
  showTranscript("");
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

const ACTION_WORDS = { answer: "Working out the answer", work: "Working out the steps",
                       hint: "Thinking of a hint",
                       check: "Checking your work", drawing: "Looking at your drawing",
                       submit: "Checking your work",
                       recommend: "Choosing the action that fits",
                       cv: "Reading the ink with computer vision",
                       auto: "Reading the picture" };

async function runAction(action, button) {
  if (busy) return;
  if (!strokes.length) { setStatus("The board is empty. Write or draw something first.", "error"); return; }
  armSpeaker();

  setBusy(true);
  button.classList.add("is-busy");
  setStatus(`${ACTION_WORDS[action] || "Looking at the board"}…`);
  clearThoughts();
  const thinking = action === "cv" ? "Reading the ink with computer vision" : "Sending the board to the AI";
  const live = addThought("Thinking", `${thinking}… 0.0 s`, "live");
  const t0 = performance.now();
  const ticker = setInterval(() => {
    live.querySelector(".thought-detail").textContent =
      `${thinking}… ${((performance.now() - t0) / 1000).toFixed(1)} s`;
  }, 100);
  try {
    const image = snapshot();
    const res = await fetch("/api/complete", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ image, width: W, height: H, action, review: $("reviewToggle").checked,
                             strokes: strokes.map((s) => s.points), provider }),
    });
    const data = await res.json().catch(() => ({}));
    clearInterval(ticker);
    live.remove();
    if (!res.ok) throw new Error(data.detail || `Server error ${res.status}`);
    // Clock stops when the answer is about to be written, not after the pen finishes.
    const runtime = ((performance.now() - t0) / 1000).toFixed(1);
    showProblemType(data.category);

    const recommended = data.recommended ? `Recommended: ${data.recommended}. ` : "";
    const label = {
      math: `Read ${data.expression} and wrote ${data.answer}`,
      fill: `${data.description || "Next up"}: wrote ${data.answer}`,
      answer: `Answered: ${data.answer}`,
      hint: data.answer,
      check: `Checked your work: ${data.answer}`,
    }[data.mode] || `Finishing ${data.description || "the drawing"}`;
    const checkNote = data.checks ? ` · ${data.checks} check${data.checks === 1 ? "" : "s"}` : "";
    saveHistory();
    (data.highlight || []).forEach((i) => {
      if (strokes[i] && strokes[i].owner === "user") strokes[i].highlight = true;
    });
    await showThoughts(data.steps);
    setStatus(`${recommended}${label}${checkNote} · ${runtime} s`, "robot");
    addThought("Runtime", `${runtime} s until writing`);
    noteSaved(data);
    showTranscript(data.transcript);
    playSpeech(data.speech);
    render();
    lastRobotStrokes = data.strokes;
    const drawing = addThought("Drawing", "The robot is drawing it on the board now.", "live");
    await animateRobot(data.strokes);
    drawing.classList.remove("is-live");
    drawing.querySelector(".thought-title").textContent = "Done";
    drawing.querySelector(".thought-detail").textContent = "Finished. Press Replay to watch again.";
    const finished = data.mode !== "drawing" ? label : `Finished ${data.description || "the drawing"}`;
    setStatus(`${recommended}${finished}${checkNote} · ${runtime} s`, "robot");
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

// ---------- robot simulator ----------
// Strokes are drawn in the order the backend sent them, like backend/gcode.py: writing goes
// line by line from the top (each line from left to right, the answer last); drawings come
// already sorted nearest-first.
function orderStrokes(list) {
  return list.filter((s) => s.length >= 2);
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
  return 4;
}

const MARKER_MM = 8;

function axisSpeeds() {
  const preview = speedMultiplier();
  return {
    x: (Number($("speedX").value) || 120) * preview,
    y: (Number($("speedY").value) || 80) * preview,
    z: (Number($("speedZ").value) || 40) * preview,
  };
}

function moveSeconds(from, to) {
  const [x1, y1] = toMM(from);
  const [x2, y2] = toMM(to);
  const speeds = axisSpeeds();
  return Math.max(Math.abs(x2 - x1) / speeds.x, Math.abs(y2 - y1) / speeds.y);
}

let machineTrail = [];

function logMachine(text) {
  const list = $("machineLog");
  if (!list) return;
  const li = document.createElement("li");
  li.textContent = text;
  list.append(li);
  while (list.children.length > 6) list.removeChild(list.firstChild);
}

function drawMachine() {
  const canvas = $("machine");
  if (!canvas) return;
  const c = canvas.getContext("2d");
  const dpr = window.devicePixelRatio || 1;
  const w = canvas.clientWidth || 300;
  const h = 210;
  if (canvas.width !== Math.round(w * dpr) || canvas.height !== Math.round(h * dpr)) {
    canvas.width = Math.round(w * dpr);
    canvas.height = Math.round(h * dpr);
  }
  c.setTransform(dpr, 0, 0, dpr, 0, 0);
  c.clearRect(0, 0, w, h);
  c.fillStyle = "#12171f";
  c.fillRect(0, 0, w, h);

  const [xMm, yMm] = robot ? toMM(robot.pos) : [0, 0];
  const zMm = robot ? (robot.z ?? (robot.penDown ? 0 : MARKER_MM)) : MARKER_MM;
  const down = robot ? robot.penDown && zMm < 0.4 : false;
  $("readX").firstChild.textContent = `${xMm.toFixed(1)} `;
  $("readY").firstChild.textContent = `${yMm.toFixed(1)} `;
  const zRead = $("readZ");
  zRead.classList.toggle("is-down", down);
  zRead.firstChild.textContent = `${zMm.toFixed(1)} `;
  zRead.querySelector("span").textContent = down ? "mm down" : "mm up";

  const padL = 26, padR = 14, padT = 28, padB = 22;
  const boardTop = padT + 24;
  const boardH = h - boardTop - padB;
  const boardW = w - padL - padR;
  const px = (x, y) => [padL + (x / BOARD_MM.w) * boardW, boardTop + (1 - y / BOARD_MM.h) * boardH];
  const [cx, cy] = px(xMm, yMm);
  const lift = (zMm / MARKER_MM) * 26;

  c.fillStyle = "#f4f7f8";
  c.fillRect(padL, boardTop, boardW, boardH);
  if (machineTrail.length > 1) {
    c.strokeStyle = "#1f57c3";
    c.lineWidth = 2;
    c.lineCap = "round";
    c.lineJoin = "round";
    c.beginPath();
    machineTrail.forEach((p, i) => {
      const [tx, ty] = px(p[0], p[1]);
      if (i === 0) c.moveTo(tx, ty);
      else c.lineTo(tx, ty);
    });
    c.stroke();
  }

  c.strokeStyle = "#8b97a6";
  c.lineWidth = 6;
  c.beginPath();
  c.moveTo(padL, boardTop - 8);
  c.lineTo(padL, boardTop + boardH + 8);
  c.moveTo(padL + boardW, boardTop - 8);
  c.lineTo(padL + boardW, boardTop + boardH + 8);
  c.stroke();

  c.strokeStyle = "#d5dde6";
  c.lineWidth = 8;
  c.lineCap = "butt";
  c.beginPath();
  c.moveTo(padL - 4, cy);
  c.lineTo(padL + boardW + 4, cy);
  c.stroke();

  c.fillStyle = "#2a3342";
  c.strokeStyle = "#d6ff6b";
  c.lineWidth = 2;
  c.beginPath();
  c.roundRect(cx - 16, cy - 14, 32, 28, 4);
  c.fill();
  c.stroke();

  c.strokeStyle = down ? "#8eb6ff" : "#d6ff6b";
  c.lineWidth = 3;
  c.beginPath();
  c.moveTo(cx, cy + 14 - lift);
  c.lineTo(cx, cy + 14);
  c.stroke();
  c.fillStyle = down ? "#8eb6ff" : "#d6ff6b";
  c.beginPath();
  c.arc(cx, cy + 14, down ? 4 : 3, 0, Math.PI * 2);
  c.fill();
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

function animateRobot(newStrokes, opts = {}) {
  const owner = opts.owner || "robot";
  const record = opts.record !== false;
  const ink = opts.ink !== false;
  if (record) lastRobotObjs = [];
  const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  if (reduce) {
    if (ink) {
      newStrokes.forEach((pts) => {
        const obj = { owner, points: pts };
        strokes.push(obj);
        if (record) lastRobotObjs.push(obj);
      });
    }
    render();
    return Promise.resolve();
  }

  const plan = buildPlan(newStrokes);
  machineTrail = [];
  $("machineLog").replaceChildren();
  robot = { pos: HOME.slice(), penDown: false, z: MARKER_MM, active: null, target: null,
            done: 0, total: newStrokes.filter((s) => s.length >= 2).length };
  let i = 0, penTimer = 0, lastTime = null, logged = -1;

  return new Promise((resolve) => {
    function frame(t) {
      if (lastTime === null) lastTime = t;
      let budget = Math.min((t - lastTime) / 1000, 0.05);
      lastTime = t;

      while (budget > 0 && i < plan.length) {
        const step = plan[i];
        if (i !== logged) {
          logged = i;
          if (step.kind === "pen") logMachine(step.down ? "Marker down" : "Marker up");
          else {
            const [mx, my] = toMM(step.to);
            logMachine(`${step.draw ? "Draw" : "Move"}  X ${mx.toFixed(1)}  Y ${my.toFixed(1)}`);
          }
        }
        if (step.kind === "pen") {
          const need = MARKER_MM / axisSpeeds().z;
          penTimer += budget;
          const along = Math.min(penTimer / need, 1);
          robot.z = step.down ? MARKER_MM * (1 - along) : MARKER_MM * along;
          if (penTimer < need) { budget = 0; break; }
          budget = penTimer - need;
          penTimer = 0;
          robot.penDown = step.down;
          robot.z = step.down ? 0 : MARKER_MM;
          if (step.down) {
            robot.active = { owner, points: [robot.pos.slice()] };
            if (ink) {
              strokes.push(robot.active);
              if (record) lastRobotObjs.push(robot.active);
            }
            const [tx, ty] = toMM(robot.pos);
            machineTrail.push([tx, ty]);
          } else {
            robot.active = null;
            robot.done++;
          }
          i++;
          continue;
        }
        robot.target = step.to;
        const dx = step.to[0] - robot.pos[0], dy = step.to[1] - robot.pos[1];
        const dist = Math.hypot(dx, dy);
        const need = dist < 0.01 ? 0 : moveSeconds(robot.pos, step.to);
        if (need === 0 || budget >= need) {
          robot.pos = step.to.slice();
          if (step.draw && robot.active) {
            if (ink) robot.active.points.push(step.to.slice());
            const [tx, ty] = toMM(robot.pos);
            machineTrail.push([tx, ty]);
          }
          budget -= need;
          i++;
        } else {
          const f = budget / need;
          robot.pos = [robot.pos[0] + dx * f, robot.pos[1] + dy * f];
          if (step.draw) {
            const [tx, ty] = toMM(robot.pos);
            machineTrail.push([tx, ty]);
          }
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
  showTranscript(lastTranscript);
  playSpeech(lastSpeech);
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

const SILENCE = "data:audio/wav;base64,UklGRiQAAABXQVZFZm10IBAAAAABAAEAQB8AAEAfAAABAAgAZGF0YQAAAAA=";
const voice = new Audio(SILENCE);
let lastTranscript = "";

function armSpeaker() {
  voice.src = SILENCE;
  const play = voice.play();
  if (play) play.then(() => voice.pause()).catch(() => {});
}

function showTranscript(text) {
  const box = $("transcript");
  const body = $("transcriptText");
  lastTranscript = text || "";
  if (!text) {
    box.hidden = true;
    body.replaceChildren();
    return;
  }
  body.replaceChildren();
  text.split(/(\s+)/).forEach((part) => {
    if (!part.trim()) {
      body.append(part);
      return;
    }
    const word = document.createElement("span");
    word.className = "transcript-word";
    word.textContent = part;
    body.append(word);
  });
  box.hidden = false;
  box.scrollIntoView({ block: "nearest" });
}

function markTranscript(fraction) {
  const words = $("transcriptText").querySelectorAll(".transcript-word");
  if (!words.length) return;
  const said = fraction <= 0 ? 0 : fraction >= 1 ? words.length : Math.ceil(fraction * words.length);
  words.forEach((word, i) => {
    word.classList.toggle("is-pending", i >= said);
    word.classList.toggle("is-said", i < said);
  });
}

function playSpeech(url) {
  lastSpeech = url || "";
  if (!url) {
    voice.pause();
    if (lastTranscript) markTranscript(1);
    return;
  }
  voice.src = url;
  markTranscript(0);
  const play = voice.play();
  if (play) play.catch(() => markTranscript(1));
}

voice.addEventListener("timeupdate", () => {
  if (!voice.duration) return;
  markTranscript(voice.currentTime / voice.duration);
});
voice.addEventListener("ended", () => markTranscript(1));

function noteSaved(data) {
  if (!data.board_id) return;
  addThought("Saved", "Stored this board in MongoDB Atlas.");
  loadSaved();
}

function historyTitle(board) {
  const names = { math: "Math", answer: "Question", hint: "Hint", check: "Check", fill: "Pattern",
                  drawing: "Drawing", speak: "Voice", ask: "Ask", write: "Writing", trace: "Image" };
  const kind = names[board.mode] || names[board.source] || "Board";
  const expr = (board.expression || "").trim();
  const answer = (board.answer || "").trim();
  const heard = (board.heard || "").trim();
  const desc = (board.description || "").trim();
  let body = board.label || "Saved board";
  if (heard) body = heard;
  else if (expr && answer && !expr.includes(answer)) body = `${expr} → ${answer}`;
  else if (expr) body = expr;
  else if (answer && desc && desc !== answer) body = `${desc} → ${answer}`;
  else if (answer) body = answer;
  else if (desc) body = desc;
  else if (body.includes(": ")) body = body.slice(body.indexOf(": ") + 2);
  return { kind, body };
}

async function loadSaved() {
  const list = $("savedList");
  const empty = $("savedEmpty");
  try {
    const res = await fetch("/api/boards");
    const data = await res.json().catch(() => ({}));
    const boards = data.boards || [];
    list.replaceChildren();
    if (!data.configured) {
      empty.hidden = false;
      empty.textContent = "Add MONGODB_URI to backend/.env to keep finished boards in Atlas.";
      return;
    }
    empty.hidden = boards.length > 0;
    empty.textContent = "Finished boards show up here.";
    boards.forEach((board) => {
      const item = document.createElement("li");
      const btn = document.createElement("button");
      btn.type = "button";
      const { kind, body } = historyTitle(board);
      const when = board.created
        ? new Date(board.created).toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" })
        : "";
      const kindEl = document.createElement("span");
      kindEl.className = "history-kind";
      kindEl.textContent = kind;
      const titleEl = document.createElement("span");
      titleEl.className = "history-title";
      titleEl.textContent = body;
      const whenEl = document.createElement("span");
      whenEl.className = "history-when";
      whenEl.textContent = [when, board.seconds ? `${board.seconds} s` : ""].filter(Boolean).join(" · ");
      btn.append(kindEl, titleEl, whenEl);
      btn.onclick = () => openSaved(board.id);
      item.append(btn);
      list.append(item);
    });
  } catch {
    empty.hidden = false;
    empty.textContent = "Couldn't load saved boards.";
  }
}

async function openSaved(id) {
  if (busy) return;
  setBusy(true);
  setStatus("Opening a saved board…");
  try {
    const res = await fetch(`/api/boards/${id}`);
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || `Server error ${res.status}`);
    history = [];
    strokes = (data.strokes || []).map((s) => ({ owner: s.owner === "robot" ? "robot" : "user", points: s.points }));
    lastRobotObjs = strokes.filter((s) => s.owner === "robot");
    lastRobotStrokes = lastRobotObjs.map((s) => s.points);
    render();
    setStatus(data.label ? `Opened ${data.label}` : "Opened a saved board", "robot");
  } catch (err) {
    setStatus(err.message, "error");
  } finally {
    setBusy(false);
  }
}

function fromMM(x, y) {
  return [(x * W) / BOARD_MM.w, H - (y * H) / BOARD_MM.h];
}

["speedX", "speedY", "speedZ"].forEach((id) => {
  const input = $(id);
  const out = $(id + "Out");
  const show = () => { out.textContent = `${input.value} mm/s`; };
  input.addEventListener("input", show);
  show();
});

$("machineTest").onclick = async () => {
  if (busy) return;
  setBusy(true);
  setStatus("Testing the gantry…", "robot");
  const a = fromMM(120, 70), b = fromMM(460, 70), c = fromMM(460, 240), d = fromMM(120, 240);
  await animateRobot([[a, b, c, d, a]], { ink: false, record: false });
  setStatus("Test move finished. X, Y, and the marker are back home.", "robot");
  setBusy(false);
};

window.addEventListener("resize", resize);
resize();
loadSaved();
fetch("/api/host").then((res) => res.json()).then((data) => {
  if (!data.public_url) return;
  const hint = document.querySelector(".top .hint");
  if (hint) hint.append(document.createTextNode("  Live at " + data.public_url));
}).catch(() => {});