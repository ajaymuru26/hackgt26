"""
Turn strokes (canvas pixels) into G-code for a GRBL-style pen plotter.

Adjust PEN_UP / PEN_DOWN to match your hardware:
  - servo pen lift on GRBL (e.g. grbl-servo): "M3 S90" / "M3 S30" style commands
  - Z-axis pen lift: "G0 Z5" / "G1 Z0 F500"
"""
import math

PEN_UP = "M3 S90"
PEN_DOWN = "M3 S30"
PEN_DELAY = "G4 P0.15"  # short pause so the pen finishes moving


def order_strokes(strokes):
    """Greedy nearest-neighbour ordering (and flipping) so the pen travels less."""
    remaining = [s for s in strokes if len(s) >= 2]
    ordered, pos = [], (0.0, 0.0)
    while remaining:
        best_i, best_d, flip = 0, float("inf"), False
        for i, s in enumerate(remaining):
            d_start = math.dist(pos, s[0])
            d_end = math.dist(pos, s[-1])
            if d_start < best_d:
                best_i, best_d, flip = i, d_start, False
            if d_end < best_d:
                best_i, best_d, flip = i, d_end, True
        s = remaining.pop(best_i)
        s = s[::-1] if flip else s
        ordered.append(s)
        pos = s[-1]
    return ordered


def strokes_to_gcode(strokes, canvas_w, canvas_h, board_w_mm, board_h_mm,
                     draw_feed=3000, flip_y=True) -> str:
    sx, sy = board_w_mm / canvas_w, board_h_mm / canvas_h

    def mm(p):
        x = p[0] * sx
        y = (canvas_h - p[1]) * sy if flip_y else p[1] * sy  # machine Y usually points up
        return f"X{x:.2f} Y{y:.2f}"

    lines = ["G21 ; millimetres", "G90 ; absolute positioning", PEN_UP, PEN_DELAY]
    for stroke in order_strokes(strokes):
        lines.append(f"G0 {mm(stroke[0])}")
        lines += [PEN_DOWN, PEN_DELAY]
        lines += [f"G1 {mm(p)} F{draw_feed}" for p in stroke[1:]]
        lines += [PEN_UP, PEN_DELAY]
    lines.append("G0 X0 Y0")
    return "\n".join(lines) + "\n"
