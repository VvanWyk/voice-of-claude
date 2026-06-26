"""Persistent local TTS daemon for Claude Code voice output.

Loads the Kokoro ONNX model ONCE and listens on a localhost socket for text to
speak. Keeping the model warm is what makes speech start ~instantly instead of
paying a 1-2s model load on every reply.

Protocol: newline-delimited UTF-8 over TCP on 127.0.0.1:<TTS_PORT>.
  - any normal line  -> speak it
  - __STOP__         -> stop current playback, clear the queue
  - __MUTE__ / __UNMUTE__
  - __PING__         -> health check (used by launch_server.py)
  - __RELOAD__ [TTS_KEY=value ...] -> re-read config + rebuild engine in place

Interrupt playback at any time with the configured key (default ESC), which is
polled globally via the Win32 API so it works regardless of which process owns
the terminal.

Run directly to test in the foreground:  python src/tts_server.py
"""
from __future__ import annotations

import ctypes
import importlib
import logging
import os
import queue
import socket
import sys
import threading
import time

import config

# Heavy modules (sounddevice pulls in PortAudio, engines pull in numpy/onnx) are
# imported lazily AFTER the single-instance guard in main(), so a duplicate
# launch exits in milliseconds instead of spending 1-2s importing first.
sd = None  # set by _load_audio() once this process wins the singleton lock

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[],
)
log = logging.getLogger("tts_server")


