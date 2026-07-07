# The Voice of Claude

Give Claude Code a **local voice**. When Claude finishes a response, a local
text-to-speech engine speaks a cleaned-up version aloud. A floating
**karaoke-style overlay** highlights the sentence being spoken in real time.
**Nothing leaves your PC — no cloud.**

Voice *input* is already handled by Claude Code's built-in dictation (hold
spacebar). This project adds the missing half: voice *output*.

## Features

- **Streaming audio** — synthesis and playback overlap so speech starts on the
  first sentence while the rest is being generated.
- **Karaoke overlay** — a borderless always-on-top window shows the full reply
  and highlights each sentence as it is spoken, auto-scrolled to the middle of
  the view (mouse wheel scrolls manually). Draggable, resizable via the ◢ grip,
  and it remembers its size and position across runs — on any monitor. Press
  **ESC** to stop playback and dismiss, or use the ✕ button.
- **Transport controls** — pause/resume, skip forward/back a sentence, and
  **click any sentence in the overlay to jump straight to it**. Global hotkeys
  (work whatever window has focus): `Ctrl+Alt+Space` pause/resume,
  `Ctrl+Alt+→` / `Ctrl+Alt+←` skip, `ESC` stop. The overlay has matching
  ⏮ ⏯ ⏭ buttons.
- **Progress indicator** — a thin bar along the overlay's bottom edge plus a
  "sentence 4 of 12 · ~35s left" readout. Sentence durations are exact for
  synthesized audio; the not-yet-synthesized remainder is estimated from the
  speaking rate observed so far.
- **Attention chime** — a short sound plays when Claude needs your input
  (permission prompt or `AskUserQuestion`), instead of interrupting the current
  reply.
- **Text normalisation** — numbers, units, decimals, ordinals, abbreviations
  and currency are converted to natural spoken form before synthesis
  (`15,200kg` → *fifteen thousand two hundred kilograms*; `3.14` → *three
  point one four*; `1.62 m/s` → *one point six two metres per second*).
- **GPU acceleration** — Kokoro runs on CUDA or DirectML when available; falls
  back to CPU transparently.
- **Self-healing daemon** — the `Stop` hook checks whether the daemon and
  overlay are running and restarts them if not, so a crashed process recovers
  automatically on the next reply.
- **ESC interrupt** — stops playback instantly, polled globally so it works
  regardless of which window has focus.

## Two TTS engines

