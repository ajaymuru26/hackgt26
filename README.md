# Whiteboard Finisher

Draw on a web whiteboard, press **Finish it**, and the backend completes it:
math problems get solved and the answer is written in, partial drawings get finished.
The robot's lines animate in blue, and **Download G-code** exports them for a pen plotter.

## Run it

```bash
cd backend
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...        # Windows: set ANTHROPIC_API_KEY=sk-ant-...
uvicorn main:app --reload

cd backend
python3 -m uvicorn main:app --reload
```

Open http://localhost:8000

Optional: `export CLAUDE_MODEL=claude-opus-5-5` to try a stronger model (default is `claude-sonnet-5`).

## Files

- `frontend/app.js`: canvas whiteboard (1200x700 logical coords), records strokes, sends PNG + strokes, animates the reply
- `backend/main.py`: `POST /api/complete` and `POST /api/gcode`, also serves the frontend
- `backend/vision.py`: adds a labeled grid to the image, asks Claude what it sees, returns JSON
- `backend/math_solver.py`: SymPy solves the equation so the answer is exact
- `backend/handwriting.py`: single-line stroke font that writes the answer as pen strokes
- `backend/gcode.py`: orders strokes to reduce travel, converts pixels to mm, outputs G-code

## Math it handles

- `12+7=` writes `19` after the =
- `2x+3=7` writes `x=2` underneath
- `x^2-5x+6=0` writes `x=2,3`
- `2+2=4` writes a check mark
- `1/3+1/6=` writes `1/2`

## Hooking up the robot

Edit `PEN_UP` / `PEN_DOWN` in `gcode.py` for your pen lift (servo or Z axis), and set
`board_width_mm` / `board_height_mm` in the `/api/gcode` request to your drawing area.
Send the file with a G-code sender (UGS, CNCjs) or pyserial. For a real whiteboard,
replace the canvas snapshot with a camera photo flattened with a homography; everything
else stays the same.
