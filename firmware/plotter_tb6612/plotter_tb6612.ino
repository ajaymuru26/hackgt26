/*
  Pen plotter firmware: Arduino Nano + two NEMA 17 steppers on TB6612FNG dual
  H-bridges (one board per motor) + a pen-lift servo.

  GRBL can't be used here: it outputs step/direction pulses for stepper drivers,
  and a TB6612FNG needs the Nano to switch each coil itself. This firmware does
  that, and speaks enough of GRBL's serial protocol that backend/robot.py (the
  app's "Send to robot") and G-code senders work unchanged:

    - every line gets "ok" or "error: ..." back
    - '?' reports "<Idle|Run|Hold,MPos:x,y,0.000,WPos:x,y,0.000>"
    - '!' pauses, '~' resumes, Ctrl-X stops and clears the queue
    - G0/G1 X Y F, G4 P<seconds>, G90/G91, G21, G92, M3 S<0-90> (pen), M5
    - $$ lists settings, $<n>=<value> changes one (saved in EEPROM), $X unlocks

  Wiring (see firmware/README.md):
    X driver: AIN1 D2, AIN2 D3, BIN1 D4, BIN2 D7, PWMA+PWMB D5
    Y driver: AIN1 A0, AIN2 A1, BIN1 A2, BIN2 A3, PWMA+PWMB D6
    STBY of both drivers: D8     Pen servo signal: D11
*/
#include <EEPROM.h>
#include <Servo.h>

// ---------- pins ----------
const uint8_t COIL_PINS[2][4] = {{2, 3, 4, 7},        // X: AIN1, AIN2, BIN1, BIN2
                                 {A0, A1, A2, A3}};   // Y
const uint8_t PWM_PINS[2] = {5, 6};  // PWMA and PWMB of each driver, tied together
const uint8_t STBY_PIN = 8;          // both drivers' STBY: LOW = coils off
const uint8_t SERVO_PIN = 11;

const char BANNER[] = "Grbl 0.9i ['$' for help] (TB6612 plotter)";

// Half-step sequence: current direction in coil A and coil B (+1, 0 = off, -1).
// Full steps use only the odd entries (both coils on: more torque, coarser steps).
const int8_t PHASES[8][2] = {{1, 0}, {1, 1}, {0, 1}, {-1, 1}, {-1, 0}, {-1, -1}, {0, -1}, {1, -1}};

// ---------- settings ($$) ----------
struct Settings {
  uint8_t version;
  float stepsPerMm[2];  // $100 $101
  float maxRate[2];     // $110 $111  mm/min
  float accel[2];       // $120 $121  mm/s^2
  uint8_t idleMs;       // $1   ms before the coils switch off when idle; 255 = always on
  uint8_t invert;       // $3   direction invert mask: bit 0 = X, bit 1 = Y
  uint8_t power;        // $140 motor power %, as PWM duty (the TB6612 has no current limit)
  uint8_t fullStep;     // $150 1 = full steps, 0 = half steps
};
const uint8_t SETTINGS_VERSION = 1;
Settings cfg;

void defaults() {
  cfg.version = SETTINGS_VERSION;
  cfg.stepsPerMm[0] = cfg.stepsPerMm[1] = 10.0;  // calibrate: see README
  cfg.maxRate[0] = cfg.maxRate[1] = 1500.0;
  cfg.accel[0] = cfg.accel[1] = 150.0;
  cfg.idleMs = 250;
  cfg.invert = 0;
  cfg.power = 60;
  cfg.fullStep = 0;
}

// ---------- queue of moves, pauses and pen changes ----------
enum Kind : uint8_t { MOVE, DWELL, PEN };
struct Block {
  Kind kind;
  long target[2];  // steps (MOVE)
  float feed;      // mm/min (MOVE)
  float value;     // seconds (DWELL) or servo angle (PEN)
};
const uint8_t QUEUE = 8;
Block queue[QUEUE];
uint8_t qHead = 0, qCount = 0;

// ---------- machine state ----------
long pos[2] = {0, 0};      // where the motors are, in steps
long planned[2] = {0, 0};  // where the last queued move ends
uint8_t phase[2] = {1, 1};
bool absolute = true, rapid = false, hold = false, coilsOn = false;
float feed = 1000.0;
unsigned long idleSince = 0;
Servo pen;

// The block being run
bool active = false;
Block cur;
long total, done, delta[2], err[2];
int8_t dir[2];
float v0, vmax, a2;  // start speed, top speed (steps/s) and 2 * acceleration (steps/s^2)
unsigned long lastStep, interval, dwellStart;

