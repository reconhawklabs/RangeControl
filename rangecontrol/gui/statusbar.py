"""Persistent status strip, visible from both tabs."""

from __future__ import annotations

from tkinter import ttk

from rangecontrol.gui.runtime import (
    FAILED,
    RUNNING,
    STARTING,
    STOPPED,
    STOPPING,
    BotState,
)
from rangecontrol.range_doc.report import IngestReport

_INDICATOR = {
    STOPPED: ("○", "Bot stopped"),
    STARTING: ("◐", "Connecting"),
    RUNNING: ("●", "Bot running"),
    STOPPING: ("◑", "Bot still shutting down"),
    FAILED: ("✕", "Bot failed"),
}


class StatusBar(ttk.Frame):
    def __init__(self, master) -> None:
        super().__init__(master, padding=(10, 5))
        self._state = ttk.Label(self, text="○ Bot stopped")
        self._stats = ttk.Label(self, text="")
        self._pending = ttk.Label(self, text="")
        self._warning = ttk.Label(self, text="", foreground="#c05050")
        self._state.grid(row=0, column=0, sticky="w")
        self._stats.grid(row=0, column=1, sticky="w", padx=(16, 0))
        self._pending.grid(row=0, column=2, sticky="w", padx=(16, 0))
        self._warning.grid(row=1, column=0, columnspan=3, sticky="w")
        self.columnconfigure(1, weight=1)

    def set_bot_state(self, state: BotState) -> None:
        # An unrecognised state (there should never be one, but a fallback
        # that crashes or renders nothing is worse than a plain label) falls
        # back to a neutral glyph and the raw state string, so the operator
        # sees *something* actionable instead of a dead status bar.
        glyph, label = _INDICATOR.get(state.state, ("○", state.state))
        text = f"{glyph} {label}"
        if state.detail:
            text = f"{text} — {state.detail}"
        self._state.configure(text=text)

    def set_report(self, report: IngestReport | None) -> None:
        if report is None:
            self._stats.configure(text="No Range.md yet")
            return
        self._stats.configure(
            text=(
                f"Injects {report.inject_count}   "
                f"Index {report.index_rows}   "
                f"Context ~{report.approx_context_tokens():,} tok"
            )
        )

    def set_warning(self, text: str) -> None:
        self._warning.configure(text=text)

    def set_pending(self, count: int) -> None:
        """Show how many rulings are held awaiting white cell review.

        Blank at zero rather than "0 awaiting review": the operator should
        see this line appear when it becomes true, not sit there reading
        zero for the entire exercise.
        """
        self._pending.configure(text=f"{count} awaiting review" if count else "")

    def dump(self) -> str:
        """Concatenated label text. Exists for tests; no production caller."""
        return " ".join(
            w.cget("text")
            for w in (self._state, self._stats, self._pending, self._warning)
        )
