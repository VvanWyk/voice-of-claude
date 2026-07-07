"""Play a short attention chime for Notification and PreToolUse hooks.

Only notifications that actually need the user trigger the chime: the idle
"waiting for your input" reminder (which fires ~a minute after every reply)
is skipped unless TTS_BELL_IDLE=1. Every event is appended to .state/bell.log
with its type and message, so a mystery chime can be traced to its source.

Configure the sound file via the TTS_BELL_SOUND environment variable:
    TTS_BELL_SOUND=C:\\path\\to\\chime.wav

Supports WAV files (via winsound, built-in) and MP3/other formats
(via sounddevice + soundfile, if installed). Falls back to the Windows
system Asterisk sound if no file is configured or the file is missing.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

STATE_DIR = Path(__file__).resolve().parent.parent / ".state"


def _play_wav(path: str) -> None:
    import winsound
    winsound.PlaySound(path, winsound.SND_FILENAME)


def _play_other(path: str) -> None:
    import soundfile as sf
    import sounddevice as sd
    data, sr = sf.read(path, dtype="float32")
    sd.play(data, sr)
    sd.wait()


def _play_system_fallback() -> None:
    import winsound
    winsound.MessageBeep(winsound.MB_ICONASTERISK)
    import time; time.sleep(0.8)


def play() -> None:
    path = os.environ.get("TTS_BELL_SOUND", "").strip()
    if path and os.path.isfile(path):
        ext = os.path.splitext(path)[1].lower()
        if ext == ".wav":
            _play_wav(path)
        else:
            try:
                _play_other(path)
            except Exception:
                _play_system_fallback()
    else:
        _play_system_fallback()


def _log(line: str) -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        with open(STATE_DIR / "bell.log", "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {line}\n")
    except OSError:
        pass


def _should_chime(payload: dict) -> bool:
    """Chime only when Claude is actually blocked on the user."""
    if payload.get("hook_event_name", "") != "Notification":
        return True  # PreToolUse:AskUserQuestion - a real question
    ntype = str(payload.get("notification_type") or "").lower()
    msg = str(payload.get("message") or "").lower()
    if ntype == "idle_prompt" or "waiting for your input" in msg:
        # Idle nag ~60s after each reply; redundant with the spoken reply.
        return os.environ.get("TTS_BELL_IDLE", "0").strip().lower() in (
            "1", "true", "yes", "on"
        )
    return True


def main() -> int:
    if os.environ.get("TTS_MUTE", "").strip().lower() in ("1", "true", "yes", "on"):
        return 0  # muted (e.g. inside the TL;DR summariser's claude session)
    try:
        payload = json.load(sys.stdin)
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}

    chime = _should_chime(payload)
    _log(
        f"event={payload.get('hook_event_name', '')!r} "
        f"type={payload.get('notification_type', '')!r} "
        f"tool={payload.get('tool_name', '')!r} "
        f"chime={chime} "
        f"msg={str(payload.get('message') or '')[:120]!r}"
    )
    if chime:
        try:
            play()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
