"""Floating karaoke-style overlay that highlights the sentence being spoken.

Started alongside the TTS daemon by launch_server.py. Listens on
TTS_OVERLAY_PORT (default 7767) for newline-terminated commands:
  SHOW:<base64>   – display the overlay with the full reply text
  SPEAK:<base64>[:<total>:<lead>:<voiced>] – highlight this chunk; the ms
                    durations drive a word-level sweep that waits out the
                    leading silence and spans only the voiced audio
  STATE:<name>    – transport state ("paused" / "playing"); also freezes and
                    resumes the word sweep
  PROG:<cur>:<total>:<frac>:<secs> – progress bar + "sentence 4 of 12" readout
  HIDE            – fade out and hide the window

The window is borderless, always-on-top, draggable, resizable via the ◢ grip
(bottom-right) and scrollable with the mouse wheel; the spoken sentence is
auto-centred in the view. The – button collapses to a one-line "pill" showing
only the current sentence (▢ expands back). Size, position and pill mode
persist across runs in .state/overlay_geometry.txt (any monitor; falls back
to centred-on-primary if the saved spot is no longer on-screen). Transport
buttons (⏮ ⏯ ⏭ ✕) and click-a-sentence-to-seek send commands back to the
daemon on TTS_PORT. Set TTS_OVERLAY=0 to disable.
"""
from __future__ import annotations

import base64
import os
import re
import socket
import sys
import threading
import time
import tkinter as tk
from tkinter import font as tkfont

import config

HOST = "127.0.0.1"
PORT = config.OVERLAY_PORT
GEOM_FILE = config.STATE_DIR / "overlay_geometry.txt"


