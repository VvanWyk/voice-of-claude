"""Play a short attention chime for Notification and PreToolUse hooks.

Configure the sound file via the TTS_BELL_SOUND environment variable:
    TTS_BELL_SOUND=C:\\path\\to\\chime.wav

Supports WAV files (via winsound, built-in) and MP3/other formats
(via sounddevice + soundfile, if installed). Falls back to the Windows
system Asterisk sound if no file is configured or the file is missing.
"""
from __future__ import annotations

import os
import sys


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


def main() -> int:
    try:
        sys.stdin.read()
    except Exception:
        pass
    try:
        play()
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