| Engine | Speed on CPU (i7-1265U) | Voice quality | License |
|--------|--------------------------|---------------|---------|
| **[Piper](https://github.com/OHF-Voice/piper1-gpl)** (default) | ~2× real-time — **low latency** | Good, slightly synthetic | GPL-3.0 |
| **[Kokoro](https://github.com/thewh1teagle/kokoro-onnx)** | ~0.27× real-time on CPU; near-instant on GPU | More natural | Apache-2.0 / MIT |

Piper is the default for CPU users. Switch to Kokoro with `TTS_ENGINE=kokoro`
if you have a GPU or prefer the more natural voice. On an NVIDIA GPU, Kokoro
matches Piper's latency while sounding noticeably better.

## How it works

```
claude finishes a reply ──────────────┐
                                      └─► Stop hook ──► launch_server.py (self-heal)
                                                    └──► speak.py ──(socket)──► tts_server.py
                                                                                  ├─ normalise text
                                                                                  ├─ synthesise chunks
                                                                                  ├─ stream to speaker
                                                                                  └─► overlay.py (highlight)

claude asks for permission / input ───► Notification hook ──► bell.py (chime)
claude asks a structured question  ───► PreToolUse hook   ──► bell.py (chime)

SessionStart / Stop hook ─► launch_server.py ─► starts daemon + overlay if not running
```

The daemon loads the model **once** and stays warm, so speech starts quickly
on every reply. A producer-consumer pipeline overlaps synthesis and playback —
by the time the first chunk finishes playing, the second is already synthesised.

## Install

```powershell
./setup.ps1
```

Creates `.venv`, installs dependencies, downloads the Piper voice and the
Kokoro int8 model into `models/`, and prints a hooks snippet. Merge that
snippet into `%USERPROFILE%\.claude\settings.json`, then start a fresh
`claude` session.

> Requires Python 3.10+ on PATH. Download needs internet once; after that
> everything is offline.

### Recommended settings.json hooks

```json
{
  "hooks": {
    "SessionStart": [
      { "type": "command", "command": "\"<repo>\\.venv\\Scripts\\pythonw.exe\" \"<repo>\\src\\launch_server.py\"", "timeout": 15 }
    ],
    "Stop": [
      { "type": "command", "command": "\"<repo>\\.venv\\Scripts\\pythonw.exe\" \"<repo>\\src\\launch_server.py\"", "timeout": 15 },
      { "type": "command", "command": "\"<repo>\\.venv\\Scripts\\python.exe\" \"<repo>\\src\\speak.py\"", "timeout": 15 }
    ],
    "Notification": [
      { "type": "command", "command": "\"<repo>\\.venv\\Scripts\\python.exe\" \"<repo>\\src\\bell.py\"", "timeout": 5 }
    ],
    "PreToolUse": [
      { "matcher": "AskUserQuestion", "type": "command", "command": "\"<repo>\\.venv\\Scripts\\python.exe\" \"<repo>\\src\\bell.py\"", "timeout": 5 }
    ]
  },
  "env": {
    "TTS_ENGINE": "kokoro",
    "TTS_DEVICE": "cuda",
    "TTS_VOICE": "af_sarah",
    "TTS_BELL_SOUND": "<repo>\\sounds\\chime.wav"
  }
}
```

Replace `<repo>` with the absolute path to your clone. The `Stop` hook runs
`launch_server.py` first so a crashed daemon/overlay is automatically restarted
before the reply is spoken.

## Test before wiring up hooks

1. **Start the daemon** (you'll see logs):
   ```powershell
   .\.venv\Scripts\python.exe .\src\tts_server.py
   ```
2. **Send it text** from another terminal:
   ```powershell
   .\.venv\Scripts\python.exe -c "import socket; socket.create_connection(('127.0.0.1',7766)).sendall(b'Hello from Claude.\n')"
   ```
3. **Start the overlay** (optional):
   ```powershell
   .\.venv\Scripts\pythonw.exe .\src\overlay.py
   ```
4. **Run the smoke test**:
   ```powershell
   .\.venv\Scripts\python.exe .\tests\smoke_test.py
   ```

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `TTS_ENGINE` | `piper` | `piper` (fast) or `kokoro` (natural) |
| `TTS_DEVICE` | `auto` | `auto`, `cpu`, `cuda`, `dml` — GPU provider for onnxruntime |
| `TTS_VOICE` | `af_heart` | Kokoro voice id (`af_sarah`, `am_adam`, `bf_emma`, …) |
| `TTS_PIPER_VOICE` | `en_US-lessac-medium` | Piper voice model under `models/piper/` |
| `TTS_PIPER_SPEAKER` | _(unset)_ | Speaker index for multi-speaker Piper voices |
| `TTS_SPEED` | `1.0` | Playback speed (`<1` slower, `>1` faster) |
| `TTS_GAP_MS` | `0` | Extra silence between sentences (ms) |
| `TTS_MAX_CHARS` | `10000` | Cap reply length before truncating (`0` = no cap) |
| `TTS_MUTE` | `0` | `1` = stay silent |
| `TTS_BARGE_IN` | `1` | `1` = new reply interrupts the current one |
| `TTS_SPEAK_NOTIFICATIONS` | `1` | `0` = silence Notification hook |
| `TTS_SPEAK_QUESTIONS` | `1` | `0` = silence AskUserQuestion hook |
| `TTS_SPEAK_IDLE` | `0` | `1` = also speak the "waiting for input" nag |
| `TTS_PORT` | `7766` | Daemon localhost port |
| `TTS_INTERRUPT_VK` | `27` (ESC) | Win32 virtual-key code to interrupt playback |
| `TTS_KOKORO_THREADS` | `4` | Kokoro onnxruntime intra-op threads (CPU tuning) |
| `TTS_LANG` | `en-us` | Language for Kokoro phonemisation |
| `TTS_OVERLAY` | `1` | `0` = disable the karaoke overlay |
| `TTS_OVERLAY_PORT` | `7767` | Overlay localhost port |
| `TTS_BELL_SOUND` | _(unset)_ | Path to a `.wav` file for the attention chime |

Changes to engine, voice or device need a daemon restart:

```powershell
# Restart via the helper script
./restart-daemon.ps1 -Engine kokoro -Voice af_sarah

# Or reload config in place (no process restart):
.\.venv\Scripts\python.exe -c "import socket; socket.create_connection(('127.0.0.1',7766)).sendall(b'__RELOAD__ TTS_VOICE=af_sarah\n')"
```

### GPU acceleration

The default `onnxruntime` package is CPU-only. To enable a GPU:

```powershell
# NVIDIA CUDA (accelerates Piper and Kokoro). The [cuda,cudnn] extras pull the
# CUDA runtime + cuDNN as pip wheels, so no system-wide CUDA toolkit is needed:
.\.venv\Scripts\python.exe -m pip uninstall -y onnxruntime
.\.venv\Scripts\python.exe -m pip install "onnxruntime-gpu[cuda,cudnn]"

# Any DirectX 12 GPU — Intel / AMD (Kokoro only, Piper has no DML path):
.\.venv\Scripts\python.exe -m pip uninstall -y onnxruntime
.\.venv\Scripts\python.exe -m pip install onnxruntime-directml
```

The engine loader calls `onnxruntime.preload_dlls()` at startup so the wheels'
CUDA/cuDNN DLLs are found without touching PATH. If `nvidia-smi` works but the
log still says `device: cpu`, check that only ONE onnxruntime package is
installed — a plain `onnxruntime` sitting next to `onnxruntime-gpu` silently
shadows the GPU build.

`onnxruntime`, `onnxruntime-gpu`, and `onnxruntime-directml` are mutually
exclusive — install exactly one. Set `TTS_DEVICE=cuda` or `TTS_DEVICE=dml`
then restart the daemon. The active device is logged at startup:
`Engine 'kokoro' ready (device: cuda)`.

### Transport controls

While a reply is being spoken:

| Action | Global hotkey | Overlay | Socket command |
|---|---|---|---|
| Stop & dismiss | `ESC` | ✕ button | `__STOP__` |
| Pause / resume | `Ctrl+Alt+Space` | ⏯ button | `__PAUSE__` |
| Next sentence | `Ctrl+Alt+→` | ⏭ button | `__NEXT__` |
| Previous sentence | `Ctrl+Alt+←` | ⏮ button | `__PREV__` |
| Jump anywhere | — | click a sentence | `__SEEK__ <char offset>` |

Hotkeys are polled globally (Win32), so they work no matter which window has
focus. Pausing freezes mid-word and resumes exactly where it stopped; skipping
past the last sentence simply ends playback. Seeking to a sentence that hasn't
been synthesised yet waits for it, then jumps.

### About the ESC interrupt key

ESC also clears the Claude Code input line. To use a different key, set
`TTS_INTERRUPT_VK` — e.g. `19` (Pause/Break) or `145` (Scroll Lock) — and
restart the daemon. The transport hotkeys need `Ctrl+Alt` precisely so they
never hijack normal typing.

## Files

| Path | Role |
|------|------|
| `src/tts_server.py` | Warm daemon: socket server, streaming producer-consumer playback |
| `src/engines.py` | Pluggable Piper / Kokoro engines (`stream()` → `(samples, sr, chunk)`) |
| `src/normalizer.py` | Text normalisation: numbers, units, decimals, ordinals, abbreviations |
| `src/overlay.py` | Karaoke overlay: sentence highlighting, transport buttons, click-to-seek |
| `src/speak.py` | Stop / Notification / AskUserQuestion hook client → daemon socket |
| `src/bell.py` | Attention chime: plays `TTS_BELL_SOUND` WAV or system beep |
| `src/launch_server.py` | SessionStart / Stop hook: start daemon + overlay if not running |
| `src/transcript.py` | Extract last assistant message from the `.jsonl` transcript |
| `src/text_filter.py` | Markdown strip, em-dash normalisation, code-block summarising, length cap |
| `src/config.py` | Env-var driven configuration |
| `sounds/chime.wav` | Default attention chime (replace with any WAV via `TTS_BELL_SOUND`) |
| `setup.ps1` | venv + deps + model download + prints hook config |
| `restart-daemon.ps1` | Stop + relaunch daemon (optionally change engine/voice/speed/gap) |

## Troubleshooting

- **No audio or overlay**: check `.state/tts_server.log`. Send `__PING__` to
  port 7766 — it should return `PONG`. If not, run `launch_server.py` manually.
- **Delayed first reply**: normal on cold start — the model loads in ~3 s. The
  `Stop` hook self-heals a crashed daemon so you rarely need to restart manually.
- **Duplicate daemon processes**: harmless — the named-mutex singleton guard
  ensures only one instance serves the port; extras exit immediately.
- **Numbers sound wrong**: the normaliser runs before synthesis. Check
  `src/normalizer.py` if a specific pattern isn't converted.
- **It reads code aloud**: it shouldn't — code fences become *"I shared a code
  block."* Inline `code` is read as plain words by design.

## License notes

- **Piper** is **GPL-3.0**. Fine for personal/local use; mind the GPL terms if
  you redistribute.
- **Kokoro** weights are **Apache-2.0** and `kokoro-onnx` is **MIT** —
  fully permissive.
