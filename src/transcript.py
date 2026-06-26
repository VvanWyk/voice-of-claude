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
    last_uuid = ""
    last_text = ""
    try:
        with open(transcript_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(obj, dict):
                    continue
                if obj.get("type") != "assistant":
                    continue
                message = obj.get("message")
                if not isinstance(message, dict):
                    continue
                text = _extract_text(message)
                if text:
                    last_text = text
                    last_uuid = obj.get("uuid") or message.get("id") or ""
    except (OSError, UnicodeDecodeError):
        return "", ""
    return last_uuid, last_text