// ---------- serial input ----------
char line[96];
uint8_t lineLen = 0;
bool lineReady = false, lineTooLong = false;

// ---------- motors ----------
void energize(uint8_t axis) {
  const uint8_t* p = COIL_PINS[axis];
  int8_t a = PHASES[phase[axis]][0], b = PHASES[phase[axis]][1];
  digitalWrite(p[0], a > 0); digitalWrite(p[1], a < 0);
  digitalWrite(p[2], b > 0); digitalWrite(p[3], b < 0);
}

void motorsOn(bool on) {
  if (on == coilsOn) return;
  if (on) {
    energize(0);
    energize(1);
  }
  digitalWrite(STBY_PIN, on);
  coilsOn = on;
  if (on) delay(5);  // let the coils settle before the first step
}

void applyPower() {
  uint8_t duty = (uint16_t)constrain(cfg.power, 0, 100) * 255 / 100;
  analogWrite(PWM_PINS[0], duty);
  analogWrite(PWM_PINS[1], duty);
}

void stepAxis(uint8_t axis, int8_t d) {
  uint8_t inc = cfg.fullStep ? 2 : 1;
  if (cfg.fullStep && !(phase[axis] & 1)) phase[axis] = (phase[axis] + 1) & 7;  // onto a two-coil step
  if (cfg.invert & (1 << axis)) d = -d;
  phase[axis] = (phase[axis] + (d > 0 ? inc : 8 - inc)) & 7;
  energize(axis);
}

// ---------- running blocks ----------
bool busy() { return active || qCount > 0; }

void startBlock() {
  cur = queue[qHead];
  qHead = (qHead + 1) % QUEUE;
  qCount--;
  if (cur.kind == PEN) {
    pen.write((int)constrain(cur.value, 0, 180));
    return;  // G-code follows pen changes with a G4 pause for the servo
  }
  if (cur.kind == DWELL) {
    active = true;
    dwellStart = millis();
    return;
  }
  total = 0;
  float mm[2], len = 0;
  for (uint8_t i = 0; i < 2; i++) {
    long d = cur.target[i] - pos[i];
    dir[i] = d < 0 ? -1 : 1;
    delta[i] = labs(d);
    if (delta[i] > total) total = delta[i];
    mm[i] = delta[i] / cfg.stepsPerMm[i];
    len += mm[i] * mm[i];
  }
  if (total == 0) return;
  len = sqrt(len);
  // Slowest of: the requested feed, and each axis's own speed and acceleration limits
  float rate = cur.feed, acc = 1e9;
  for (uint8_t i = 0; i < 2; i++) {
    if (mm[i] > 0) {
      rate = min(rate, cfg.maxRate[i] * len / mm[i]);
      acc = min(acc, cfg.accel[i] * len / mm[i]);
    }
  }
  float stepsPerMm = total / len;  // along the path, counted on the axis that moves most
  vmax = min(rate / 60.0 * stepsPerMm, 2500.0);
  v0 = min(vmax, 5.0 * stepsPerMm);  // start and end at 5 mm/s
  a2 = 2.0 * acc * stepsPerMm;
  err[0] = err[1] = total / 2;
  done = 0;
  motorsOn(true);
  active = true;
  interval = 0;
  lastStep = micros();
}

void runBlock() {
  if (cur.kind == DWELL) {
    if (millis() - dwellStart >= (unsigned long)(cur.value * 1000.0)) active = false;
    return;
  }
  if (hold || micros() - lastStep < interval) return;
  lastStep = micros();
  for (uint8_t i = 0; i < 2; i++) {  // Bresenham: both axes arrive together
    err[i] -= delta[i];
    if (err[i] < 0) {
      err[i] += total;
      stepAxis(i, dir[i]);
      pos[i] += dir[i];
    }
  }
  done++;
  if (done >= total) {
    active = false;
    return;
  }
  // Trapezoid: speed up from v0, cruise at vmax, slow down to v0 at the end
  float v = min(vmax, min(sqrt(v0 * v0 + a2 * done), sqrt(v0 * v0 + a2 * (total - done))));
  interval = (unsigned long)(1e6 / max(v, 1.0));
}

// ---------- reports ----------
void reportStatus() {
  Serial.print('<');
  Serial.print(hold && busy() ? "Hold" : busy() ? "Run" : "Idle");
  for (uint8_t k = 0; k < 2; k++) {
    Serial.print(k == 0 ? ",MPos:" : ",WPos:");
    Serial.print(pos[0] / cfg.stepsPerMm[0], 3);
    Serial.print(',');
    Serial.print(pos[1] / cfg.stepsPerMm[1], 3);
    Serial.print(",0.000");
  }
  Serial.println('>');
}

