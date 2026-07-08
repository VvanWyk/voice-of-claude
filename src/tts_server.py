"""Persistent local TTS daemon for Claude Code voice output.

Loads the Kokoro ONNX model ONCE and listens on a localhost socket for text to
speak. Keeping the model warm is what makes speech start ~instantly instead of
paying a 1-2s model load on every reply.

Protocol: newline-delimited UTF-8 over TCP on 127.0.0.1:<TTS_PORT>.
  - any normal line  -> speak it
  - __STOP__         -> stop current playback, clear the queue
  - __PAUSE__        -> toggle pause / resume
  - __NEXT__ / __PREV__ -> skip forward / back one sentence chunk
  - __SEEK__ <n>     -> jump to the chunk containing char offset <n>
  - __MUTE__ / __UNMUTE__
  - __PING__         -> health check (used by launch_server.py)
  - __HISTORY__      -> JSON list of the last 10 spoken replies (newest first)
  - __SAY__ <n>      -> re-speak history item n (0 = latest)
  - __EXPORT__ <n>   -> synthesise history item n to a WAV; replies with path
  - __RELOAD__ [TTS_KEY=value ...] -> re-read config + rebuild engine in place

Global hotkeys (polled via Win32, work whatever window has focus):
  ESC stop · Ctrl+Alt+Space pause/resume · Ctrl+Alt+Right/Left skip ·
  Ctrl+Alt+R replay last reply.

Run directly to test in the foreground:  python src/tts_server.py
"""
from __future__ import annotations

import base64
import ctypes
import importlib
import json
import logging
import math
import os
import queue
import socket
import sys
import threading
import time
from collections import deque
from pathlib import Path

import config

# Heavy modules (sounddevice pulls in PortAudio, engines pull in numpy/onnx) are
# imported lazily AFTER the single-instance guard in main(), so a duplicate
# launch exits in milliseconds instead of spending 1-2s importing first.
sd = None  # set by _load_audio() once this process wins the singleton lock
np = None

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
    """Import sounddevice + numpy once (after the singleton guard)."""
    global sd, np
    if sd is None:
        import sounddevice
        import numpy
        sd = sounddevice
        np = numpy


class DictationTruce:
    """Auto-pause while the user holds spacebar to dictate; resume on release.

    Pure state machine so it is unit-testable; the key poller feeds it.
    `update` returns "pause", "resume", or None. It only ever resumes a pause
    it engaged itself, so a manual pause is never overridden.
    """

    def __init__(self, hold_ms: int) -> None:
        self.hold_s = max(0, hold_ms) / 1000.0
        self.since: float | None = None
        self.engaged = False

    def update(self, space_down: bool, speaking: bool, now: float):
        if space_down:
            if self.since is None:
                self.since = now
            elif (not self.engaged and speaking
                    and now - self.since >= self.hold_s):
                self.engaged = True
                return "pause"
        else:
            self.since = None
            if self.engaged:
                self.engaged = False
                return "resume"
        return None


class Ducker:
    """Lower other apps' volumes while speaking; restore them exactly after.

    Uses per-session volumes (Windows Core Audio via pycaw), so the system
    master volume and this process's own output are untouched. Sessions that
    appear mid-speech are simply not ducked - next reply catches them.
    """

    def __init__(self) -> None:
        self._orig: list = []  # [(ISimpleAudioVolume, original_level)]

    def duck(self, factor: float) -> None:
        if factor >= 1.0 or self._orig:
            return
        try:
            from pycaw.pycaw import AudioUtilities

            for session in AudioUtilities.GetAllSessions():
                try:
                    if session.Process and session.Process.pid == os.getpid():
                        continue  # never duck our own voice
                    vol = session.SimpleAudioVolume
                    if vol is None:
                        continue
                    level = vol.GetMasterVolume()
                    if level <= 0.0:
                        continue
                    vol.SetMasterVolume(max(0.0, level * factor), None)
                    self._orig.append((vol, level))
                except Exception:
                    continue
        except Exception as e:
            log.debug("Ducking unavailable: %s", e)

    def restore(self) -> None:
        for vol, level in self._orig:
            try:
                vol.SetMasterVolume(level, None)
            except Exception:
                pass
        self._orig.clear()


