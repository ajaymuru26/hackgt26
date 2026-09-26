"""
Talk to the plotter: an Arduino Nano running GRBL, or a simulated GRBL for testing without one.

GRBL answers every line of G-code with "ok" (or "error:N"). We send a line, wait for
its answer, then send the next, so GRBL's small serial buffer never overflows. Its
status report ("<Idle,MPos:1.000,2.000,0.000,...>") is polled a few times a second
for the pen's position and whether it's still moving.
"""
import math
import re
import threading
import time
from collections import deque

BAUD = 115200
STATUS_EVERY = 0.25   # seconds between status polls
ACK_TIMEOUT = 180     # a full GRBL buffer can take a while to drain at low speeds
SIM = "sim"


class RobotError(Exception):
    pass


def clean(line: str) -> str:
    """Strip comments and spaces GRBL doesn't need: 'G21 ; millimetres' -> 'G21'."""
    line = re.sub(r"\(.*?\)", "", line.split(";", 1)[0])
    return line.strip()


def list_ports():
    ports = [{"device": SIM, "description": "Simulated robot (no hardware)"}]
    try:
        from serial.tools import list_ports as lp
        ports += [{"device": p.device, "description": p.description} for p in lp.comports()]
    except ImportError:
        pass
    return ports


# ---------- links: something to write bytes to and read lines from ----------

class SerialLink:
    def __init__(self, port):
        try:
            import serial
        except ImportError:
            raise RobotError("pyserial isn't installed. Run: pip install pyserial")
        try:
            self.port = serial.Serial(port, BAUD, timeout=0.1, write_timeout=2)
        except serial.SerialException as e:
            raise RobotError(f"Couldn't open {port}. Is the Arduino Serial Monitor (or another program) "
                             f"using it? ({e})")
        # The status poller and the sender both write. On Windows, two writes at once
        # cancel each other ("Write timeout"), so they take turns.
        self.write_lock = threading.Lock()
        time.sleep(2.0)  # opening the port resets the Nano; wait for GRBL to boot

    def write(self, data: bytes):
        with self.write_lock:
            self.port.write(data)

    def readline(self):
        raw = self.port.readline()
        return raw.decode(errors="replace").strip() if raw else None

    def close(self):
        self.port.close()


class SimLink:
    """Behaves like GRBL 0.9: a 16-move planner buffer, real move timing, '?' status,
    '!' hold, '~' resume, Ctrl-X reset. Nothing moves, but the timing is real."""

    PLANNER = 16

    def __init__(self, max_rate=3000.0):
        self.max_rate = max_rate  # mm/min, like GRBL's $110/$111
        self.pos = [0.0, 0.0]
        self.feed = max_rate
        self.planner = deque()    # (kind, target or seconds, feed)
        self.waiting = deque()    # "ok"s owed once the planner has room
        self.out = deque(["Grbl 0.9i ['$' for help]"])
        self.hold = False
        self.lock = threading.Lock()
        self.alive = True
        threading.Thread(target=self._run, daemon=True).start()

    def write(self, data: bytes):
        for ch in data.decode(errors="replace"):
            with self.lock:
                if ch == "?":
                    self.out.append(self._status())
                elif ch == "!":
                    self.hold = True
                elif ch == "~":
                    self.hold = False
                elif ch == "\x18":
                    self.planner.clear()
                    self.waiting.clear()
                    self.hold = False
                    self.out.append("Grbl 0.9i ['$' for help]")
        for line in data.decode(errors="replace").splitlines():
            line = clean(line.replace("?", "").replace("!", "").replace("~", "").replace("\x18", "")).upper()
            if line:
                self._accept(line)

    def _accept(self, line):
        with self.lock:
            if line.startswith("$"):
                self.out.append("ok")
                return
            words = dict((w[0], float(w[1:])) for w in re.findall(r"[A-Z][-+]?[\d.]+", line))
            if "G4" in line:
                self.planner.append(("wait", words.get("P", 0.0), 0))
            elif "M" in words:
                self.planner.append(("wait", 0.0, 0))  # like GRBL, pen (spindle) changes wait their turn
            elif "X" in words or "Y" in words:
                if "F" in words:
                    self.feed = words["F"]
                rapid = re.search(r"\bG0?0\b", line) is not None
                self.planner.append(("move", (words.get("X"), words.get("Y")),
                                     self.max_rate if rapid else min(self.feed, self.max_rate)))
            if len(self.planner) <= self.PLANNER:
                self.out.append("ok")
            else:
                self.waiting.append("ok")

    def _status(self):
        state = "Hold" if self.hold else ("Run" if self.planner else "Idle")
        x, y = self.pos
        return f"<{state},MPos:{x:.3f},{y:.3f},0.000,WPos:{x:.3f},{y:.3f},0.000>"

    def _run(self):
        tick = 0.02
        while self.alive:
            with self.lock:
                job = self.planner[0] if self.planner else None
            if job is None or self.hold:
                time.sleep(tick)
                continue
            kind, what, feed = job
            if kind == "wait":
                end = time.monotonic() + what
                while time.monotonic() < end and self.alive and self.planner and self.planner[0] is job:
                    time.sleep(tick)
            else:
                start = list(self.pos)
                target = [start[0] if what[0] is None else what[0], start[1] if what[1] is None else what[1]]
                seconds = math.dist(start, target) / max(feed / 60.0, 1e-6)
                done = 0.0
                while done < seconds and self.alive and self.planner and self.planner[0] is job:
                    if not self.hold:
                        done = min(seconds, done + tick)
                        f = done / seconds
                        self.pos = [start[0] + (target[0] - start[0]) * f, start[1] + (target[1] - start[1]) * f]
                    time.sleep(tick)
            with self.lock:
                if self.planner and self.planner[0] is job:
                    self.planner.popleft()
                    if self.waiting:
                        self.out.append(self.waiting.popleft())

    def readline(self):
        for _ in range(5):
            with self.lock:
                if self.out:
                    return self.out.popleft()
            time.sleep(0.02)
        return None

    def close(self):
        self.alive = False


