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
import time
from pathlib import Path

import config


def _pid_alive(pid: int) -> bool:
    """Return True if the process with this PID is currently running."""
    if os.name == "nt":
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if handle:
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _daemon_alive() -> bool:
    # Check PID file first — written by tts_server.py as soon as it acquires
    # the singleton, before it binds the port. This prevents spawning a
    # duplicate while the model is loading (~3 s before the port is ready).
    pid_file = config.STATE_DIR / "tts_server.pid"
    try:
        pid = int(pid_file.read_text().strip())
        if _pid_alive(pid):
            return True
    except Exception:
        pass
    # Fallback: port ping (covers the case where the PID file is stale/missing)
    try:
        with socket.create_connection((config.HOST, config.PORT), timeout=0.4) as s:
            s.sendall((config.CTRL_PING + "\n").encode("utf-8"))
            s.settimeout(0.6)
            return s.recv(16).strip() == b"PONG"
    except OSError:
        return False


def _spawn(script: Path) -> None:
    exe = Path(sys.executable)
    pythonw = exe.with_name("pythonw.exe")
    interpreter = str(pythonw if pythonw.exists() else exe)

    creationflags = 0
    if os.name == "nt":
        # DETACHED_PROCESS | CREATE_NO_WINDOW
        creationflags = 0x00000008 | 0x08000000

    subprocess.Popen(
        [interpreter, str(script)],
        cwd=str(script.parent.parent),
        creationflags=creationflags,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )


def _overlay_alive() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", config.OVERLAY_PORT), timeout=0.4):
            return True
    except OSError:
        return False


def _launch() -> None:
    src = Path(__file__).resolve().parent
    _spawn(src / "tts_server.py")
    if config.OVERLAY and not _overlay_alive():
        _spawn(src / "overlay.py")


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
