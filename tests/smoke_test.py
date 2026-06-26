"""Offline end-to-end smoke test for the voice pipeline.

Builds a fake Claude Code transcript containing markdown, a code block, and an
over-long paragraph, then runs it through the real transcript parser + text
filter and prints what *would* be spoken. If the TTS daemon is running, it also
sends the result to the speaker so you can hear it.

Run:  .\.venv\Scripts\python.exe .\tests\smoke_test.py
"""
from __future__ import annotations

import json
import socket
import sys
import tempfile
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

import config          # noqa: E402
import text_filter     # noqa: E402
import transcript      # noqa: E402

LONG = ("This is a deliberately long sentence designed to push past the "
        "character cap so that the truncation logic and the trailing notice "
        "are exercised properly during the smoke test. ") * 8

ASSISTANT_TEXT = (
    "# Here's the fix\n\n"
    "I updated the **handler** and added a `retry` wrapper. "
    "See the [docs](https://example.com) for details.\n\n"
    "```python\n"
    "def retry(fn):\n"
    "    return fn()\n"
    "```\n\n"
    "That should resolve the *timeout* issue.\n\n"
    + LONG
)


def build_transcript(path: Path) -> None:
    lines = [
        {"type": "user", "uuid": "u1",
         "message": {"role": "user", "content": "fix the bug"}},
        {"type": "assistant", "uuid": "a1",
         "message": {"role": "assistant",
                     "content": [{"type": "text", "text": "older reply"}]}},
        {"type": "assistant", "uuid": "a2",
         "message": {"role": "assistant",
                     "content": [
                         {"type": "text", "text": ASSISTANT_TEXT},
                         {"type": "tool_use", "name": "Edit", "input": {}},
                     ]}},
    ]
    path.write_text("\n".join(json.dumps(o) for o in lines), encoding="utf-8")


def main() -> int:
    tmp = Path(tempfile.gettempdir()) / "voice_of_claude_smoke.jsonl"
    build_transcript(tmp)

    payload = {"hook_event_name": "Stop", "session_id": "smoke",
               "transcript_path": str(tmp)}
    print("Stop payload:", json.dumps(payload))

    uuid, raw = transcript.last_assistant_message(payload["transcript_path"])
    assert uuid == "a2", f"expected last assistant uuid a2, got {uuid!r}"
    print(f"\nExtracted last assistant message (uuid={uuid}, {len(raw)} chars).")

    spoken = text_filter.clean(raw, max_chars=config.MAX_CHARS)
    print("\n--- WOULD SPEAK ---")
    print(spoken)
    print("-------------------")

    assert "def retry" not in spoken, "code leaked into speech!"
    assert "code block" in spoken, "code-block notice missing!"
    assert "see the terminal" in spoken.lower(), "truncation notice missing!"
    print("\nFilter assertions passed.")

    try:
        with socket.create_connection((config.HOST, config.PORT), timeout=1.0) as s:
            s.sendall((spoken + "\n").encode("utf-8"))
        print(f"Sent to daemon on {config.HOST}:{config.PORT} - you should hear it.")
    except OSError:
        print(f"\n(Daemon not running on {config.HOST}:{config.PORT}; "
              "start tts_server.py to hear audio.)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