# ---------- the robot ----------

class Robot:
    def __init__(self):
        self.link = None
        self.port = None
        self.state = "disconnected"   # disconnected | idle | drawing | paused | stopping | error
        self.grbl = ""                # GRBL's own state: Idle, Run, Hold, Alarm...
        self.pos = [0.0, 0.0]
        self.sent = self.total = 0
        self.message = ""
        self.error = ""
        self.started = None
        self.acks = deque()
        self.ack_ready = threading.Condition()
        self.cmd_lock = threading.Lock()   # one sender at a time
        self.stop_flag = threading.Event()
        self.threads_alive = False

    # ----- connection -----

    def connect(self, port):
        if self.link:
            self.disconnect()
        self.link = SimLink() if port == SIM else SerialLink(port)
        self.port = port
        self.error = ""
        self.threads_alive = True
        threading.Thread(target=self._reader, daemon=True).start()
        threading.Thread(target=self._poller, daemon=True).start()
        self.state = "idle"
        self.message = "Connected"
        try:
            self.command("$X")  # clear any alarm left from a previous session
        except RobotError:
            pass
        self.message = "Connected to the simulated robot" if port == SIM else f"Connected to GRBL on {port}"

    def disconnect(self):
        self.stop_flag.set()
        self.threads_alive = False
        if self.link:
            try:
                self.link.close()
            except Exception:
                pass
        self.link = None
        self.port = None
        self.state = "disconnected"
        self.grbl = ""
        self.error = ""
        self.message = "Disconnected"

    def _need_link(self):
        if not self.link:
            raise RobotError("The robot isn't connected.")

    # ----- background threads -----

    def _reader(self):
        link = self.link
        while self.threads_alive and self.link is link:
            try:
                line = link.readline()
            except Exception as e:
                if self.link is link and self.threads_alive:  # not a deliberate disconnect
                    self.state, self.error = "error", f"Lost the connection (was the USB cable unplugged?): {e}"
                    self.threads_alive = False
                return
            if not line:
                continue
            if line.startswith("<"):
                self._parse_status(line)
            elif line == "ok" or line.startswith("error"):
                with self.ack_ready:
                    self.acks.append(line)
                    self.ack_ready.notify_all()
            elif line.startswith("ALARM"):
                self.error = f"GRBL alarm: {line}"
            elif line.startswith("["):  # GRBL's messages and settings replies
                self.message = line.strip("[]")

    def _poller(self):
        link = self.link
        while self.threads_alive and self.link is link:
            try:
                link.write(b"?")
            except Exception:
                pass
            time.sleep(STATUS_EVERY)

    def _parse_status(self, line):
        body = line.strip("<>")
        self.grbl = re.split(r"[,|]", body, 1)[0]
        m = re.search(r"MPos:([-\d.]+),([-\d.]+)", body) or re.search(r"WPos:([-\d.]+),([-\d.]+)", body)
        if m:
            self.pos = [float(m.group(1)), float(m.group(2))]

    # ----- sending -----

    def _send(self, line, timeout=ACK_TIMEOUT):
        """Send one line and wait for GRBL's answer."""
        with self.ack_ready:
            self.acks.clear()
        self.link.write((line + "\n").encode())
        with self.ack_ready:
            if not self.ack_ready.wait_for(lambda: self.acks or self.stop_flag.is_set(), timeout):
                raise RobotError(f"GRBL didn't answer '{line}' within {timeout} s.")
            return self.acks.popleft() if self.acks else "stopped"

    def command(self, line):
        """One command while idle (jogging, pen tests, settings)."""
        self._need_link()
        if self.state in ("drawing", "paused", "stopping"):
            raise RobotError("The robot is busy drawing. Stop it first.")
        line = clean(line)
        if not line:
            raise RobotError("Type a G-code command first.")
        with self.cmd_lock:
            self.stop_flag.clear()
            reply = self._send(line, timeout=30)
        if reply.startswith("error"):
            raise RobotError(f"GRBL rejected '{line}': {reply}")
        return reply

    def draw(self, gcode_text):
        self._need_link()
        if self.state in ("drawing", "paused", "stopping"):
            raise RobotError("The robot is already drawing.")
        lines = [c for c in (clean(l) for l in gcode_text.splitlines()) if c]
        if not lines:
            raise RobotError("There's nothing to draw.")
        self.stop_flag.clear()
        self.sent, self.total = 0, len(lines)
        self.error = ""
        self.state = "drawing"
        self.started = time.monotonic()
        self.message = f"Drawing: {self.total} lines of G-code"
        threading.Thread(target=self._stream, args=(lines,), daemon=True).start()

    def _stream(self, lines):
        with self.cmd_lock:
            try:
                for line in lines:
                    if self.stop_flag.is_set():
                        return
                    reply = self._send(line)
                    if self.stop_flag.is_set():
                        return
                    if reply.startswith("error"):
                        raise RobotError(f"GRBL rejected line {self.sent + 1} ('{line}'): {reply}")
                    self.sent += 1
                # Everything is queued in GRBL; wait for the pen to actually finish.
                time.sleep(STATUS_EVERY * 2)
                while self.grbl not in ("Idle", "Alarm") and not self.stop_flag.is_set():
                    time.sleep(STATUS_EVERY)
                if not self.stop_flag.is_set():
                    self.state = "idle"
                    self.message = f"Finished in {time.monotonic() - self.started:.0f} s"
            except RobotError as e:
                self.state, self.error = "error", str(e)
            except Exception as e:
                self.state, self.error = "error", f"Lost the connection: {e}"

    # ----- realtime controls (work mid-drawing) -----

    def pause(self):
        self._need_link()
        if self.state == "drawing":
            self.link.write(b"!")
            self.state = "paused"
            self.message = "Paused"

    def resume(self):
        self._need_link()
        if self.state == "paused":
            self.link.write(b"~")
            self.state = "drawing"
            self.message = "Drawing"

    def stop(self, pen_up="M3 S90"):
        """Stop now: hold, reset (clears GRBL's queue), unlock, lift the pen."""
        self._need_link()
        self.state = "stopping"
        self.stop_flag.set()
        with self.ack_ready:
            self.ack_ready.notify_all()
        self.link.write(b"!")
        time.sleep(0.2)
        self.link.write(b"\x18")
        time.sleep(1.0 if self.port != SIM else 0.1)  # GRBL reboots after a reset
        with self.cmd_lock:
            self.stop_flag.clear()
            self.state = "idle"
            for line in ("$X", pen_up):
                try:
                    self._send(line, timeout=5)
                except RobotError:
                    pass
        self.error = ""  # the reset's "Abort during cycle" alarm is expected, and $X just cleared it
        self.message = "Stopped. The pen is up; the position may be off until it's homed."

    def status(self):
        return {
            "connected": self.link is not None,
            "port": self.port,
            "state": self.state,
            "grbl": self.grbl,
            "pos": [round(v, 2) for v in self.pos],
            "sent": self.sent,
            "total": self.total,
            "elapsed": round(time.monotonic() - self.started, 1) if self.started and self.state in ("drawing", "paused") else None,
            "message": self.message,
            "error": self.error,
        }


robot = Robot()
