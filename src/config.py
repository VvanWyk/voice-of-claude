"""Shared configuration for the Claude Code local voice (TTS) feature.

Everything is driven by environment variables so behaviour can be tweaked
without editing code. Both the daemon (tts_server.py) and the hook client
(speak.py) import this module.
"""
from __future__ import annotations

import os
from pathlib import Path

# --- Paths -----------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent
MODELS_DIR = PROJECT_ROOT / "models"
MODEL_PATH = MODELS_DIR / "kokoro-v1.0.int8.onnx"   # Kokoro int8 weights
VOICES_PATH = MODELS_DIR / "voices-v1.0.bin"        # Kokoro voice styles
PIPER_DIR = MODELS_DIR / "piper"
STATE_DIR = PROJECT_ROOT / ".state"  # last-spoken tracking, daemon log


def _env(name: str, default: str) -> str:
    val = os.environ.get(name)
    return val if val not in (None, "") else default


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(_env(name, str(default)))
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    return _env(name, "1" if default else "0").strip().lower() in ("1", "true", "yes", "on")


# --- Network ---------------------------------------------------------------
HOST = "127.0.0.1"
PORT = _env_int("TTS_PORT", 7766)

# --- Engine selection ------------------------------------------------------
# "piper"  -> fast, low latency (DEFAULT; ~0.5x real-time on an i7-1265U)
# "kokoro" -> more natural, but ~3.7x real-time on the same CPU
ENGINE = _env("TTS_ENGINE", "piper").lower()

# --- Voice / synthesis -----------------------------------------------------
SPEED = _env_float("TTS_SPEED", 1.0)
LANG = _env("TTS_LANG", "en-us")

# Kokoro voice style + thread count (tuning matters on hybrid P/E-core CPUs).
VOICE = _env("TTS_VOICE", "af_heart")
KOKORO_THREADS = _env_int("TTS_KOKORO_THREADS", 4)

# Piper voice model name (file <name>.onnx under models/piper/).
PIPER_VOICE = _env("TTS_PIPER_VOICE", "en_US-lessac-medium")

# Speaker index for multi-speaker Piper voices (e.g. en_US-libritts_r-medium).
# Leave unset for single-speaker voices. This is the integer index from the
# voice's speaker_id_map, e.g. 16 = LibriTTS speaker "2769".
_piper_speaker = _env("TTS_PIPER_SPEAKER", "").strip()
PIPER_SPEAKER = int(_piper_speaker) if _piper_speaker.lstrip("-").isdigit() else None


def piper_model_path() -> Path:
    return PIPER_DIR / f"{PIPER_VOICE}.onnx"

# --- Behaviour -------------------------------------------------------------
MUTE = _env_bool("TTS_MUTE", False)
# Cap on how much of a reply is spoken before "see the terminal for the rest".
# Set to 0 (or any value <= 0) to disable the cap and speak the whole reply.
MAX_CHARS = _env_int("TTS_MAX_CHARS", 10000)
# Extra silence (milliseconds) inserted between spoken chunks - i.e. at sentence
# boundaries for Piper. Widens the pauses WITHOUT slowing the words (which is
# what TTS_SPEED does). 0 = no added gap.
GAP_MS = _env_int("TTS_GAP_MS", 0)
# If True, a new response interrupts whatever is currently being spoken
# (latest reply wins). If False, responses queue up.
BARGE_IN = _env_bool("TTS_BARGE_IN", True)

# Virtual-key code polled to interrupt playback. Default 0x1B = ESC.
# Common alternatives: 0x13 = PAUSE/Break, 0x91 = ScrollLock.
INTERRUPT_VK = _env_int("TTS_INTERRUPT_VK", 0x1B)

# --- Which events to speak --------------------------------------------------
# Stop (the finished reply) is always spoken. These add the other moments where
# Claude is waiting on you, so a voice-only workflow doesn't miss them.
#   Notifications: permission prompts, MCP input dialogs, etc.
#   Questions:     the AskUserQuestion tool (structured multiple-choice asks).
SPEAK_NOTIFICATIONS = _env_bool("TTS_SPEAK_NOTIFICATIONS", True)
SPEAK_QUESTIONS = _env_bool("TTS_SPEAK_QUESTIONS", True)
# "idle_prompt" fires right after Stop ("waiting for your input"), so it is
# redundant with the spoken reply; off by default. Flip on if you want it.
SPEAK_IDLE = _env_bool("TTS_SPEAK_IDLE", False)

# Control verbs sent over the socket (newline-delimited UTF-8 protocol).
CTRL_STOP = "__STOP__"
CTRL_MUTE = "__MUTE__"
CTRL_UNMUTE = "__UNMUTE__"
CTRL_PING = "__PING__"
# __RELOAD__ re-reads config and rebuilds the engine in place (no process
# restart). Optional inline overrides apply first, e.g.
#   __RELOAD__ TTS_PIPER_VOICE=en_US-amy-medium TTS_SPEED=1.1
CTRL_RELOAD = "__RELOAD__"
