"""Remember a wrong answer in Backboard so the next hint can mention it.

MongoDB still stores the finished board. Backboard stores the mistake itself,
tied to one assistant, and a later Hint search pulls the closest facts back.
"""
import json
import os
import ssl
import urllib.error
import urllib.request
from pathlib import Path

import certifi

_API = "https://app.backboard.io/api"
_ID_FILE = Path(__file__).resolve().parent / ".backboard_assistant"
_assistant = ""


def configured() -> bool:
    return bool(os.environ.get("BACKBOARD_API_KEY", "").strip())


def _request(method: str, path: str, payload: dict | None = None):
    key = os.environ.get("BACKBOARD_API_KEY", "").strip()
    if not key:
        return None
    data = None if payload is None else json.dumps(payload).encode()
    request = urllib.request.Request(
        _API + path,
        data=data,
        method=method,
        headers={"X-API-Key": key, "Content-Type": "application/json", "Accept": "application/json"},
    )
    context = ssl.create_default_context(cafile=certifi.where())
    try:
        with urllib.request.urlopen(request, timeout=20, context=context) as response:
            raw = response.read().decode()
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:300]
        print(f"Backboard {method} {path} failed ({e.code}): {detail}")
        return None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        print(f"Backboard {method} {path} skipped: {e}")
        return None
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def assistant_id() -> str:
    """The assistant whose memories every hint shares. Created once, then reused."""
    global _assistant
    if _assistant:
        return _assistant
    chosen = os.environ.get("BACKBOARD_ASSISTANT_ID", "").strip()
    if not chosen and _ID_FILE.exists():
        chosen = _ID_FILE.read_text().strip()
    if chosen:
        _assistant = chosen
        return _assistant
    created = _request("POST", "/assistants", {
        "name": "Whiteboard tutor",
        "system_prompt": "Remember math mistakes a student made on a classroom whiteboard.",
    })
    if not isinstance(created, dict):
        return ""
    chosen = str(created.get("assistant_id") or created.get("id") or "").strip()
    if not chosen:
        return ""
    try:
        _ID_FILE.write_text(chosen)
    except OSError as e:
        print(f"Backboard assistant id not saved locally: {e}")
    _assistant = chosen
    return _assistant


def remember(fact: str) -> bool:
    """Store one mistake. False means Backboard is off or the call failed."""
    text = " ".join((fact or "").split())[:400]
    if not text or not configured():
        return False
    aid = assistant_id()
    if not aid:
        return False
    saved = _request("POST", f"/assistants/{aid}/memories", {"content": text})
    return isinstance(saved, dict)


def hint_note() -> str:
    """A short note for the vision prompt, naming mistakes that may match this board."""
    if not configured():
        return ""
    aid = assistant_id()
    if not aid:
        return ""
    found = _request("POST", f"/assistants/{aid}/memories/search", {
        "query": "math problems this student got wrong",
        "limit": 3,
    })
    if not isinstance(found, dict):
        return ""
    facts = []
    for item in found.get("memories") or []:
        if not isinstance(item, dict):
            continue
        content = " ".join(str(item.get("content") or "").split())
        if content:
            facts.append(content[:180])
    if not facts:
        return ""
    return ("This student previously got these wrong: " + "; ".join(facts)
            + ". If this board is the same kind of problem, mention that past mistake in the hint. "
            "Do not give the final answer.")
