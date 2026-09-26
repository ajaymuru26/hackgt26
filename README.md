# Whiteboard Finisher

Draw on the whiteboard and press **Finish it**. The app can solve handwritten math,
continue a pattern, finish a drawing, or answer a handwritten question. It can also
write a prompt or text on the board. Generated pen strokes animate in blue, and
**Download G-code** exports them for a pen plotter.

## Run it

From the repository root, create a virtual environment, install the backend
dependencies, and configure an AI provider key:

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
```

Edit `backend/.env` and replace the placeholder with a real API key. The app uses
OpenAI when `OPENAI_API_KEY` is set; otherwise it uses Claude when
`ANTHROPIC_API_KEY` is set. Keep the key private and do not commit `.env`.

To use Claude instead, put this in `backend/.env`:

```dotenv
ANTHROPIC_API_KEY=your-anthropic-api-key
```

Start the server from the `backend` directory:

```bash
python -m uvicorn main:app --reload
```

On Windows, activate the environment with `.venv\Scripts\activate` instead of
`source .venv/bin/activate`.

Open http://localhost:8000

Optional model settings can also go in `backend/.env`:

```dotenv
OPENAI_MODEL=gpt-4o
CLAUDE_MODEL=claude-sonnet-5
```

The defaults are `gpt-4o` and `claude-sonnet-5`, respectively.

## Features

- **Auto, Math, Pattern, and Drawing modes:** choose a mode or let the AI classify the board.
- **Ask tool:** click the board and enter a question or drawing instruction; the response is written there.
- **Write text:** choose print or cursive style and a size.
- **Thought process:** see the interpretation, calculations, and planned robot strokes.
- **Undo, clear, replay, and speed controls** for editing and previewing the output.
- **G-code export** for pen plotters.

Math is solved by the AI. For example:

- `12+7=` writes `19` after the equals sign.
- `2x+3=7` writes `x=2` underneath.
- `x^2-5x+6=0` writes `x=2,3`.
- `2+2=4` writes a check mark.
- `1/3+1/6=` writes `1/2`.

## Backend map

- `backend/main.py`: FastAPI routes, request validation, and response assembly. Also serves the frontend.
- `backend/vision.py`: prepares board images and stroke coordinates, and asks the AI to read and solve the maths.
- `backend/shapes.py`: converts AI-described shapes into strokes and snaps them to the user's drawing.
- `backend/handwriting.py` and `backend/text_writer.py`: convert answers and text into plotter strokes.
- `backend/gcode.py`: orders strokes, converts pixels to millimetres, and generates G-code.
- `frontend/app.js`: records and submits strokes, then animates the backend response.

The main API routes are `POST /api/complete`, `POST /api/ask`, `POST /api/write`, and
`POST /api/gcode`.

## Hooking up the robot

Edit `PEN_UP` and `PEN_DOWN` in `backend/gcode.py` for your pen-lift hardware (servo or
Z axis). Set `board_width_mm` and `board_height_mm` in the `/api/gcode` request to match
the plotter's drawing area. Send the downloaded file with a G-code sender such as UGS or
CNCjs. The current frontend uses a 1200 x 700 logical-pixel canvas.
