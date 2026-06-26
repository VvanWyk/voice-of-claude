"""Claude Code `SessionStart` hook: ensure the TTS daemon is running.

Pings the daemon port; if nothing answers, launches tts_server.py detached (no
console window) using the same interpreter that ran this script. Returns
immediately so it never delays session startup.
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
from pathlib import Path

import config


def _daemon_alive() -> bool:
    try:
        with socket.create_connection((config.HOST, config.PORT), timeout=0.4) as s:
            s.sendall((config.CTRL_PING + "\n").encode("utf-8"))
            s.settimeout(0.6)
            return s.recv(16).strip() == b"PONG"
    except OSError:
        return False


def _launch() -> None:
    server = Path(__file__).resolve().parent / "tts_server.py"

    # Prefer pythonw.exe (no console window) when available next to python.exe.
    exe = Path(sys.executable)
    pythonw = exe.with_name("pythonw.exe")
    interpreter = str(pythonw if pythonw.exists() else exe)

    creationflags = 0
    if os.name == "nt":
        # DETACHED_PROCESS | CREATE_NO_WINDOW
        creationflags = 0x00000008 | 0x08000000

    subprocess.Popen(
        [interpreter, str(server)],
        cwd=str(server.parent.parent),
        creationflags=creationflags,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )


def main() -> int:
    # Drain stdin (SessionStart sends a JSON payload we don't need).
    try:
        sys.stdin.read()
    except Exception:
        pass

    if not _daemon_alive():
        try:
            _launch()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
