# Firmware

The plotter runs [grbl-servo](https://github.com/robottini/grbl-servo), a GRBL 0.9i fork that
drives a pen-lift servo from spindle commands. The library is in `libraries/grbl` (GPL; see
`libraries/grbl/examples/grblUpload/license.txt`).

## Flashing the Arduino Nano

1. In the Arduino IDE, open **File > Preferences** and set **Sketchbook location** to this
   `firmware` folder. Restart the IDE.
2. Open **File > Examples > grbl > grblUpload**.
3. Select **Tools > Board > Arduino Nano**, **Processor > ATmega328P** (use **Old Bootloader** if
   the upload fails with "not in sync"), and the Nano's COM port.
4. Click **Upload**. The "low memory" warning is normal.
5. Open the Serial Monitor at **115200** baud with **Both NL & CR**. It should print
   `Grbl 0.9i ['$' for help]`.

## Pen servo

- Signal wire goes to **D11** (the `Z+` limit pin on a CNC shield).
- `M3 S0` to `M3 S90` sweeps the servo across its range (`SPINDLE_MAX_RPM` in `config.h`).
  The backend uses `M3 S90` (pen up) and `M3 S30` (pen down); tune them in
  `backend/gcode.py` for your mount.