void printSetting(const char* id, float v, uint8_t digits, const char* what) {
  Serial.print('$'); Serial.print(id); Serial.print('=');
  Serial.print(v, digits);
  Serial.print(" ("); Serial.print(what); Serial.println(')');
}

void listSettings() {
  printSetting("1", cfg.idleMs, 0, "coils off after idle, msec, 255=always on");
  printSetting("3", cfg.invert, 0, "direction invert mask: 1=X 2=Y 3=both");
  printSetting("100", cfg.stepsPerMm[0], 3, "x, step/mm");
  printSetting("101", cfg.stepsPerMm[1], 3, "y, step/mm");
  printSetting("110", cfg.maxRate[0], 3, "x max rate, mm/min");
  printSetting("111", cfg.maxRate[1], 3, "y max rate, mm/min");
  printSetting("120", cfg.accel[0], 3, "x accel, mm/sec^2");
  printSetting("121", cfg.accel[1], 3, "y accel, mm/sec^2");
  printSetting("140", cfg.power, 0, "motor power, percent PWM");
  printSetting("150", cfg.fullStep, 0, "1=full steps, 0=half steps");
}

// ---------- commands ----------
// (kind is a plain byte: the Arduino IDE declares functions above the Kind enum)
void enqueue(uint8_t kind, long x, long y, float f, float value) {
  Block& b = queue[(qHead + qCount) % QUEUE];
  b.kind = (Kind)kind;
  b.target[0] = x;
  b.target[1] = y;
  b.feed = f;
  b.value = value;
  qCount++;
}

const char* dollar(char* s) {
  if (!strcmp(s, "$")) { Serial.println("[$$ settings, $X unlock, $<n>=<value> set, $RST=* defaults]"); return 0; }
  if (!strcmp(s, "$$")) { listSettings(); return 0; }
  if (!strcmp(s, "$X")) { Serial.println("[Caution: Unlocked]"); return 0; }
  if (!strcmp(s, "$RST=*") || !strcmp(s, "$RST=$")) { defaults(); EEPROM.put(0, cfg); applyPower(); return 0; }
  char* eq = strchr(s, '=');
  if (!eq) return "Unsupported command";
  *eq = 0;
  int id = atoi(s + 1);
  float v = atof(eq + 1);
  switch (id) {
    case 1: cfg.idleMs = constrain(v, 0, 255); break;
    case 3: cfg.invert = constrain(v, 0, 3); break;
    case 100: case 101: if (v <= 0) return "Value must be > 0"; cfg.stepsPerMm[id - 100] = v; break;
    case 110: case 111: if (v <= 0) return "Value must be > 0"; cfg.maxRate[id - 110] = v; break;
    case 120: case 121: if (v <= 0) return "Value must be > 0"; cfg.accel[id - 120] = v; break;
    case 140: cfg.power = constrain(v, 0, 100); applyPower(); break;
    case 150: cfg.fullStep = v != 0; break;
    default: return "Invalid statement";
  }
  // Changing steps/mm keeps the pen where it is, in millimetres
  EEPROM.put(0, cfg);
  return 0;
}

// Returns an error message, or 0 for ok.
const char* gcode(char* s) {
  bool has[26] = {false};
  float val[26];
  int gs[4], ng = 0, m = -1;
  while (*s) {
    char letter = *s++;
    if (letter < 'A' || letter > 'Z') return "Expected command letter";
    char* end;
    float v = strtod(s, &end);
    if (end == s) return "Bad number format";
    s = end;
    if (letter == 'G') {
      if (ng == 4) return "Too many G-codes";
      gs[ng++] = (int)(v * 10 + 0.5);  // G92 -> 920, G4 -> 40
    } else if (letter == 'M') {
      m = (int)v;
    } else {
      has[letter - 'A'] = true;
      val[letter - 'A'] = v;
    }
  }
  bool dwell = false, setPos = false;
  for (int i = 0; i < ng; i++) {
    switch (gs[i]) {
      case 0: rapid = true; break;
      case 10: rapid = false; break;
      case 40: dwell = true; break;
      case 210: break;  // millimetres (the only units)
      case 200: return "Inches aren't supported, use G21";
      case 900: absolute = true; break;
      case 910: absolute = false; break;
      case 920: setPos = true; break;
      default: return "Unsupported command";
    }
  }
  if (has['F' - 'A']) {
    if (val['F' - 'A'] <= 0) return "Feed rate must be > 0";
    feed = val['F' - 'A'];
  }
  if (setPos) {  // G92: "the pen is here now", e.g. G92 X0 Y0 after moving it to the corner by hand
    for (uint8_t i = 0; i < 2; i++) {
      if (has['X' - 'A' + i]) pos[i] = planned[i] = lround(val['X' - 'A' + i] * cfg.stepsPerMm[i]);
    }
    return 0;
  }
  if (m == 3) {
    float amount = has['S' - 'A'] ? val['S' - 'A'] : 0;
    enqueue(PEN, 0, 0, 0, constrain(amount, 0, 90) * 2.0);  // S0-90 like grbl-servo: servo 0-180 degrees
  } else if (m == 5) {
    enqueue(PEN, 0, 0, 0, 0);
  } else if (m != -1 && m != 2 && m != 30) {
    return "Unsupported command";
  }
  if (dwell) {
    if (!has['P' - 'A']) return "G4 needs P (seconds)";
    enqueue(DWELL, 0, 0, 0, max(val['P' - 'A'], 0.0));
  } else if (has['X' - 'A'] || has['Y' - 'A']) {
    long t[2];
    for (uint8_t i = 0; i < 2; i++) {
      t[i] = planned[i];
      if (has['X' - 'A' + i]) {
        long steps = lround(val['X' - 'A' + i] * cfg.stepsPerMm[i]);
        t[i] = absolute ? steps : planned[i] + steps;
      }
    }
    enqueue(MOVE, t[0], t[1], rapid ? 1e9 : feed, 0);
    planned[0] = t[0];
    planned[1] = t[1];
  }
  return 0;
}