class Transport:
    """Playback state for one reply, shared by the consumer and control paths.

    All fields are guarded by `cond`. The consumer plays `chunks[idx]`; control
    commands (pause/skip/seek) mutate `paused` / `jump` and notify. `jump` is a
    requested chunk index; `pending_seek` holds a char offset that has not been
    synthesized yet (the producer resolves it as chunks arrive).
    """

    def __init__(self) -> None:
        self.chunks: list = []  # [(samples, sr, piece, start, end)]
        self.done = False       # producer finished
        self.idx = 0
        self.paused = False
        self.jump: int | None = None
        self.pending_seek: int | None = None
        self.cond = threading.Condition()


class TTSDaemon:
    def __init__(self) -> None:
        self.speak_q: "queue.Queue[str]" = queue.Queue()
        self.interrupt = threading.Event()
        self.shutdown = threading.Event()
        self.muted = config.MUTE
        self.engine = None
        self.transport: Transport | None = None  # reply currently playing
        self.history: deque = deque(maxlen=10)   # spoken replies, newest first
        self.synth_lock = threading.Lock()       # engine is not re-entrant
        self.ducker = Ducker()

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
        try:
            import comtypes
            comtypes.CoInitialize()  # pycaw needs COM on this thread
        except Exception:
            pass
        while not self.shutdown.is_set():
            try:
                text = self.speak_q.get(timeout=0.2)
            except queue.Empty:
                continue
            if text is None:
                break
            if self.muted:
                continue
            if config.TLDR_CHARS > 0 and len(text) > config.TLDR_CHARS:
                text = self._tldr_or_full(text)
                if self.shutdown.is_set():
                    break
            # Remember what was actually spoken (post-TL;DR), so replays and
            # exports reproduce exactly what was heard. Consecutive-duplicate
            # guard keeps replays from stacking up in the history.
            if not self.history or self.history[0]["text"] != text:
                self.history.appendleft(
                    {"ts": time.strftime("%H:%M"), "text": text}
                )
            self.interrupt.clear()
            factor = max(0, min(100, config.DUCK_PCT)) / 100.0
            self.ducker.duck(factor)
            try:
                self._speak(text)
            finally:
                self.ducker.restore()

    _TLDR_PROMPT = (
        "Summarize the following coding-assistant reply in at most two short "
        "sentences, written to be read aloud by text-to-speech. Keep the key "
        "outcome, numbers and names. Reply with ONLY the summary text, no "
        "preamble and no markdown."
    )

    def _tldr_or_full(self, text: str) -> str:
        """Summarise a long reply via `claude -p`; fall back to the full text.

        The child claude session runs with TTS_MUTE=1 so its own Stop hooks
        cannot speak (which would recurse into this very daemon).
        """
        import shutil
        import subprocess

        exe = shutil.which("claude")
        if not exe:
            log.warning("TL;DR: 'claude' CLI not on PATH; speaking full reply")
            return text
        cmd = [exe, "-p", "--model", config.TLDR_MODEL, self._TLDR_PROMPT]
        if exe.lower().endswith((".cmd", ".bat")):
            cmd = ["cmd", "/c"] + cmd
        t0 = time.time()
        try:
            r = subprocess.run(
                cmd,
                input=text.replace(config.PAUSE_TOKEN, "").encode("utf-8"),
                capture_output=True,
                timeout=max(5, config.TLDR_TIMEOUT),
                env=dict(os.environ, TTS_MUTE="1"),
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            summary = r.stdout.decode("utf-8", "replace").strip()
            if r.returncode == 0 and summary:
                log.info(
                    "TL;DR: %d -> %d chars in %.1fs",
                    len(text), len(summary), time.time() - t0,
                )
                return "Summary: " + summary
            log.warning("TL;DR failed (rc=%s); speaking full reply", r.returncode)
        except subprocess.TimeoutExpired:
            log.warning("TL;DR timed out (%ss); speaking full reply", config.TLDR_TIMEOUT)
        except Exception as e:
            log.warning("TL;DR error: %s; speaking full reply", e)
        return text

    def _speak(self, text: str) -> None:
        gap = max(0, config.GAP_MS) / 1000.0
        # Single-space all whitespace runs: paragraph breaks arrive as double
        # spaces, but the chunker rejoins sentences with single spaces, and
        # any mismatch breaks highlight/seek offset lookups.
        # The engine needs the pause tokens (extra silence after headings);
        # the overlay text and char offsets must not contain them.
        speak_text = " ".join(text.split())
        text = speak_text.replace(config.PAUSE_TOKEN, "")
        tr = Transport()
        self.transport = tr

        b64 = lambda s: base64.b64encode(s.encode("utf-8")).decode("ascii")
        self._overlay_send(f"SHOW:{b64(text)}")
        self._overlay_send("STATE:playing")

        def _producer() -> None:
            # Track each chunk's char range in `text` (same forward-search the
            # overlay uses) so seek requests can map an offset to a chunk.
            search_from = 0
            prev_end = 0
            try:
                # synth_lock serialises engine use with WAV export.
                with self.synth_lock:
                    for samples, sr, piece in self.engine.stream(speak_text):
                        if self.interrupt.is_set() or self.shutdown.is_set():
                            return
                        if piece:
                            start = text.find(piece, search_from)
                            if start == -1:
                                start = text.find(piece)
                            if start == -1:
                                start = prev_end
                            end = start + len(piece)
                            search_from = end
                        else:
                            start = end = prev_end
                        prev_end = end
                        with tr.cond:
                            tr.chunks.append((samples, sr, piece, start, end))
                            if tr.pending_seek is not None and tr.pending_seek < end:
                                tr.jump = len(tr.chunks) - 1
                                tr.pending_seek = None
                            tr.cond.notify_all()
            except Exception as e:
                log.warning("Synthesis failed: %s", e)
            finally:
                with tr.cond:
                    tr.done = True
                    if tr.pending_seek is not None:
                        tr.pending_seek = None
                        if tr.chunks:
                            tr.jump = len(tr.chunks) - 1
                    tr.cond.notify_all()

        threading.Thread(target=_producer, daemon=True).start()

        sequential = False  # True when the previous chunk finished naturally
        stream = None       # ONE output stream for the whole reply: opening a
        stream_key = None   # device per chunk costs 100s of ms on WASAPI and
        try:                # would run the overlay's word sweep ahead of audio
            while True:
                with tr.cond:
                    while True:
                        if self.interrupt.is_set() or self.shutdown.is_set():
                            return
                        if tr.jump is not None:
                            tr.idx = max(0, tr.jump)
                            tr.jump = None
                            sequential = False
                        if tr.idx < len(tr.chunks):
                            break
                        if tr.done:
                            return  # finished, or skipped past the end
                        tr.cond.wait(0.1)
                    samples, sr, piece, _start, _end = tr.chunks[tr.idx]
                if gap and sequential:
                    self._pause(gap)
                    if self.interrupt.is_set() or self.shutdown.is_set():
                        return
                data, dtype = self._prep_samples(samples)
                key = (sr, data.shape[1], dtype)
                if key != stream_key:
                    if stream is not None:
                        try:
                            stream.abort()
                            stream.close()
                        except Exception:
                            pass
                    stream = sd.OutputStream(
                        samplerate=sr, channels=data.shape[1], dtype=dtype,
                        latency="low",
                    )
                    stream.start()
                    stream_key = key
                if piece:
                    # SPEAK is sent AFTER the stream is ready, so the overlay's
                    # word sweep clock starts with the audio. Durations: total,
                    # leading silence (+ output latency), and the voiced span.
                    dur_ms = int(len(samples) / sr * 1000)
                    lead_ms, voice_ms = self._voiced_bounds_ms(samples, sr)
                    lead_ms += int((getattr(stream, "latency", 0.0) or 0.0) * 1000)
                    lead_ms = max(0, lead_ms + config.SWEEP_OFFSET_MS)
                    self._overlay_send(
                        f"SPEAK:{b64(piece)}:{dur_ms}:{lead_ms}:{voice_ms}"
                    )
                with tr.cond:
                    prog = self._progress_msg(tr, len(text))
                self._overlay_send(prog)
                result = self._play_chunk(data, stream, tr)
                if result == "abort":
                    return
                if result == "jump":
                    continue
                sequential = True
                with tr.cond:
                    if tr.jump is None:
                        tr.idx += 1
        finally:
            if stream is not None:
                try:
                    if self.interrupt.is_set() or self.shutdown.is_set():
                        stream.abort()   # cut immediately
                    else:
                        stream.stop()    # drain the last chunk's tail
                    stream.close()
                except Exception:
                    pass
            self.transport = None
            self._overlay_send("HIDE")

    def _pause(self, seconds: float) -> None:
        """Silent, interruptible delay between chunks."""
        end = time.time() + seconds
        while time.time() < end:
            if self.interrupt.is_set() or self.shutdown.is_set():
                return
            time.sleep(0.02)

    @staticmethod
    def _prep_samples(samples):
        """(frames, channels) contiguous array + sounddevice dtype string."""
        data = np.ascontiguousarray(samples)
        if data.ndim == 1:
            data = data.reshape(-1, 1)
        if data.dtype == np.int16:
            return data, "int16"
        if data.dtype != np.float32:
            data = data.astype(np.float32)
        return data, "float32"

    def _play_chunk(self, data, stream, tr: Transport) -> str:
        """Write one chunk in ~50 ms blocks so pause/skip/stop react mid-way.

        The stream is shared across the reply and stays open. Returns "done"
        (chunk fully written), "jump" (a skip/seek moved the cursor; buffered
        audio is discarded) or "abort" (interrupt/shutdown).
        """
        block = max(1, int(stream.samplerate * 0.05))
        try:
            i = 0
            while i < len(data):
                if self.interrupt.is_set() or self.shutdown.is_set():
                    return "abort"
                if tr.jump is not None:
                    self._discard_buffered(stream)
                    return "jump"
                if tr.paused:
                    stream.stop()  # drains the small buffered remainder
                    while tr.paused:
                        if self.interrupt.is_set() or self.shutdown.is_set():
                            return "abort"
                        if tr.jump is not None:
                            break
                        time.sleep(0.03)
                    stream.start()
                    continue  # re-check jump/interrupt before writing on
                stream.write(data[i:i + block])
                i += block
        except Exception as e:
            log.warning("Playback failed: %s", e)
        return "done"

    @staticmethod
    def _discard_buffered(stream) -> None:
        """Drop not-yet-played audio (on skip/seek) and keep the stream usable."""
        try:
            stream.abort()
            stream.start()
        except Exception:
            pass

    @staticmethod
    def _voiced_bounds_ms(samples, sr) -> tuple:
        """(leading-silence ms, voiced-span ms) of a chunk via energy threshold.

        TTS chunks start with model lead-in silence and end with padding; the
        overlay's word sweep should only span the audible part.
        """
        try:
            a = np.abs(np.asarray(samples, dtype=np.float32))
            if samples.dtype == np.int16:
                a /= 32768.0
            peak = float(a.max()) if a.size else 0.0
            total = int(len(samples) / sr * 1000)
            if peak < 1e-4:
                return 0, total
            idx = np.nonzero(a > peak * 0.04)[0]
            lead = int(idx[0] / sr * 1000)
            voice = max(1, int((idx[-1] - idx[0] + 1) / sr * 1000))
            return lead, voice
        except Exception:
            return 0, int(len(samples) / sr * 1000)

    @staticmethod
    def _progress_msg(tr: Transport, text_len: int) -> str:
        """Build a PROG:<cur>:<total>:<frac>:<secs-left> line (tr.cond held).

        Durations of synthesized chunks are exact (samples / rate); text not
        yet synthesized is estimated from the speaking rate observed so far.
        While the producer is still running, `total` is likewise an estimate
        from the average chunk length; it settles once synthesis finishes.
        """
        idx = min(tr.idx, len(tr.chunks) - 1)
        durs = [len(c[0]) / c[1] for c in tr.chunks]
        synth_end = max(c[4] for c in tr.chunks)
        remaining = sum(durs[idx:])
        total = len(tr.chunks)
        if not tr.done and 0 < synth_end < text_len:
            rate = sum(durs) / synth_end  # seconds per char so far
            remaining += (text_len - synth_end) * rate
            avg_chars = synth_end / len(tr.chunks)
            total += max(0, math.ceil((text_len - synth_end) / avg_chars))
        frac = tr.chunks[idx][3] / text_len if text_len else 0.0
        return f"PROG:{idx + 1}:{total}:{frac:.3f}:{int(round(remaining))}"

    # --- reply history -------------------------------------------------------
    def say_history(self, n: int) -> None:
        """Re-speak history item n (0 = latest)."""
        try:
            item = self.history[n]
        except IndexError:
            return
        log.info("Replay history[%d] (%d chars)", n, len(item["text"]))
        self.enqueue(item["text"])

    def export_history(self, n: int):
        """Synthesise history item n to a WAV file; returns the path or None."""
        try:
            item = self.history[n]
        except IndexError:
            return None
        out_dir = Path(config.EXPORT_DIR)
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            log.warning("Export dir unavailable: %s", e)
            return None
        path = out_dir / time.strftime("reply-%Y%m%d-%H%M%S.wav")
        parts, sr_out = [], None
        t0 = time.time()
        with self.synth_lock:  # wait out any in-flight synthesis
            try:
                for samples, sr, _piece in self.engine.stream(item["text"]):
                    if self.shutdown.is_set():
                        return None
                    parts.append(samples)
                    sr_out = sr
            except Exception as e:
                log.warning("Export synthesis failed: %s", e)
                return None
        if not parts:
            return None
        data = np.concatenate(parts)
        if data.dtype != np.int16:
            data = (np.clip(data, -1.0, 1.0) * 32767).astype(np.int16)
        import wave
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(sr_out)
            w.writeframes(data.tobytes())
        log.info(
            "Exported history[%d] (%d chars) -> %s in %.1fs",
            n, len(item["text"]), path, time.time() - t0,
        )
        return str(path)

    # --- transport controls --------------------------------------------------
    def set_pause(self, paused: bool) -> None:
        tr = self.transport
        if tr is None:
            return
        with tr.cond:
            if tr.paused == paused:
                return
            tr.paused = paused
            tr.cond.notify_all()
        self._overlay_send("STATE:paused" if paused else "STATE:playing")
        log.info("Playback %s", "paused" if paused else "resumed")

    def toggle_pause(self) -> None:
        tr = self.transport
        if tr is None:
            return
        self.set_pause(not tr.paused)

    def skip(self, delta: int) -> None:
        """Move the cursor by `delta` chunks; past the end simply finishes."""
        tr = self.transport
        if tr is None:
            return
        with tr.cond:
            tr.jump = max(0, tr.idx + delta)
            tr.paused = False
            tr.cond.notify_all()
        self._overlay_send("STATE:playing")
        log.info("Skip %+d", delta)

    def seek(self, offset: int) -> None:
        """Jump to the chunk containing char `offset` (overlay click-to-seek)."""
        tr = self.transport
        if tr is None:
            return
        with tr.cond:
            target = None
            covered = False
            for i, (_, _, piece, start, end) in enumerate(tr.chunks):
                if piece and start <= offset:
                    target = i
                    covered = offset < end
            if target is None:
                if tr.chunks:
                    tr.jump = 0  # clicked before the first chunk
                elif not tr.done:
                    tr.pending_seek = offset
            elif covered or tr.done:
                tr.jump = target
            else:
                tr.pending_seek = offset  # not synthesized yet
            tr.paused = False
            tr.cond.notify_all()
        self._overlay_send("STATE:playing")
        log.info("Seek to char %d", offset)

    # --- global hotkey poll --------------------------------------------------
    def key_poller(self) -> None:
        """Poll global hotkeys via Win32 (works whatever window has focus).

        ESC (bare)          -> stop playback
        Ctrl+Alt+Space      -> pause / resume
        Ctrl+Alt+Right/Left -> skip forward / back one sentence

        Transport keys need Ctrl+Alt so normal typing is never hijacked; ESC
        stays bare for backwards compatibility (TTS_INTERRUPT_VK).
        """
        try:
            user32 = ctypes.windll.user32
        except AttributeError:
            log.info("Key polling unavailable on this platform; skipping.")
            return

        def down(vk: int) -> bool:
            return bool(user32.GetAsyncKeyState(vk) & 0x8000)

        VK_CONTROL, VK_MENU = 0x11, 0x12
        combos = {
            0x20: self.toggle_pause,           # Space
            0x27: lambda: self.skip(+1),       # Right arrow
            0x25: lambda: self.skip(-1),       # Left arrow
            0x52: lambda: self.say_history(0), # R - replay last reply
        }
        was = {vk: False for vk in combos}
        was_stop = False
        truce = (
            DictationTruce(config.DICTATION_MS)
            if config.DICTATION_TRUCE else None
        )
        while not self.shutdown.is_set():
            stop_down = down(config.INTERRUPT_VK)
            if stop_down and not was_stop:
                self.stop_now()
                log.info("Interrupt key pressed -> playback stopped")
            was_stop = stop_down
            mods = down(VK_CONTROL) and down(VK_MENU)
            for vk, action in combos.items():
                pressed = mods and down(vk)
                if pressed and not was[vk]:
                    action()
                was[vk] = pressed
            if truce is not None:
                tr = self.transport
                speaking = tr is not None and not tr.paused
                verdict = truce.update(
                    down(0x20) and not mods, speaking, time.time(),
                )
                if verdict == "pause":
                    self.set_pause(True)
                    log.info("Dictation truce: paused (spacebar held)")
                elif verdict == "resume":
                    self.set_pause(False)
                    log.info("Dictation truce: resumed")
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
            elif line == config.CTRL_PAUSE:
                self.toggle_pause()
            elif line == config.CTRL_NEXT:
                self.skip(+1)
            elif line == config.CTRL_PREV:
                self.skip(-1)
            elif line.startswith(config.CTRL_SEEK):
                arg = line[len(config.CTRL_SEEK):].strip()
                if arg.isdigit():
                    self.seek(int(arg))
            elif line == config.CTRL_HISTORY:
                items = [
                    {"ts": e["ts"],
                     "preview": e["text"].replace(config.PAUSE_TOKEN, "")[:70]}
                    for e in self.history
                ]
                try:
                    conn.sendall((json.dumps(items) + "\n").encode("utf-8"))
                except OSError:
                    pass
            elif line.startswith(config.CTRL_SAY):
                arg = line[len(config.CTRL_SAY):].strip()
                self.say_history(int(arg) if arg.isdigit() else 0)
            elif line.startswith(config.CTRL_EXPORT):
                arg = line[len(config.CTRL_EXPORT):].strip()
                path = self.export_history(int(arg) if arg.isdigit() else 0)
                try:
                    conn.sendall(((path or "ERROR") + "\n").encode("utf-8"))
                except OSError:
                    pass
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

    # --- overlay -----------------------------------------------------------
    def _overlay_send(self, cmd: str) -> None:
        if not config.OVERLAY:
            return
        try:
            with socket.create_connection(("127.0.0.1", config.OVERLAY_PORT), timeout=0.15) as s:
                s.sendall((cmd + "\n").encode("utf-8"))
        except OSError:
            pass  # overlay not running — skip silently

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
            self.ducker.restore()  # never leave other apps ducked
            sd.stop()
            log.info("Daemon stopped.")


def _log_unhandled(exc_type, exc_val, exc_tb) -> None:
    log.critical("Unhandled exception — daemon will exit", exc_info=(exc_type, exc_val, exc_tb))


def _write_pid() -> None:
    config.STATE_DIR.mkdir(parents=True, exist_ok=True)
    (config.STATE_DIR / "tts_server.pid").write_text(str(os.getpid()))


def _remove_pid() -> None:
    try:
        (config.STATE_DIR / "tts_server.pid").unlink(missing_ok=True)
    except Exception:
        pass


def main() -> None:
    _setup_logging()
    sys.excepthook = _log_unhandled
    if not _acquire_singleton():
        log.info("Another TTS daemon already owns port %s; exiting.", config.PORT)
        return
    _write_pid()
    try:
        TTSDaemon().run()
    finally:
        _remove_pid()


if __name__ == "__main__":
    main()
