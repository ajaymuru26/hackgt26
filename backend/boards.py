"""Finished-board history in MongoDB Atlas.

The live board stays in the browser. This only stores a copy after the robot
finishes, and it never blocks an answer when Atlas is unset or unreachable.
"""
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import certifi
from bson import ObjectId
from bson.errors import InvalidId
from dotenv import load_dotenv
from pymongo import MongoClient
from pymongo.errors import PyMongoError

_ENV = Path(__file__).resolve().parent / ".env"
_client = None
_uri = None
_indexed = False
_down_until = 0.0

MAX_STROKES = 400
MAX_POINTS = 120


def configured() -> bool:
    _reload()
    uri = os.getenv("MONGODB_URI", "").strip()
    return uri.startswith("mongodb://") or uri.startswith("mongodb+srv://")


def remember(result, *, source, action="", provider="", user_strokes=None, heard="", seconds=0.0):
    """Store one finished board. Returns the id, or None if it was not saved."""
    global _down_until
    heard = heard or str(result.pop("_heard", "") or "")
    if time.time() < _down_until:
        return None
    coll = _collection()
    if coll is None:
        return None
    strokes = []
    for pts in user_strokes or []:
        thin = _thin(pts)
        if thin:
            strokes.append({"owner": "user", "points": thin})
    for pts in result.get("prompt_strokes") or []:
        thin = _thin(pts)
        if thin:
            strokes.append({"owner": "user", "points": thin})
    for pts in result.get("strokes") or []:
        thin = _thin(pts)
        if thin:
            strokes.append({"owner": "robot", "points": thin})
    label = str(result.get("answer") or result.get("expression") or result.get("description") or source)
    doc = {
        "created": datetime.now(timezone.utc),
        "source": source,
        "action": action,
        "mode": str(result.get("mode") or ""),
        "provider": provider,
        "label": label[:120],
        "answer": str(result.get("answer") or "")[:240],
        "description": str(result.get("description") or "")[:240],
        "expression": str(result.get("expression") or "")[:240],
        "heard": heard[:240],
        "seconds": round(float(seconds or 0), 1),
        "stroke_count": len(strokes[:MAX_STROKES]),
        "strokes": strokes[:MAX_STROKES],
    }
    try:
        inserted = coll.insert_one(doc)
    except PyMongoError as e:
        _down_until = time.time() + 30
        print(f"MongoDB save skipped: {e}")
        return None
    board_id = str(inserted.inserted_id)
    result["board_id"] = board_id
    return board_id


def list_boards(limit=20):
    coll = _collection()
    if coll is None:
        return []
    try:
        cursor = coll.find({}, {"strokes": 0}).sort("created", -1).limit(limit)
        return [_summary(doc) for doc in cursor]
    except PyMongoError as e:
        print(f"MongoDB list skipped: {e}")
        return []


def get_board(board_id):
    coll = _collection()
    if coll is None:
        return None
    try:
        oid = ObjectId(board_id)
    except (InvalidId, TypeError):
        return None
    try:
        doc = coll.find_one({"_id": oid})
    except PyMongoError as e:
        print(f"MongoDB read skipped: {e}")
        return None
    if not doc:
        return None
    summary = _summary(doc)
    summary["strokes"] = doc.get("strokes") or []
    return summary


def _summary(doc):
    created = doc.get("created")
    return {
        "id": str(doc["_id"]),
        "created": created.isoformat() if hasattr(created, "isoformat") else "",
        "source": doc.get("source") or "",
        "action": doc.get("action") or "",
        "mode": doc.get("mode") or "",
        "label": doc.get("label") or "",
        "seconds": doc.get("seconds") or 0,
        "strokes": doc.get("stroke_count") if doc.get("stroke_count") is not None else len(doc.get("strokes") or []),
    }


def _collection():
    global _client, _uri, _indexed, _down_until
    if time.time() < _down_until:
        return None
    _reload()
    uri = os.getenv("MONGODB_URI", "").strip()
    if not uri.startswith("mongodb://") and not uri.startswith("mongodb+srv://"):
        return None
    try:
        if _client is None or uri != _uri:
            _client = MongoClient(uri, tlsCAFile=certifi.where(), serverSelectionTimeoutMS=2500)
            _uri = uri
            _indexed = False
        name = os.getenv("MONGODB_DB", "").strip()
        if name:
            db = _client[name]
        else:
            try:
                db = _client.get_default_database()
            except PyMongoError:
                db = _client["whiteboard"]
        coll = db["boards"]
        if not _indexed:
            coll.create_index("created")
            _indexed = True
        return coll
    except PyMongoError as e:
        print(f"MongoDB unavailable: {e}")
        _client = None
        _uri = None
        _indexed = False
        _down_until = time.time() + 30
        return None


def _reload():
    load_dotenv(_ENV, override=True)


def _thin(points, limit=MAX_POINTS):
    clean = []
    for p in points or []:
        if len(p) < 2:
            continue
        clean.append([round(float(p[0]), 1), round(float(p[1]), 1)])
    if len(clean) <= limit:
        return clean
    step = (len(clean) - 1) / (limit - 1)
    return [clean[round(i * step)] for i in range(limit)]
