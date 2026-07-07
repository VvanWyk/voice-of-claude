"""System tray icon for The Voice of Claude.

Menu: mute toggle, Kokoro voice switcher (radio list, applied live via the
daemon's __RELOAD__ verb), stop-speaking, and quit. The icon shows a speaker,
with a red slash while muted.

Started alongside the daemon by launch_server.py. Single-instance is enforced
by binding TTS_TRAY_PORT (default 7768) — the port carries no protocol, it is
only a lock. Set TTS_TRAY=0 to disable.
"""
from __future__ import annotations

import os
import socket
import sys
import threading

import config

HOST = "127.0.0.1"

# Kokoro voice styles bundled in voices-v1.0.bin (a=American, b=British).
VOICES = [
    "af_heart", "af_sarah", "af_bella", "af_nicole", "af_sky",
    "am_adam", "am_michael",
    "bf_emma", "bf_isabella",
    "bm_george", "bm_lewis",
]

_guard = None  # keeps the singleton port bound for the process lifetime


def _acquire_singleton() -> bool:
    global _guard
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    # No SO_REUSEADDR — on Windows it would defeat the single-instance guard.
    try:
        srv.bind((HOST, config.TRAY_PORT))
    except OSError:
        return False
    srv.listen(4)
    _guard = srv

    def _drain() -> None:
        # Accept and close launch_server's liveness probes; a backlog of
        # never-accepted connections would make the probe unreliable.
        while True:
            try:
                conn, _ = srv.accept()
                conn.close()
            except OSError:
                return

    threading.Thread(target=_drain, daemon=True).start()
    return True


def _send_daemon(cmd: str) -> None:
    """Fire a control verb at the daemon; never block the menu thread."""
    def _worker() -> None:
        try:
            with socket.create_connection((config.HOST, config.PORT), timeout=3) as s:
                s.sendall((cmd + "\n").encode("utf-8"))
        except OSError:
            pass  # daemon down — the Stop hook will revive it
    threading.Thread(target=_worker, daemon=True).start()


def _make_image(muted: bool):
    """Draw the speaker icon: amber ring, speaker glyph, arcs or mute slash."""
    from PIL import Image, ImageDraw

    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse([2, 2, 62, 62], fill=(26, 27, 38, 255),
              outline=(224, 175, 104, 255), width=3)
    d.polygon([(16, 26), (25, 26), (35, 16), (35, 48), (25, 38), (16, 38)],
              fill=(192, 202, 245, 255))
    if muted:
        d.line([(14, 50), (50, 14)], fill=(247, 118, 142, 255), width=6)
    else:
        d.arc([37, 23, 49, 41], start=-55, end=55,
              fill=(224, 175, 104, 255), width=3)
        d.arc([41, 16, 57, 48], start=-55, end=55,
              fill=(224, 175, 104, 255), width=3)
    return img


class Tray:
    def __init__(self) -> None:
        self.muted = config.MUTE
        self.voice = config.VOICE
        self.icon = None

    # -- menu actions ---------------------------------------------------------
    def _toggle_mute(self, icon, item) -> None:
        self.muted = not self.muted
        _send_daemon(config.CTRL_MUTE if self.muted else config.CTRL_UNMUTE)
        icon.icon = _make_image(self.muted)

    def _voice_setter(self, voice: str):
        def _set(icon, item) -> None:
            self.voice = voice
            # Rebuilds the engine in place; takes a few seconds, done async.
            _send_daemon(f"{config.CTRL_RELOAD} TTS_VOICE={voice}")
        return _set

    def _voice_checked(self, voice: str):
        return lambda item: self.voice == voice

    def _stop(self, icon, item) -> None:
        _send_daemon(config.CTRL_STOP)

    def _quit(self, icon, item) -> None:
        icon.stop()

    # -- lifecycle ------------------------------------------------------------
    def run(self) -> None:
        import pystray
        from pystray import Menu, MenuItem as Item

        voice_menu = Menu(*[
            Item(v, self._voice_setter(v), radio=True,
                 checked=self._voice_checked(v))
            for v in VOICES
        ])
        menu = Menu(
            Item("Mute", self._toggle_mute,
                 checked=lambda item: self.muted),
            Item("Voice", voice_menu),
            Item("Stop speaking", self._stop),
            Menu.SEPARATOR,
            Item("Quit tray", self._quit),
        )
        self.icon = pystray.Icon(
            "voice-of-claude", _make_image(self.muted),
            "The Voice of Claude", menu,
        )
        self.icon.run()


def main() -> None:
    if not config.TRAY:
        return
    if not _acquire_singleton():
        return  # another tray already running
    try:
        Tray().run()
    except Exception:
        # pythonw has no stderr — leave a trace for debugging.
        import traceback
        try:
            config.STATE_DIR.mkdir(parents=True, exist_ok=True)
            (config.STATE_DIR / "tray.log").write_text(
                traceback.format_exc(), encoding="utf-8"
            )
        except OSError:
            pass


if __name__ == "__main__":
    main()
