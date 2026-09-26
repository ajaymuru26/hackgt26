"""Start a Vultr cloud server for this app.

Requires VULTR_API_KEY in the environment. Creating an instance bills the Vultr
account, so this does nothing until you run it on purpose:

    python vultr_up.py

Then point a .tech domain at the printed IP, copy this repo onto the server,
and run: docker build -t whiteboard . && docker run --env-file backend/.env -p 80:8000 whiteboard
"""
import json
import os
import ssl
import urllib.request
from pathlib import Path

import certifi
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

API = "https://api.vultr.com/v2"


def _get(path: str, key: str):
    request = urllib.request.Request(API + path, headers={"Authorization": f"Bearer {key}"})
    context = ssl.create_default_context(cafile=certifi.where())
    with urllib.request.urlopen(request, timeout=30, context=context) as response:
        return json.loads(response.read().decode())


def _post(path: str, key: str, payload: dict):
    body = json.dumps(payload).encode()
    request = urllib.request.Request(
        API + path,
        data=body,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    context = ssl.create_default_context(cafile=certifi.where())
    with urllib.request.urlopen(request, timeout=60, context=context) as response:
        return json.loads(response.read().decode())


def ubuntu_id(key: str) -> int:
    for os_row in _get("/os", key).get("os") or []:
        name = str(os_row.get("name") or "")
        if "Ubuntu 24.04" in name and "x64" in name:
            return int(os_row["id"])
    raise SystemExit("Vultr did not list Ubuntu 24.04 x64.")


def main():
    key = os.environ.get("VULTR_API_KEY", "").strip()
    if not key:
        raise SystemExit("Add VULTR_API_KEY to backend/.env first. This script creates a paid cloud server.")
    os_id = ubuntu_id(key)
    created = _post("/instances", key, {
        "region": os.environ.get("VULTR_REGION", "ewr"),
        "plan": os.environ.get("VULTR_PLAN", "vc2-1c-1gb"),
        "os_id": os_id,
        "label": "whiteboard-finisher",
        "hostname": "whiteboard",
        "user_data": (
            "#!/bin/bash\n"
            "apt-get update && apt-get install -y docker.io\n"
            "systemctl enable --now docker\n"
        ),
    })
    instance = created.get("instance") or {}
    print("Vultr instance:", instance.get("id"))
    print("Status:", instance.get("status"))
    print("When it shows an IP, point your .tech domain at it, copy this repo over, and run the Docker command in this file's header.")


if __name__ == "__main__":
    main()
