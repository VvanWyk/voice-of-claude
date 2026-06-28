"""Extract the latest assistant message text from a Claude Code transcript.

The transcript is a JSONL file. Each line is a JSON object. Assistant message
lines look like:

    {
      "type": "assistant",
      "uuid": "....",
      "message": {
        "role": "assistant",
        "content": [
          {"type": "text", "text": "Hello"},
          {"type": "tool_use", ...}
        ]
      }
    }

We take the LAST line whose type is "assistant" and concatenate its text blocks.
Returns (uuid, text). Either may be "" if nothing usable is found.

The parsing is defensive: schemas drift, and some assistant content arrives as a
plain string rather than a list of blocks.
"""
from __future__ import annotations

import json
from typing import Tuple


def _extract_text(message: dict) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                t = block.get("text")
                if isinstance(t, str):
                    parts.append(t)
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(parts).strip()
    return ""


def last_assistant_message(transcript_path: str) -> Tuple[str, str]:
    """Return (uuid, text) for the last assistant turn.

    Claude Code writes one JSONL entry per streaming chunk, so a single response
    produces many consecutive assistant entries. We walk backwards and collect
    all of them, stopping at the first non-assistant line, then join the chunks
    in order to reconstruct the full reply.
    """
    try:
        with open(transcript_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except (OSError, UnicodeDecodeError):
        return "", ""

    chunks: list[tuple[str, str]] = []  # (uuid, text) in reverse order
    for raw in reversed(lines):
        raw = raw.strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        if obj.get("type") != "assistant":
            break  # hit a user/tool line — all streaming chunks collected
        message = obj.get("message")
        if not isinstance(message, dict):
            continue
        text = _extract_text(message)
        if text:
            uuid = obj.get("uuid") or message.get("id") or ""
            chunks.append((uuid, text))

    if not chunks:
        return "", ""

    chunks.reverse()  # restore chronological order
    last_uuid = chunks[-1][0]
    full_text = "\n\n".join(text for _, text in chunks)
    return last_uuid, full_text
