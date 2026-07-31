"""The detail window, styled after Claude's interface.

Tkinter, deliberately: it ships with Python, so the packaged app needs no extra
runtime, and it renders natively on both platforms. It runs as its own process
and reads `live.json`, so it never shares a main thread with rumps'
NSApplication loop or pystray's icon loop.

The layout is a single centred progress ring with the countdown inside it, then
a quiet detail block. That mirrors the tray icon, so the small and large views
read as the same object. Renders at 4 Hz, extrapolating from the document's
age, so the clock is smooth off a 1 Hz feed.
"""

from __future__ import annotations

import sys
import tkinter as tk
from pathlib import Path
from tkinter import font as tkfont

from ..live import (
    LiveState,
    format_clock,
    format_local,
    read,
    urgency,
)
from .theme import font_stack, palette, urgency_colour

REFRESH_MS = 250

RING_SIZE = 188
RING_WIDTH = 10
WINDOW_WIDTH = 348


class Panel:
    def __init__(
        self,
        live_file: Path,
        *,
        window_hours: float = 5.0,
        theme: str = "dark",
    ) -> None:
        self.live_file = live_file
        self.window_hours = window_hours
        self.p = palette(theme)

        self.root = tk.Tk()
        self.root.title("ClaudeClock")
        self.root.configure(bg=self.p.bg)
        self.root.resizable(False, False)
        self.root.attributes("-topmost", True)
        self.root.bind("<Escape>", lambda _e: self.root.destroy())

        self._build()
        self._position()
        self._refresh()

    # -- layout -------------------------------------------------------------

    def _build(self) -> None:
        ui, numeric = font_stack()
        p = self.p

        self.f_wordmark = tkfont.Font(family=ui, size=10, weight="bold")
        self.f_status = tkfont.Font(family=ui, size=11, weight="bold")
        self.f_clock = tkfont.Font(family=numeric, size=33, weight="bold")
        self.f_caption = tkfont.Font(family=ui, size=9)
        self.f_label = tkfont.Font(family=ui, size=10)
        self.f_value = tkfont.Font(family=ui, size=10, weight="bold")

        outer = tk.Frame(self.root, bg=p.bg, padx=24, pady=20)
        outer.pack(fill="both", expand=True)

        # -- header: status on the left, wordmark on the right
        header = tk.Frame(outer, bg=p.bg)
        header.pack(fill="x")

        self.status_label = tk.Label(
            header, text="", font=self.f_status, bg=p.bg, fg=p.muted, anchor="w"
        )
        self.status_label.pack(side="left")

        tk.Label(
            header, text="claudeclock", font=self.f_wordmark,
            bg=p.bg, fg=p.subtle, anchor="e",
        ).pack(side="right")

        # -- the ring
        self.canvas = tk.Canvas(
            outer, width=RING_SIZE, height=RING_SIZE,
            bg=p.bg, highlightthickness=0, bd=0,
        )
        self.canvas.pack(pady=(18, 4))

        inset = RING_WIDTH // 2 + 2
        box = (inset, inset, RING_SIZE - inset, RING_SIZE - inset)

        # Full-circle track, then the progress arc drawn over it.
        self.canvas.create_oval(*box, outline=p.border, width=RING_WIDTH)
        self.arc = self.canvas.create_arc(
            *box, start=90, extent=0, style="arc",
            outline=p.accent, width=RING_WIDTH,
        )
        self.clock_text = self.canvas.create_text(
            RING_SIZE / 2, RING_SIZE / 2 - 6,
            text="--:--:--", font=self.f_clock, fill=p.text,
        )
        self.caption_text = self.canvas.create_text(
            RING_SIZE / 2, RING_SIZE / 2 + 24,
            text="remaining", font=self.f_caption, fill=p.subtle,
        )

        self.pct_label = tk.Label(
            outer, text="", font=self.f_caption, bg=p.bg, fg=p.subtle
        )
        self.pct_label.pack(pady=(2, 16))

        # -- hairline divider
        tk.Frame(outer, bg=p.border, height=1).pack(fill="x", pady=(0, 14))

        # -- detail rows
        self.rows: dict[str, tk.Label] = {}
        for key, caption in (
            ("session_start", "Session start"),
            ("resets_at", "Resets at"),
            ("utilization", "Limit used"),
            ("source", "Source"),
        ):
            row = tk.Frame(outer, bg=p.bg)
            row.pack(fill="x", pady=2)
            tk.Label(
                row, text=caption, font=self.f_label, width=12, anchor="w",
                bg=p.bg, fg=p.muted,
            ).pack(side="left")
            value = tk.Label(
                row, text="—", font=self.f_value, anchor="e",
                bg=p.bg, fg=p.text,
            )
            value.pack(side="right")
            self.rows[key] = value

        self.footer = tk.Label(
            outer, text="", font=self.f_caption, anchor="w", justify="left",
            bg=p.bg, fg=p.subtle, wraplength=WINDOW_WIDTH - 56,
        )
        self.footer.pack(anchor="w", pady=(16, 0), fill="x")

    def _position(self) -> None:
        """Open near the top-right, below where the menu bar item sits."""
        self.root.update_idletasks()
        width = max(WINDOW_WIDTH, self.root.winfo_reqwidth())
        height = self.root.winfo_reqheight()
        x = max(0, self.root.winfo_screenwidth() - width - 24)
        y = 38 if sys.platform == "darwin" else 60
        self.root.geometry(f"{width}x{height}+{x}+{y}")

    # -- refresh ------------------------------------------------------------

    def _refresh(self) -> None:
        try:
            self._render(read(self.live_file))
        except Exception:
            pass  # a render glitch must never kill the window
        self.root.after(REFRESH_MS, self._refresh)

    def _render(self, state: LiveState) -> None:
        p = self.p

        if not state.connected:
            self.status_label.config(text="Not connected", fg=p.critical)
            self.canvas.itemconfig(self.arc, extent=0)
            self.canvas.itemconfig(self.clock_text, text="--:--:--", fill=p.subtle)
            self.canvas.itemconfig(
                self.caption_text, text=state.reason or "unavailable", fill=p.subtle
            )
            self.pct_label.config(text="")
            for label in self.rows.values():
                label.config(text="—")
            self.footer.config(text="Start it from the menu bar, or run:  cclock tray")
            return

        seconds = state.remaining_seconds
        level = urgency(seconds)
        colour = urgency_colour(level, p)

        self.status_label.config(text=state.state, fg=colour)

        # A negative extent sweeps clockwise from twelve o'clock, like a clock.
        self.canvas.itemconfig(
            self.arc, extent=-max(0.01, state.progress * 359.9), outline=colour
        )
        self.canvas.itemconfig(self.clock_text, text=format_clock(seconds), fill=p.text)
        self.canvas.itemconfig(
            self.caption_text,
            text="window lapsed" if level == "expired" else "remaining",
            fill=p.subtle,
        )

        used = state.get("utilization_percent")
        bits = [f"{state.progress * 100:.0f}% elapsed"]
        if isinstance(used, (int, float)):
            bits.append(f"{float(used):.0f}% of limit used")
        self.pct_label.config(text="   ·   ".join(bits))

        self.rows["session_start"].config(text=format_local(state.get("session_start")))
        self.rows["resets_at"].config(text=format_local(state.get("resets_at")))
        self.rows["utilization"].config(
            text=f"{float(used):.1f}%" if isinstance(used, (int, float)) else "—"
        )
        self.rows["source"].config(text=str(state.get("source", "—")))

        notes = []
        if state.get("degraded_reason"):
            notes.append(str(state.get("degraded_reason")))
        cycles = state.get("cycles_observed", 0)
        triggers = state.get("triggers_sent", 0)
        notes.append(
            f"{cycles} window{'' if cycles == 1 else 's'} observed"
            f"   ·   {triggers} re-arm{'' if triggers == 1 else 's'} sent"
        )
        self.footer.config(text="\n".join(notes))

    def run(self) -> int:
        self.root.mainloop()
        return 0


def main(live_file: Path, window_hours: float = 5.0, theme: str = "dark") -> int:
    try:
        return Panel(live_file, window_hours=window_hours, theme=theme).run()
    except tk.TclError as exc:
        print(f"could not open a window: {exc}", file=sys.stderr)
        print(
            "Tkinter needs a display. On macOS install python.org Python or "
            "`brew install python-tk`.",
            file=sys.stderr,
        )
        return 1