def _setup_logging() -> None:
    config.STATE_DIR.mkdir(parents=True, exist_ok=True)
    fh = logging.FileHandler(config.STATE_DIR / "tts_server.log", encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(fh)
    if sys.stdout and sys.stdout.isatty():
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        log.addHandler(sh)


# Kept alive for the whole process: releasing it (or the process dying) frees
# the lock for the next launch. Module-level so it is never garbage-collected.
_singleton_handle = None


def _acquire_singleton() -> bool:
    """Race-free single-instance guard via a Windows named mutex.

    The first daemon to call this owns the name; any later launch sees
    ERROR_ALREADY_EXISTS and returns False so it can exit immediately - before
    binding the port or loading the model. Non-Windows falls back to the bind()
    guard in serve(). Keyed by port so a custom TTS_PORT gets its own instance.
    """
    global _singleton_handle
    if os.name != "nt":
        return True
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateMutexW.restype = wintypes.HANDLE
    k32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
    handle = k32.CreateMutexW(None, True, f"voice-of-claude-tts-{config.PORT}")
    ERROR_ALREADY_EXISTS = 183
    if not handle or ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        return False
    _singleton_handle = handle
    return True


def _load_audio() -> None:
    """Import sounddevice once (after the singleton guard)."""
    global sd
    if sd is None:
        import sounddevice
        sd = sounddevice


class TTSDaemon:
    def __init__(self) -> None:
        self.speak_q: "queue.Queue[str]" = queue.Queue()
        self.interrupt = threading.Event()
        self.shutdown = threading.Event()
        self.muted = config.MUTE
        self.engine = None

    # --- model -------------------------------------------------------------
    def load_model(self) -> None:
        import engines

        log.info("Loading TTS engine (requested: %s) ...", config.ENGINE)
        t0 = time.time()
        try:
            self.engine = engines.load_engine()
        except Exception as e:
            log.error("Could not load any TTS engine: %s. Run setup.ps1.", e)
            raise SystemExit(2)
        log.info(
            "Engine '%s' ready in %.1fs (device: %s)",
            self.engine.name, time.time() - t0, getattr(self.engine, "device", "cpu"),
        )

    def reload(self, args: str) -> bool:
        """Re-read config and rebuild the engine in place (no process restart).

        `args` may carry inline `TTS_*=value` overrides applied before reload,
        so a voice change takes effect without the daemon's launch environment.
        Returns True on success.
        """
        for token in args.split():
            key, _, value = token.partition("=")
            if key.startswith("TTS_") and value:
                os.environ[key] = value
        import engines

        self.stop_now()  # silence current playback before swapping the engine
        try:
            importlib.reload(config)
            self.muted = config.MUTE
            self.engine = engines.load_engine()
        except Exception as e:
            log.error("Reload failed: %s", e)
            return False
        log.info(
            "Reloaded: engine='%s' piper_voice=%s kokoro_voice=%s speed=%s",
            self.engine.name, config.PIPER_VOICE, config.VOICE, config.SPEED,
        )
        return True

    # --- queue control -----------------------------------------------------
    def _drain(self) -> None:
        try:
            while True:
                self.speak_q.get_nowait()
        except queue.Empty:
            pass

    def stop_now(self) -> None:
        self.interrupt.set()
        self._drain()

    def enqueue(self, text: str) -> None:
        if config.BARGE_IN:
            self.stop_now()  # latest reply wins
        self.speak_q.put(text)

    # --- worker ------------------------------------------------------------
    def worker(self) -> None:
        while not self.shutdown.is_set():
            try:
                text = self.speak_q.get(timeout=0.2)
            except queue.Empty:
                continue
            if text is None:
                break
            if self.muted:
                continue
            self.interrupt.clear()
            self._speak(text)

    def _speak(self, text: str) -> None:
        gap = max(0, config.GAP_MS) / 1000.0
        try:
            first = True
            for samples, sr in self.engine.stream(text):
                if self.interrupt.is_set() or self.shutdown.is_set():
                    return
                if gap and not first:
                    self._pause(gap)  # widen the pause between sentences
                    if self.interrupt.is_set() or self.shutdown.is_set():
                        return
                self._play(samples, sr)
                first = False
        except Exception as e:  # synthesis must never crash the daemon
            log.warning("Synthesis failed: %s", e)

    def _pause(self, seconds: float) -> None:
        """Silent, interruptible delay between chunks."""
        end = time.time() + seconds
        while time.time() < end:
            if self.interrupt.is_set() or self.shutdown.is_set():
                return
            time.sleep(0.02)

    def _play(self, samples, sr) -> None:
        try:
            sd.play(samples, sr)
        except Exception as e:
            log.warning("Playback failed: %s", e)
            return
        while True:
            if self.interrupt.is_set() or self.shutdown.is_set():
                sd.stop()
                return
            stream = sd.get_stream()
            if stream is None or not stream.active:
                return
            time.sleep(0.02)

    # --- interrupt key poll ------------------------------------------------
    def key_poller(self) -> None:
        try:
            user32 = ctypes.windll.user32
        except AttributeError:
            log.info("Key polling unavailable on this platform; skipping.")
            return
        was_down = False
        while not self.shutdown.is_set():
            down = bool(user32.GetAsyncKeyState(config.INTERRUPT_VK) & 0x8000)
            if down and not was_down:
                self.stop_now()
                log.info("Interrupt key pressed -> playback stopped")
            was_down = down
            time.sleep(0.03)

    # --- socket server -----------------------------------------------------
    def bind(self) -> socket.socket:
        """Bind the listening port. Secondary single-instance guard.

        The named-mutex guard in main() is the primary defence against a
        duplicate daemon (and the only thing that helps on non-Windows is this).
        We deliberately do NOT set SO_REUSEADDR: on Windows it lets multiple
        processes bind the same port, which would defeat the guard. Without it,
        a second bind fails and that process exits.
        """
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            srv.bind((config.HOST, config.PORT))
        except OSError:
            log.error("Port %s busy - another daemon is already running. Exiting.", config.PORT)
            raise SystemExit(0)
        srv.listen(8)
        srv.settimeout(0.5)
        return srv

    def serve(self, srv: socket.socket) -> None:
        log.info("Listening on %s:%s", config.HOST, config.PORT)

        while not self.shutdown.is_set():
            try:
                conn, _ = srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._handle_conn, args=(conn,), daemon=True).start()
        srv.close()

    def _handle_conn(self, conn: socket.socket) -> None:
        # Process each complete (newline-terminated) line as soon as it arrives
        # so health checks like __PING__ get an immediate reply.
        with conn:
            conn.settimeout(2.0)
            buf = b""
            try:
                while not self.shutdown.is_set():
                    try:
                        chunk = conn.recv(4096)
                    except socket.timeout:
                        break
                    if not chunk:
                        break
                    buf += chunk
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        self._handle_line(line.decode("utf-8", errors="replace"), conn)
            except OSError:
                pass

    def _handle_line(self, line: str, conn: socket.socket) -> None:
        line = line.strip()
        if line:
            if line == config.CTRL_PING:
                try:
                    conn.sendall(b"PONG\n")
                except OSError:
                    pass
            elif line == config.CTRL_RELOAD or line.startswith(config.CTRL_RELOAD + " "):
                ok = self.reload(line[len(config.CTRL_RELOAD):].strip())
                try:
                    conn.sendall(b"RELOADED\n" if ok else b"RELOAD_FAILED\n")
                except OSError:
                    pass
            elif line == config.CTRL_STOP:
                self.stop_now()
            elif line == config.CTRL_MUTE:
                self.muted = True
                self.stop_now()
                log.info("Muted")
            elif line == config.CTRL_UNMUTE:
                self.muted = False
                log.info("Unmuted")
            else:
                log.info("Speak (%d chars): %s", len(line), line[:80])
                self.enqueue(line)

    # --- lifecycle ---------------------------------------------------------
    def run(self) -> None:
        # The named-mutex guard in main() already made us the single instance;
        # bind() is a secondary guard (and the only one on non-Windows).
        srv = self.bind()
        _load_audio()
        self.load_model()
        threading.Thread(target=self.worker, daemon=True).start()
        threading.Thread(target=self.key_poller, daemon=True).start()
        try:
            self.serve(srv)
        except KeyboardInterrupt:
            pass
        finally:
            self.shutdown.set()
            sd.stop()
            log.info("Daemon stopped.")


def main() -> None:
    _setup_logging()
    if not _acquire_singleton():
        log.info("Another TTS daemon already owns port %s; exiting.", config.PORT)
        return
    TTSDaemon().run()


if __name__ == "__main__":
    main()
