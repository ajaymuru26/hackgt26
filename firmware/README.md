# Firmware

Two options, depending on the motor drivers:

| Drivers | Firmware |
|---|---|
| **TB6612FNG** dual H-bridges (what we have) | `plotter_tb6612/` (ours) |
| A4988 / DRV8825 / TMC2209 step-dir drivers | GRBL, in `libraries/grbl` (see the end) |

Both speak the same serial protocol, so the app's **Send to robot** works with either.

## TB6612FNG firmware (`plotter_tb6612/`)

GRBL can't drive a TB6612FNG: GRBL outputs step/direction pulses, and a TB6612FNG needs
the Nano to switch each coil itself. This firmware does that, and answers like GRBL
(`ok`, `?` status, `!` pause, `~` resume, Ctrl-X stop, `$$` settings).

### Wiring

One TB6612FNG per motor (a NEMA 17 has two coils, and each board has two H-bridges).

| TB6612FNG pin | X board | Y board |
|---|---|---|
| AIN1 | D2 | A0 |
| AIN2 | D3 | A1 |
| BIN1 | D4 | A2 |
| BIN2 | D7 | A3 |
| PWMA **and** PWMB (tie together) | D5 | D6 |
| STBY | D8 | D8 (shared) |
| VCC (logic) | Nano 5V | Nano 5V |
| VM (motor power) | motor supply + | motor supply + |
| GND (all of them) | Nano GND + supply - | Nano GND + supply - |
| AO1, AO2 | motor coil A (one pair of wires) | motor coil A |
| BO1, BO2 | motor coil B (the other pair) | motor coil B |

Pen servo: signal to **D11**, power to 5V (a separate 5 V supply is better than the Nano's
5V pin once the servo is loaded), ground to GND.

**Finding a motor's coil pairs:** with a multimeter on resistance, the two wires that read a
few ohms to tens of ohms between them are one coil. Wires from different coils read open.
(Common colours: black+green is one coil, red+blue the other, but check.)

### Before connecting motor power

The TB6612FNG has **no current limit**: coil current is simply supply voltage / coil
resistance. It's rated about 1.2 A per channel and 13.5 V maximum on VM.

1. Measure (or read from the label) the motor's coil resistance R.
2. Current at full power = V / R. For a 12 V supply: R = 30 ohm -> 0.4 A (fine);
   R = 2 ohm -> 6 A (the chip overheats and shuts down or dies).
3. `$140` (motor power, % PWM) scales that down. Start low, e.g. `$140=30`, and raise it
   until the motor stops skipping steps. Keep the average current under ~1 A.
4. If the chips get too hot to touch, lower `$140` or use a lower-voltage supply.

The coils switch off 250 ms after the last move (`$1`) so they don't sit heating up.

### Uploading

1. Close the Arduino Serial Monitor and disconnect the robot in the app.
2. Arduino IDE: **File > Open** `firmware/plotter_tb6612/plotter_tb6612.ino`.
3. **Tools > Board > Arduino Nano**, **Processor > ATmega328P** (or Old Bootloader), the
   Nano's port, then **Upload**.
4. Serial Monitor at 115200 baud should show `Grbl 0.9i ['$' for help] (TB6612 plotter)`.

This replaces GRBL on the Nano. To go back, upload `grblUpload` as described below.

### Setting it up

Send these from the Serial Monitor (or the app), one per line. Settings are saved on the Nano.

| Setting | Meaning | Default |
|---|---|---|
| `$100`, `$101` | steps per mm, X and Y | 2.768 (measured, see below) |
| `$110`, `$111` | top speed, mm/min | 3000 |
| `$120`, `$121` | acceleration, mm/s^2 | 150 |
| `$140` | motor power, % | 60 |
| `$150` | 1 = full steps (more torque), 0 = half steps (smoother) | 0 |
| `$3` | reverse an axis: 1 = X, 2 = Y, 3 = both | 0 |
| `$1` | coils off after this many ms idle (255 = always on) | 250 |

**Steps per mm** for a rack and pinion:

```
steps/mm = steps per revolution / (pinion teeth x rack tooth pitch in mm)
```

A 200-step NEMA 17 in half steps (`$150=0`) makes 400 steps per revolution. Our parts (from
the STLs): a 92-tooth gear on a module-0.5 rack (1.5708 mm per tooth) moves 92 x 1.5708 =
144.5 mm per turn, so **400 / 144.5 = 2.768 steps/mm** (1.384 in full steps). That assumes the
gear sits directly on the motor shaft.

To check it: send `G91 G0 X50`, measure how far the rack actually moved (say 48 mm), then set
`$100 = 2.768 x 50 / 48`, i.e. current value x commanded / measured.

**Drawing area:** the racks allow about 174 mm (X) and 190 mm (Y) of travel. Measure what the
built frame reaches and set `BOARD_WIDTH_MM` / `BOARD_HEIGHT_MM` in `backend/.env` (default
160 x 170). The app keeps drawings in proportion and centres them in that area.

**Zero:** move the pen to the board's bottom-left corner and send `G92 X0 Y0`.

**Pen:** `M3 S90` should lift the pen and `M3 S30` lower it (S0-90 = servo 0-180 degrees).
Adjust `PEN_UP` / `PEN_DOWN` in `backend/gcode.py` to suit the mount.

## GRBL (for step/dir drivers)

`libraries/grbl` is [grbl-servo](https://github.com/robottini/grbl-servo), a GRBL 0.9i
fork that drives a pen-lift servo from spindle commands (GPL; see
`libraries/grbl/examples/grblUpload/license.txt`). Use it if the TB6612FNGs are swapped for
A4988 / DRV8825 / TMC2209 drivers (with a CNC shield).

1. In the Arduino IDE, open **File > Preferences** and set **Sketchbook location** to this
   `firmware` folder. Restart the IDE.
2. Open **File > Examples > grbl > grblUpload**.
3. Select **Tools > Board > Arduino Nano**, **Processor > ATmega328P** (use **Old Bootloader** if
   the upload fails with "not in sync"), and the Nano's COM port.
4. Click **Upload**. The "low memory" warning is normal.
5. Open the Serial Monitor at **115200** baud with **Both NL & CR**. It should print
   `Grbl 0.9i ['$' for help]`.

Pen servo signal goes to **D11** (the `Z+` limit pin on a CNC shield). `M3 S0` to `M3 S90`
sweeps the servo across its range.
