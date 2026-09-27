"""Speak a finished answer with ElevenLabs text-to-speech.

Talk already sends audio to their speech-to-text API. This is the other direction:
the same key reads the answer or the hint back out loud.
"""
import json
import os
import ssl
import urllib.error
import urllib.request

import certifi

_VOICE = "pNInz6obpgDQGcFmaJgB"  # Adam. Rachel is a library voice and the free API rejects it.


def configured() -> bool:
    return bool(os.environ.get("ELEVENLABS_API_KEY", "").strip())


def synthesize(text: str) -> bytes | None:
    """MP3 bytes, or None when there is nothing to say or the call fails."""
    spoken = " ".join((text or "").split())[:800]
    key = os.environ.get("ELEVENLABS_API_KEY", "").strip()
    if not spoken or not key:
        return None
    voice = os.environ.get("ELEVENLABS_VOICE_ID", _VOICE).strip() or _VOICE
    model = os.environ.get("ELEVENLABS_TTS_MODEL", "eleven_flash_v2_5").strip() or "eleven_flash_v2_5"
    body = json.dumps({"text": spoken, "model_id": model}).encode()
    request = urllib.request.Request(
        f"https://api.elevenlabs.io/v1/text-to-speech/{voice}",
        data=body,
        headers={
            "xi-api-key": key,
            "Content-Type": "application/json",
            "Accept": "audio/mpeg",
        },
    )
    context = ssl.create_default_context(cafile=certifi.where())
    try:
        with urllib.request.urlopen(request, timeout=30, context=context) as response:
            audio = response.read()
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:180]
        print(f"ElevenLabs speech skipped: HTTP {e.code} {detail}")
        return None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        print(f"ElevenLabs speech skipped: {e}")
        return None
    return audio or None