// Tidy a line: drop comments and spaces, uppercase. Returns false if nothing is left.
bool tidy(char* s) {
  char* out = s;
  bool paren = false;
  for (char* p = s; *p; p++) {
    char c = *p;
    if (paren) { if (c == ')') paren = false; continue; }
    if (c == '(') { paren = true; continue; }
    if (c == ';') break;
    if (c == ' ' || c == '\t') continue;
    *out++ = toupper(c);
  }
  *out = 0;
  return out != s;
}

void runLine() {
  const char* error = 0;
  if (lineTooLong) error = "Line overflow";
  else if (line[0]) error = line[0] == '$' ? dollar(line) : gcode(line);  // (already tidied)
  if (error) { Serial.print("error: "); Serial.println(error); }
  else Serial.println("ok");
  lineReady = lineTooLong = false;
  lineLen = 0;
}

void softReset() {
  active = false;
  qCount = 0;
  hold = false;
  lineReady = lineTooLong = false;
  lineLen = 0;
  planned[0] = pos[0];
  planned[1] = pos[1];
  Serial.println();
  Serial.println(BANNER);
}

void readSerial() {
  while (Serial.available()) {
    char c = Serial.read();
    if (c == '?') { reportStatus(); continue; }
    if (c == '!') { hold = true; continue; }
    if (c == '~') { hold = false; continue; }
    if (c == 0x18) { softReset(); continue; }
    if (lineReady) continue;  // senders wait for "ok" before the next line
    if (c == '\n' || c == '\r') {
      if (lineLen || lineTooLong) {
        line[lineLen] = 0;
        tidy(line);
        lineReady = true;
      }
    } else if (lineLen < sizeof(line) - 1) {
      line[lineLen++] = c;
    } else {
      lineTooLong = true;
    }
  }
}

// ---------- main ----------
void setup() {
  for (uint8_t a = 0; a < 2; a++) {
    for (uint8_t i = 0; i < 4; i++) pinMode(COIL_PINS[a][i], OUTPUT);
    pinMode(PWM_PINS[a], OUTPUT);
  }
  pinMode(STBY_PIN, OUTPUT);
  digitalWrite(STBY_PIN, LOW);
  EEPROM.get(0, cfg);
  if (cfg.version != SETTINGS_VERSION) {
    defaults();
    EEPROM.put(0, cfg);
  }
  applyPower();
  pen.attach(SERVO_PIN);
  pen.write(180);  // pen up (M3 S90)
  Serial.begin(115200);
  Serial.println();
  Serial.println(BANNER);
}

void loop() {
  readSerial();
  // A line waits until the queue has room for everything it might add (a pen change and a move)
  // and, for settings and G92, until the machine has stopped.
  if (lineReady) {
    bool needsIdle = line[0] == '$' || strstr(line, "G92");
    if (qCount <= QUEUE - 2 && !(needsIdle && busy())) runLine();
  }
  if (!active && qCount) startBlock();
  if (active) {
    runBlock();
    idleSince = millis();
  } else if (!qCount && coilsOn && cfg.idleMs != 255 && millis() - idleSince > cfg.idleMs) {
    motorsOn(false);  // no current limit on the TB6612, so don't hold the coils on for no reason
  }
}
