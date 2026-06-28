"""Claude Code hook client (speaks the moments where Claude wants you).

Wired to three hook events; it dispatches on `hook_event_name` in the payload:

  * Stop          - Claude finished a reply  -> speak the reply (from transcript)
  * Notification  - permission / input prompts -> speak the `message`
  * PreToolUse    - the AskUserQuestion tool  -> speak the question + options

It is intentionally tiny and fast (it must not block the agent): read the hook
JSON from stdin, work out what to say, clean it, and hand it to the warm TTS
daemon over a localhost socket. The daemon does the slow work (synthesis +
playback).

Exit code is always 0 - a TTS hiccup must never interfere with Claude Code.
"""
from __future__ import annotations

import json
import re
import socket
import sys

import config
import text_filter
import transcript


def _send(text: str) -> None:
    # Protocol is newline-delimited; collapse newlines so the full reply is one line.
    line = " ".join(text.splitlines())
    with socket.create_connection((config.HOST, config.PORT), timeout=1.5) as s:
        s.sendall((line + "\n").encode("utf-8"))


def _already_spoken(session_id: str, uuid: str) -> bool:
    """Avoid re-speaking the same message if Stop fires more than once."""
    if not uuid:
        return False
    config.STATE_DIR.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", session_id or "default")
    marker = config.STATE_DIR / f"last_{safe}.txt"
    try:
        if marker.read_text(encoding="utf-8").strip() == uuid:
            return True
    except OSError:
        pass
    try:
        marker.write_text(uuid, encoding="utf-8")
    except OSError:
        pass
    return False


def _stop_text(payload: dict) -> str:
    """The finished reply: last assistant message, de-duplicated per session."""
    transcript_path = payload.get("transcript_path")
    if not transcript_path:
        return ""
    uuid, raw = transcript.last_assistant_message(transcript_path)
    if not raw:
        return ""
    if _already_spoken(payload.get("session_id", ""), uuid):
        return ""
    return raw


def _notification_text(payload: dict) -> str:
    """A permission / input prompt. Skip the redundant idle 'waiting' nag."""
    if not config.SPEAK_NOTIFICATIONS:
        return ""
    if payload.get("notification_type") == "idle_prompt" and not config.SPEAK_IDLE:
        return ""
    message = payload.get("message")
    return message.strip() if isinstance(message, str) else ""


def _question_text(payload: dict) -> str:
    """The AskUserQuestion tool: read each question, then its option labels."""
    if not config.SPEAK_QUESTIONS:
        return ""
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return ""
    questions = tool_input.get("questions")
    if not isinstance(questions, list):
        return ""
    parts = []
    for q in questions:
        if not isinstance(q, dict):
            continue
        question = q.get("question")
        if isinstance(question, str) and question.strip():
            parts.append(question.strip())
        labels = [
            o["label"].strip()
            for o in (q.get("options") or [])
            if isinstance(o, dict) and isinstance(o.get("label"), str) and o["label"].strip()
        ]
        if labels:
            parts.append("Options: " + "; ".join(labels) + ".")
    return " ".join(parts).strip()


def main() -> int:
    if config.MUTE:
        return 0

    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0
    if not isinstance(payload, dict):
        return 0

    event = payload.get("hook_event_name", "")
    if event == "Notification":
        raw = _notification_text(payload)
    elif event == "PreToolUse" and payload.get("tool_name") == "AskUserQuestion":
        raw = _question_text(payload)
    else:
        # Stop (also the fallback when no event name is present).
        raw = _stop_text(payload)

    text = text_filter.clean(raw, max_chars=config.MAX_CHARS)
    if not text:
        return 0

    try:
        _send(text)
    except OSError:
        # Daemon not running. SessionStart should have started it; stay silent
        # rather than block. (See README for manual start.)
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
