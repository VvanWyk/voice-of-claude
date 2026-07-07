"""Floating karaoke-style overlay that highlights the sentence being spoken.

Started alongside the TTS daemon by launch_server.py. Listens on
TTS_OVERLAY_PORT (default 7767) for newline-terminated commands:
  SHOW:<base64>   – display the overlay with the full reply text
  SPEAK:<base64>  – highlight this chunk in the displayed text
  STATE:<name>    – transport state ("paused" / "playing") for the ⏯ button
  HIDE            – fade out and hide the window

The window is borderless, always-on-top, and draggable. Transport buttons
(⏮ ⏯ ⏭ ✕) and click-a-sentence-to-seek send commands back to the daemon on
TTS_PORT. Set TTS_OVERLAY=0 to disable.
"""
from __future__ import annotations

import base64
import os
import socket
import sys
import threading
import tkinter as tk
from tkinter import font as tkfont

import config

HOST = "127.0.0.1"
PORT = config.OVERLAY_PORT


class OverlayWindow:
    BG       = "#1a1b26"
    FG       = "#c0caf5"
    HL_BG    = "#e0af68"
    HL_FG    = "#1a1b26"
    BORDER   = "#414868"
    FONT_FAM = "Segoe UI"
    FONT_SZ  = 13
    PAD      = 16
    HEIGHT   = 210
    W_FRAC   = 0.52
    ALPHA    = 0.93

    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.title("")
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.attributes("-alpha", 0.0)
        self.root.configure(bg=self.BORDER)

        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        w = max(620, min(1080, int(sw * self.W_FRAC)))
        self._w, self._sw, self._sh = w, sw, sh
        self._reposition(w, self.HEIGHT)

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
        self._txt.pack(fill=tk.BOTH, expand=True, pady=(30, 0))
        self._txt.tag_configure(
            "hl", background=self.HL_BG, foreground=self.HL_FG,
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

        # Transport buttons — top-left corner. ⏯ mirrors the daemon's state
        # (STATE:paused / STATE:playing messages).
        self._btn_pause = None
        x = 12
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

        # Keyboard shortcut hint — left of the close button
        hint = tk.Label(
            inner,
            text="Ctrl+Alt+Space pause  ·  Ctrl+Alt+←/→ skip  ·  ESC stop",
            font=(self.FONT_FAM, 9),
            bg=self.BG, fg="#6272a4",
            cursor="arrow",
        )
        hint.place(relx=1.0, rely=0.0, anchor="ne", x=-34, y=8)

        # Drag support; a release without movement on the text is a seek-click.
        for w in (self.root, inner, self._txt):
            w.bind("<Button-1>", self._drag_start)
            w.bind("<B1-Motion>", self._drag_move)
        self._txt.bind("<ButtonRelease-1>", self._txt_release)
        self._dx = self._dy = 0
        self._px = self._py = 0  # press origin, to tell a click from a drag

        self._alpha   = 0.0
        self._fade_id = None
        self._full    = ""
        self._from    = 0

        threading.Thread(target=self._serve, daemon=True).start()

    # ── positioning ──────────────────────────────────────────────────────
    def _reposition(self, w: int, h: int) -> None:
        x = (self._sw - w) // 2
        y = (self._sh - h) // 2
        self.root.geometry(f"{w}x{h}+{x}+{y}")

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

    def _drag_move(self, e: tk.Event) -> None:
        x = self.root.winfo_x() + e.x_root - self._dx
        y = self.root.winfo_y() + e.y_root - self._dy
        self.root.geometry(f"+{x}+{y}")
        self._dx, self._dy = e.x_root, e.y_root

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
            chunk = base64.b64decode(line[6:]).decode("utf-8")
            self.root.after(0, lambda c=chunk: self._highlight(c))
        elif line.startswith("STATE:"):
            state = line[6:]
            self.root.after(0, lambda s=state: self._set_state(s))

    # ── UI updates (main thread only) ─────────────────────────────────────
    def _set_state(self, state: str) -> None:
        if self._btn_pause is not None:
            self._btn_pause.configure(text="▶" if state == "paused" else "⏸")

    def _show(self, text: str) -> None:
        self._full = text
        self._from = 0
        self._txt.configure(state=tk.NORMAL)
        self._txt.delete("1.0", tk.END)
        self._txt.insert("1.0", text)
        self._txt.tag_remove("hl", "1.0", tk.END)
        self._txt.configure(state=tk.DISABLED)
        self._txt.yview_moveto(0.0)
        self._fade_in()

    def _highlight(self, chunk: str) -> None:
        pos = self._full.find(chunk, self._from)
        if pos == -1:
            pos = self._full.find(chunk)
        if pos == -1:
            return
        end = pos + len(chunk)
        self._from = end
        self._txt.tag_remove("hl", "1.0", tk.END)
        si = f"1.0+{pos}c"
        ei = f"1.0+{end}c"
        self._txt.tag_add("hl", si, ei)
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
