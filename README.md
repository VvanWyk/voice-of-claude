# The Voice of Claude

Give Claude Code a **local voice**. When Claude finishes a response, a CPU-only
text-to-speech engine speaks a cleaned-up version aloud. **No cloud, no GPU** —
everything runs on your PC.

Voice *input* is already handled by Claude Code's built-in dictation (hold
spacebar). This project adds the missing half: voice *output*.

## Two engines

| Engine | Speed on an i7-1265U (measured) | Voice | License |
|--------|----------------------------------|-------|---------|
| **[Piper](https://github.com/OHF-Voice/piper1-gpl)** (default) | ~0.5x real-time (≈2x faster than real-time) — **low latency** | Good, slightly synthetic | GPL-3.0 |
| **[Kokoro](https://github.com/thewh1teagle/kokoro-onnx)** | ~3.7x real-time (slower than real-time) — laggy on this CPU | More natural | Apache-2.0 / MIT |

Piper is the default because it comfortably hits the low-latency goal on a 15W
laptop CPU and streams audio as it generates. Switch to Kokoro with
`TTS_ENGINE=kokoro` if you prefer the more natural voice and can tolerate the
delay. Pick the engine via the `TTS_ENGINE` environment variable.

## How it works

```
claude finishes a reply ──────────┐
claude asks for permission/input ─┤   (Notification hook)
claude asks a structured question ┤   (PreToolUse / AskUserQuestion hook)
                                  └─► speak.py ──(localhost socket)──► tts_server.py (daemon, warm)
                                       picks the text for the event       synthesize → speaker
                                       strips markdown / code             ESC interrupts playback
                                       caps length
  SessionStart hook ─► launch_server.py starts the daemon if it isn't running
```

`speak.py` is wired to three events and dispatches on the hook's
`hook_event_name`: **Stop** speaks the finished reply; **Notification** speaks
permission/input prompts; **PreToolUse** (matcher `AskUserQuestion`) reads a
structured question and its options aloud the moment it appears — `Stop` can't,
since it doesn't fire mid-turn.

The daemon loads the model **once** and stays warm, so speech starts quickly
instead of paying a model-load on every reply. The `Stop` hook is a tiny
fire-and-forget client, so it never slows Claude Code down.

## Install

```powershell
./setup.ps1
```

This creates `.venv`, installs dependencies, downloads the Piper voice (default)
and the Kokoro int8 model into `models/`, and prints a hooks snippet. Merge that
snippet into your Claude Code settings (`%USERPROFILE%\.claude\settings.json`),
then start a fresh `claude` session.

> Requires Python 3.10+ on PATH. The download step needs internet **once**;
> after that everything is offline.

## Test before wiring up hooks

1. **Start the daemon in the foreground** (you'll see logs):
   ```powershell
   .\.venv\Scripts\python.exe .\src\tts_server.py
   ```
2. **Send it text** from another terminal:
   ```powershell
   .\.venv\Scripts\python.exe -c "import socket; socket.create_connection(('127.0.0.1',7766)).sendall(b'Hello, this is Claude speaking locally.\n')"
   ```
   You should hear the voice. Press **ESC** to cut it off mid-sentence.
3. **Test the hook end-to-end offline** with the bundled smoke test:
   ```powershell
   .\.venv\Scripts\python.exe .\tests\smoke_test.py
   ```
   It builds a fake transcript (with markdown, a code block, and an over-long
   reply), feeds a synthetic `Stop` payload to `speak.py`, and you hear the
   filtered result.

## Configuration (environment variables)

| Variable             | Default              | Meaning                                              |
|----------------------|----------------------|------------------------------------------------------|
| `TTS_ENGINE`         | `piper`              | `piper` (fast) or `kokoro` (natural)                 |
| `TTS_MUTE`           | `0`                  | `1` = stay silent                                    |
| `TTS_SPEED`          | `1.0`               | Word speed, both engines (`<1` slower, `>1` faster)  |
| `TTS_GAP_MS`         | `0`                 | Extra silence between sentences, ms — wider pauses without slowing words |
| `TTS_MAX_CHARS`      | `10000`             | Cap before "see the terminal for the rest" (`0` = no cap, speak it all) |
| `TTS_BARGE_IN`       | `1`                  | `1` = a new reply interrupts the current one         |
| `TTS_SPEAK_NOTIFICATIONS` | `1`            | Speak permission / input prompts (Notification hook) |
| `TTS_SPEAK_QUESTIONS`| `1`                  | Speak `AskUserQuestion` prompts + their options      |
| `TTS_SPEAK_IDLE`     | `0`                  | `1` = also speak the "waiting for your input" nag     |
| `TTS_PORT`           | `7766`              | Localhost port for the daemon                        |
| `TTS_INTERRUPT_VK`   | `27` (ESC)          | Win32 virtual-key code to interrupt playback         |
| `TTS_PIPER_VOICE`    | `en_US-lessac-medium`| Piper voice model under `models/piper/`              |
| `TTS_PIPER_SPEAKER`  | _(unset)_           | Speaker index for multi-speaker Piper voices (e.g. `16` for libritts_r) |
| `TTS_VOICE`          | `af_heart`          | Kokoro voice id (e.g. `am_adam`, `bf_emma`)          |
| `TTS_KOKORO_THREADS` | `4`                 | Kokoro onnxruntime intra-op threads (tuning)         |
| `TTS_LANG`           | `en-us`             | Language for Kokoro phonemization                    |

The daemon reads these **at launch**, so a change to the engine or voice needs
the daemon to restart or reload. Two ways:

```powershell
# Clean restart (stops any running daemon, persists + applies the change).
# All switches are optional and combinable:
./restart-daemon.ps1 -Voice en_US-amy-medium                 # Piper voice
./restart-daemon.ps1 -Engine kokoro -Voice af_heart          # switch engine + voice
./restart-daemon.ps1 -Voice en_US-libritts_r-medium -Speaker 16   # multi-speaker voice
./restart-daemon.ps1 -Speed 0.95 -Gap 350                    # word speed + sentence pause

# Or reload in place, no process restart, via the socket control verb:
.\.venv\Scripts\python.exe -c "import socket; socket.create_connection(('127.0.0.1',7766)).sendall(b'__RELOAD__ TTS_GAP_MS=350\n')"
```

> Multi-speaker Piper voices (e.g. `en_US-libritts_r-medium`) carry hundreds of
> speakers in one model. Download once, then pick a speaker by its index with
> `-Speaker` / `TTS_PIPER_SPEAKER` — no extra download to switch speakers.

`restart-daemon.ps1` is the reliable option (it also handles the spaces in the
project path and waits for the model to load). `__RELOAD__` re-reads config and
rebuilds the engine instantly; pass inline `TTS_*=value` overrides so the change
applies even though the daemon's launch environment is fixed. To mute on the fly,
send `__MUTE__` / `__UNMUTE__` to the socket.

### About the ESC interrupt key
ESC also clears the input line in the Claude Code TUI. If that bothers you, pick
another key via `TTS_INTERRUPT_VK` — e.g. `19` for Pause/Break or `145` for
Scroll Lock — and restart the daemon.

## Files

| Path                  | Role                                                        |
|-----------------------|-------------------------------------------------------------|
| `src/tts_server.py`   | Warm daemon: socket + interruptible streaming playback      |
| `src/engines.py`      | Pluggable Piper / Kokoro engines (same `stream()` interface)|
| `src/speak.py`        | Hook client for Stop / Notification / AskUserQuestion → filter → socket |
| `src/launch_server.py`| `SessionStart` hook: start the daemon if needed             |
| `src/transcript.py`   | Extract last assistant message from the `.jsonl` transcript |
| `src/text_filter.py`  | Markdown strip, code-block summarizing, length cap          |
| `src/config.py`       | Env-var driven configuration                                |
| `setup.ps1`           | venv + deps + model download + prints hook config           |
| `restart-daemon.ps1`  | Stop + relaunch the daemon (optionally set voice/engine)    |

## Troubleshooting

- **No audio**: check `.state/tts_server.log`. Confirm the daemon is running and
  bound to the port (`__PING__` returns `PONG`).
- **Speech is delayed on the first reply of a session**: the daemon was cold;
  `SessionStart` launches it but model load takes ~1–2 s. Subsequent replies are
  instant.
- **It reads code aloud**: it shouldn't — code fences become "I shared a code
  block." Inline `code` is read as plain words by design.

## License notes

- **Piper** (default engine) is **GPL-3.0**. Fine for personal/local use; if you
  redistribute this project, mind the GPL terms or switch the default to Kokoro.
- **Kokoro** weights are **Apache-2.0** and the `kokoro-onnx` code is **MIT** —
  fully permissive. Set `TTS_ENGINE=kokoro` to use it.