class OverlayWindow:
    BG       = "#1a1b26"
    FG       = "#c0caf5"
    HL_BG    = "#e0af68"
    HL_FG    = "#1a1b26"
    WORD_BG  = "#ff9e64"
    BORDER   = "#414868"
    FONT_FAM = "Segoe UI"
    FONT_SZ  = 13
    PAD      = 16
    HEIGHT   = 210
    W_FRAC   = 0.52
    ALPHA    = 0.93
    MIN_W    = 380
    MIN_H    = 140
    PILL_H   = 46

    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.title("")
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.attributes("-alpha", 0.0)
        self.root.configure(bg=self.BORDER)

        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        self._sw, self._sh = sw, sh
        self._pill = False
        self._full_h = self.HEIGHT  # height to restore when leaving pill mode
        start_pill = False
        saved = self._load_geometry()
        if saved:
            w, h, x, y, start_pill = saved
            self._full_h = h
            self.root.geometry(f"{w}x{h}+{x}+{y}")
        else:
            w = max(620, min(1080, int(sw * self.W_FRAC)))
            self._reposition(w, self.HEIGHT)
        # winfo_width() reads 1 until the window is mapped, so keep the
        # intended width ourselves for geometry calls made before/around that.
        self._win_w = w

        # 1-px border effect via a Frame
        inner = tk.Frame(self.root, bg=self.BG)
        inner.pack(fill=tk.BOTH, expand=True, padx=1, pady=1)

        self._txt = tk.Text(
            inner, wrap=tk.WORD,
            font=(self.FONT_FAM, self.FONT_SZ),
            bg=self.BG, fg=self.FG,
            relief=tk.FLAT, bd=0,
            padx=self.PAD, pady=self.PAD,
            state=tk.DISABLED, cursor="arrow",
            spacing1=3, spacing3=3,
            selectbackground=self.BG,
        )
        self._txt.pack(fill=tk.BOTH, expand=True, pady=(30, 8))

        # Pill-mode line — a one-line Text (not a Label) so the current word
        # can be highlighted and kept centred. Packed on demand.
        self._pill_txt = tk.Text(
            inner, height=1, wrap=tk.NONE,
            font=(self.FONT_FAM, 12),
            bg=self.BG, fg=self.FG,
            relief=tk.FLAT, bd=0, highlightthickness=0,
            state=tk.DISABLED, cursor="arrow",
            selectbackground=self.BG,
        )
        self._pill_txt.tag_configure(
            "word", background=self.WORD_BG, foreground=self.HL_FG,
        )

        # Progress bar — a thin accent line along the bottom edge
        track = tk.Frame(inner, height=3, bg="#2a2e42")
        track.place(relx=0.0, rely=1.0, relwidth=1.0, anchor="sw")
        self._bar = tk.Frame(track, bg=self.HL_BG)
        self._bar.place(x=0, y=0, relheight=1.0, relwidth=0.0)

        # Progress readout — "sentence 4 of 12 · ~35s left", top-centre
        self._prog = tk.Label(
            inner, text="",
            font=(self.FONT_FAM, 9),
            bg=self.BG, fg="#6272a4",
        )
        self._prog.place(relx=0.5, rely=0.0, anchor="n", y=8)
        self._txt.tag_configure(
            "hl", background=self.HL_BG, foreground=self.HL_FG,
        )
        # Configured after "hl" so the word highlight renders on top of it.
        self._txt.tag_configure(
            "word", background=self.WORD_BG, foreground=self.HL_FG,
        )

        # Close button — top-right corner, stops playback and hides the overlay
        close = tk.Label(
            inner,
            text="✕",
            font=(self.FONT_FAM, 11),
            bg=self.BG, fg="#6272a4",
            cursor="hand2",
        )
        close.place(relx=1.0, rely=0.0, anchor="ne", x=-10, y=6)
        close.bind("<Button-1>", lambda e: self._close_clicked())
        close.bind("<Enter>", lambda e: close.configure(fg="#f7768e"))
        close.bind("<Leave>", lambda e: close.configure(fg="#6272a4"))

        # Move handle — far left of the header strip. The whole header (and any
        # empty area) drags too; this is the visible affordance for it.
        move = tk.Label(
            inner, text="✥",
            font=(self.FONT_FAM, 11),
            bg=self.BG, fg="#6272a4",
            cursor="fleur",
        )
        move.place(x=10, y=6)
        move.bind("<Enter>", lambda e: move.configure(fg="#7aa2f7"))
        move.bind("<Leave>", lambda e: move.configure(fg="#6272a4"))

        # Transport buttons — right of the move handle. ⏯ mirrors the daemon's
        # state (STATE:paused / STATE:playing messages).
        self._btn_pause = None
        x = 38
        for glyph, cmd in (("⏮", config.CTRL_PREV),
                           ("⏸", config.CTRL_PAUSE),
                           ("⏭", config.CTRL_NEXT)):
            btn = tk.Label(
                inner, text=glyph,
                font=(self.FONT_FAM, 11),
                bg=self.BG, fg="#6272a4",
                cursor="hand2",
            )
            btn.place(x=x, y=6)
            btn.bind("<Button-1>", lambda e, c=cmd: self._daemon_async(c))
            btn.bind("<Enter>", lambda e, b=btn: b.configure(fg="#7aa2f7"))
            btn.bind("<Leave>", lambda e, b=btn: b.configure(fg="#6272a4"))
            if cmd == config.CTRL_PAUSE:
                self._btn_pause = btn
            x += 26

        # Pill-mode toggle — collapses to a one-line strip and back
        self._btn_mode = tk.Label(
            inner, text="–",
            font=(self.FONT_FAM, 11),
            bg=self.BG, fg="#6272a4",
            cursor="hand2",
        )
        self._btn_mode.place(relx=1.0, rely=0.0, anchor="ne", x=-32, y=6)
        self._btn_mode.bind("<Button-1>", lambda e: self._set_pill(not self._pill))
        self._btn_mode.bind("<Enter>", lambda e: self._btn_mode.configure(fg="#7aa2f7"))
        self._btn_mode.bind("<Leave>", lambda e: self._btn_mode.configure(fg="#6272a4"))

        # Keyboard shortcut hint — left of the mode/close buttons
        self._hint = tk.Label(
            inner,
            text="Ctrl+Alt+Space pause  ·  Ctrl+Alt+←/→ skip  ·  ESC stop",
            font=(self.FONT_FAM, 9),
            bg=self.BG, fg="#6272a4",
            cursor="arrow",
        )
        self._hint.place(relx=1.0, rely=0.0, anchor="ne", x=-56, y=8)

        # Resize grip — bottom-right corner. Its handlers return "break" so the
        # window-level drag bindings below don't also move the window.
        self._grip = tk.Label(
            inner, text="◢",
            font=(self.FONT_FAM, 8),
            bg=self.BG, fg="#414868",
            cursor="size_nw_se",
        )
        self._grip.place(relx=1.0, rely=1.0, anchor="se", x=-2, y=-5)
        self._grip.bind("<Button-1>", self._resize_start)
        self._grip.bind("<B1-Motion>", self._resize_move)
        self._grip.bind("<ButtonRelease-1>", self._resize_end)

        # Drag support; a release without movement on the text is a seek-click.
        # Bind ONLY on root — it receives events from every child via bindtags.
        # Binding on children too would run each handler twice per event, and
        # the second run (stale winfo_x, zero delta) undoes the move.
        self.root.bind("<Button-1>", self._drag_start)
        self.root.bind("<B1-Motion>", self._drag_move)
        self._txt.bind("<ButtonRelease-1>", self._txt_release)
        self.root.bind("<ButtonRelease-1>", lambda e: self._save_geometry())
        self.root.bind("<MouseWheel>", self._on_wheel)
        self._dx = self._dy = 0
        self._ox = self._oy = 0  # window origin at drag start
        self._px = self._py = 0  # press origin, to tell a click from a drag
        self._rw = self._rh = self._rx = self._ry = 0  # resize-drag state

        self._alpha    = 0.0
        self._fade_id  = None
        self._full     = ""
        self._from     = 0
        self._paused   = False
        self._sweep    = None  # word-karaoke sweep state
        self._sweep_id = None

        if start_pill:
            self._set_pill(True)

        threading.Thread(target=self._serve, daemon=True).start()

    # ── positioning / geometry persistence ───────────────────────────────
    def _reposition(self, w: int, h: int) -> None:
        x = (self._sw - w) // 2
        y = (self._sh - h) // 2
        self.root.geometry(f"{w}x{h}+{x}+{y}")

    def _load_geometry(self):
        """Last saved size+position, or None if absent or fully off-screen.

        Validated against the Windows virtual desktop (all monitors), so a
        position on a second screen is kept — but a stale one from a monitor
        that is no longer connected falls back to centred-on-primary.
        """
        try:
            lines = GEOM_FILE.read_text().strip().splitlines()
            m = re.fullmatch(r"(\d+)x(\d+)([+-]\d+)([+-]\d+)", lines[0].strip())
            if not m:
                return None
            pill = len(lines) > 1 and lines[1].strip() == "pill"
            w, h = max(self.MIN_W, int(m[1])), max(self.MIN_H, int(m[2]))
            x, y = int(m[3]), int(m[4])
            try:
                import ctypes
                u = ctypes.windll.user32
                vx, vy = u.GetSystemMetrics(76), u.GetSystemMetrics(77)
                vw, vh = u.GetSystemMetrics(78), u.GetSystemMetrics(79)
                margin = 40  # this much of the window must remain reachable
                if (x + w < vx + margin or x > vx + vw - margin
                        or y + h < vy + margin or y > vy + vh - margin):
                    return None
            except AttributeError:
                pass  # non-Windows: accept as-is
            return w, h, x, y, pill
        except (OSError, ValueError, IndexError):
            return None

    def _save_geometry(self) -> None:
        if self.root.winfo_width() <= 1:
            return  # not mapped yet - winfo would record a bogus 1x1+0+0
        try:
            config.STATE_DIR.mkdir(parents=True, exist_ok=True)
            # Always record the FULL-mode height so leaving pill mode (now or
            # next run) restores the real size; a second line flags pill mode.
            h = self._full_h if self._pill else self.root.winfo_height()
            data = (
                f"{self.root.winfo_width()}x{h}"
                f"+{self.root.winfo_x()}+{self.root.winfo_y()}"
            )
            if self._pill:
                data += "\npill"
            GEOM_FILE.write_text(data)
        except OSError:
            pass

    # ── pill mode ─────────────────────────────────────────────────────────
    def _set_pill(self, pill: bool) -> None:
        """Collapse to a one-line strip with the current sentence, or expand."""
        if pill == self._pill:
            return
        self._pill = pill
        w = self.root.winfo_width()
        if w > 1:
            self._win_w = w
        if pill:
            h = self.root.winfo_height()
            if h > self.PILL_H + 10:  # ignore the pre-map 1px default
                self._full_h = h
            self._txt.pack_forget()
            self._prog.place_forget()
            self._hint.place_forget()
            self._grip.place_forget()
            self._pill_txt.pack(fill=tk.X, expand=True, padx=(96, 56))
            self.root.geometry(f"{self._win_w}x{self.PILL_H}")
            self._btn_mode.configure(text="▢")
        else:
            self._pill_txt.pack_forget()
            self._txt.pack(fill=tk.BOTH, expand=True, pady=(30, 8))
            self._prog.place(relx=0.5, rely=0.0, anchor="n", y=8)
            self._hint.place(relx=1.0, rely=0.0, anchor="ne", x=-56, y=8)
            self._grip.place(relx=1.0, rely=1.0, anchor="se", x=-2, y=-5)
            self.root.geometry(f"{self._win_w}x{self._full_h}")
            self._btn_mode.configure(text="–")
        self._save_geometry()

    # ── resize grip ───────────────────────────────────────────────────────
    def _resize_start(self, e: tk.Event):
        self._rw, self._rh = self.root.winfo_width(), self.root.winfo_height()
        self._rx, self._ry = e.x_root, e.y_root
        return "break"

    def _resize_move(self, e: tk.Event):
        w = max(self.MIN_W, self._rw + e.x_root - self._rx)
        h = max(self.MIN_H, self._rh + e.y_root - self._ry)
        self._win_w = w
        self.root.geometry(f"{w}x{h}")
        return "break"

    def _resize_end(self, e: tk.Event):
        self._save_geometry()
        return "break"

    # ── scrolling ─────────────────────────────────────────────────────────
    def _on_wheel(self, e: tk.Event):
        self._txt.yview_scroll(-e.delta // 2, "pixels")
        return "break"

    # ── daemon commands ───────────────────────────────────────────────────
    def _close_clicked(self) -> None:
        self._fade_out()
        # Also stop the daemon's playback, same as pressing ESC.
        self._daemon_async(config.CTRL_STOP)

    def _daemon_async(self, cmd: str) -> None:
        threading.Thread(target=self._send_daemon, args=(cmd,), daemon=True).start()

    @staticmethod
    def _send_daemon(cmd: str) -> None:
        try:
            with socket.create_connection((config.HOST, config.PORT), timeout=0.5) as s:
                s.sendall((cmd + "\n").encode("utf-8"))
        except OSError:
            pass

    # ── drag / click-to-seek ─────────────────────────────────────────────
    def _drag_start(self, e: tk.Event) -> None:
        self._dx, self._dy = e.x_root, e.y_root
        self._px, self._py = e.x_root, e.y_root
        self._ox, self._oy = self.root.winfo_x(), self.root.winfo_y()

    def _drag_move(self, e: tk.Event) -> None:
        # Absolute maths from the drag-start origin: idempotent, so a repeated
        # delivery of the same event can't undo the move.
        x = self._ox + e.x_root - self._dx
        y = self._oy + e.y_root - self._dy
        self.root.geometry(f"+{x}+{y}")

    def _txt_release(self, e: tk.Event) -> None:
        if abs(e.x_root - self._px) + abs(e.y_root - self._py) > 4:
            return  # it was a drag, not a click
        if not self._full:
            return
        try:
            index = self._txt.index(f"@{e.x},{e.y}")
            counted = self._txt.count("1.0", index, "chars")
            offset = counted[0] if counted else 0
        except tk.TclError:
            return
        self._daemon_async(f"{config.CTRL_SEEK} {max(0, offset)}")

    # ── socket server ─────────────────────────────────────────────────────
    def _serve(self) -> None:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        # Deliberately no SO_REUSEADDR: on Windows it lets multiple processes
        # bind the same port, which would make command routing non-deterministic.
        try:
            srv.bind((HOST, PORT))
        except OSError:
            # Another overlay already owns the port — exit immediately.
            os._exit(0)
        srv.listen(8)
        srv.settimeout(0.5)
        while True:
            try:
                conn, _ = srv.accept()
            except socket.timeout:
                continue
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn: socket.socket) -> None:
        buf = b""
        with conn:
            conn.settimeout(2.0)
            try:
                while True:
                    data = conn.recv(4096)
                    if not data:
                        break
                    buf += data
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        self._dispatch(line.decode("utf-8", errors="replace").strip())
            except OSError:
                pass

    def _dispatch(self, line: str) -> None:
        if line == "HIDE":
            self.root.after(0, self._fade_out)
        elif line.startswith("SHOW:"):
            text = base64.b64decode(line[5:]).decode("utf-8")
            self.root.after(0, lambda t=text: self._show(t))
        elif line.startswith("SPEAK:"):
            payload = line[6:].split(":")  # base64 has no ":", so this is safe
            chunk = base64.b64decode(payload[0]).decode("utf-8")
            nums = []
            for part in payload[1:4]:
                try:
                    nums.append(int(part))
                except ValueError:
                    break
            dur_ms = nums[0] if len(nums) > 0 else 0
            lead_ms = nums[1] if len(nums) > 1 else 0
            voice_ms = nums[2] if len(nums) > 2 else max(0, dur_ms - lead_ms)
            self.root.after(0, lambda c=chunk, l=lead_ms, v=voice_ms:
                            self._highlight(c, l, v))
        elif line.startswith("STATE:"):
            state = line[6:]
            self.root.after(0, lambda s=state: self._set_state(s))
        elif line.startswith("PROG:"):
            try:
                cur, total, frac, secs = line[5:].split(":")
                cur, total, frac, secs = int(cur), int(total), float(frac), int(secs)
            except ValueError:
                return
            self.root.after(0, lambda: self._set_progress(cur, total, frac, secs))

    # ── UI updates (main thread only) ─────────────────────────────────────
    def _set_state(self, state: str) -> None:
        self._paused = state == "paused"
        if self._btn_pause is not None:
            self._btn_pause.configure(text="▶" if self._paused else "⏸")

    def _set_progress(self, cur: int, total: int, frac: float, secs: int) -> None:
        mins, s = divmod(max(0, secs), 60)
        left = f"{mins}:{s:02d}" if mins else f"{s}s"
        self._prog.configure(text=f"sentence {cur} of {total}  ·  ~{left} left")
        self._bar.place_configure(relwidth=max(0.0, min(1.0, frac)))

    def _pill_set(self, text: str) -> None:
        self._pill_txt.configure(state=tk.NORMAL)
        self._pill_txt.delete("1.0", tk.END)
        self._pill_txt.insert("1.0", text)
        self._pill_txt.configure(state=tk.DISABLED)

    def _show(self, text: str) -> None:
        self._cancel_sweep()
        self._full = text
        self._from = 0
        self._prog.configure(text="")
        self._bar.place_configure(relwidth=0.0)
        self._pill_set("…")
        self._txt.configure(state=tk.NORMAL)
        self._txt.delete("1.0", tk.END)
        self._txt.insert("1.0", text)
        self._txt.tag_remove("hl", "1.0", tk.END)
        self._txt.configure(state=tk.DISABLED)
        self._txt.yview_moveto(0.0)
        self._fade_in()

    def _highlight(self, chunk: str, lead_ms: int = 0, voice_ms: int = 0) -> None:
        self._pill_set(chunk)
        pos = self._full.find(chunk, self._from)
        if pos == -1:
            pos = self._full.find(chunk)
        if pos == -1:
            self._cancel_sweep()
            return
        end = pos + len(chunk)
        self._from = end
        self._txt.tag_remove("hl", "1.0", tk.END)
        si = f"1.0+{pos}c"
        ei = f"1.0+{end}c"
        self._txt.tag_add("hl", si, ei)
        self._center(si)
        self._start_sweep(pos, chunk, lead_ms, voice_ms)

    # ── word-level karaoke sweep ──────────────────────────────────────────
    def _cancel_sweep(self) -> None:
        if self._sweep_id:
            self.root.after_cancel(self._sweep_id)
            self._sweep_id = None
        self._sweep = None
        self._txt.tag_remove("word", "1.0", tk.END)
        self._pill_txt.tag_remove("word", "1.0", tk.END)

    def _start_sweep(self, base: int, chunk: str, lead_ms: int, voice_ms: int) -> None:
        """Sweep the word highlight across `chunk` in time with the audio.

        The sweep waits out the chunk's leading silence (`lead_ms`) so the
        first word lights up when it is actually spoken, then distributes the
        voiced span (`voice_ms`) across the words proportionally to length
        (+1 char for the following gap). Drift cannot accumulate: every new
        sentence restarts the sweep from the real audio clock.
        """
        self._cancel_sweep()
        if voice_ms <= 0:
            return
        words = [(m.start(), m.end()) for m in re.finditer(r"\S+", chunk)]
        if not words:
            return
        weight = sum((e - s) + 1 for s, e in words)
        spans, t = [], float(lead_ms)
        for s, e in words:
            dt = ((e - s) + 1) / weight * voice_ms
            spans.append((s, e, t, t + dt))
            t += dt
        self._sweep = {
            "base": base, "spans": spans, "dur": t,
            "t0": time.monotonic(), "elapsed": 0.0, "i": -1,
        }
        self._sweep_tick()

    def _sweep_tick(self) -> None:
        sw = self._sweep
        if sw is None:
            return
        now = time.monotonic()
        if self._paused:
            sw["t0"] = now - sw["elapsed"] / 1000.0  # freeze the clock
        else:
            sw["elapsed"] = (now - sw["t0"]) * 1000.0
        el = sw["elapsed"]
        cur = None
        for j, (_s, _e, ts, te) in enumerate(sw["spans"]):
            if ts <= el < te:
                cur = j
                break
        if cur is None and el >= sw["dur"]:
            cur = len(sw["spans"]) - 1
        if cur is not None and cur != sw["i"]:
            sw["i"] = cur
            s, e, _, _ = sw["spans"][cur]
            self._word_tag(sw["base"] + s, sw["base"] + e, s, e)
        if el < sw["dur"]:
            self._sweep_id = self.root.after(30, self._sweep_tick)
        else:
            self._sweep_id = None

    def _word_tag(self, abs_s: int, abs_e: int, rel_s: int, rel_e: int) -> None:
        self._txt.tag_remove("word", "1.0", tk.END)
        self._txt.tag_add("word", f"1.0+{abs_s}c", f"1.0+{abs_e}c")
        self._pill_txt.tag_remove("word", "1.0", tk.END)
        self._pill_txt.tag_add("word", f"1.0+{rel_s}c", f"1.0+{rel_e}c")
        if self._pill:
            self._pill_center(rel_s, rel_e)

    def _pill_center(self, s: int, e: int) -> None:
        """Keep the highlighted word horizontally centred in the pill line."""
        try:
            # "1.0 lineend", not "end": measuring across the trailing newline
            # yields 0 and the view would never move.
            total = (self._pill_txt.count("1.0", "1.0 lineend", "xpixels") or (0,))[0]
            left = (self._pill_txt.count("1.0", f"1.0+{s}c", "xpixels") or (0,))[0]
            wpx = (self._pill_txt.count(f"1.0+{s}c", f"1.0+{e}c", "xpixels") or (0,))[0]
            vis = self._pill_txt.winfo_width()
            if total > vis > 0:
                target = left + wpx / 2 - vis / 2
                self._pill_txt.xview_moveto(max(0.0, target) / total)
            else:
                self._pill_txt.xview_moveto(0.0)
        except tk.TclError:
            pass

    def _center(self, si: str) -> None:
        """Scroll so the highlighted sentence sits in the middle of the view."""
        try:
            self._txt.update_idletasks()
            y = (self._txt.count("1.0", si, "ypixels") or (0,))[0]
            total = (self._txt.count("1.0", "end", "ypixels") or (0,))[0]
            h = self._txt.winfo_height()
            if total > h > 0:
                self._txt.yview_moveto(max(0.0, y - h / 2) / total)
            else:
                self._txt.yview_moveto(0.0)
        except tk.TclError:
            self._txt.see(si)

    # ── fade animations ───────────────────────────────────────────────────
    def _cancel_fade(self) -> None:
        if self._fade_id:
            self.root.after_cancel(self._fade_id)
            self._fade_id = None

    def _fade_in(self) -> None:
        self._cancel_fade()
        self._alpha = float(self.root.attributes("-alpha"))
        self._tick_in()

    def _tick_in(self) -> None:
        self._alpha = min(self.ALPHA, self._alpha + 0.09)
        self.root.attributes("-alpha", self._alpha)
        if self._alpha < self.ALPHA:
            self._fade_id = self.root.after(16, self._tick_in)

    def _fade_out(self) -> None:
        self._cancel_sweep()
        self._cancel_fade()
        self._alpha = float(self.root.attributes("-alpha"))
        self._tick_out()

    def _tick_out(self) -> None:
        self._alpha = max(0.0, self._alpha - 0.055)
        self.root.attributes("-alpha", self._alpha)
        if self._alpha > 0.0:
            self._fade_id = self.root.after(22, self._tick_out)

    def run(self) -> None:
        self.root.mainloop()


def main() -> None:
    if not config.OVERLAY:
        return

    # Single-instance guard: if something already answers on the port, exit.
    try:
        with socket.create_connection((HOST, PORT), timeout=0.3):
            return
    except OSError:
        pass

    OverlayWindow().run()


if __name__ == "__main__":
    main()
