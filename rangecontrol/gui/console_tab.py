"""The live white-cell feed.

Shows the internal reason and impacted injects alongside the public reply.
The operator running this window holds the API keys and the bot token - they
are the white cell, and this is their one chance to catch a bad ruling while
the exercise is still running.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk

_COLOURS = {
    "approved": "#6fbf73",
    "denied": "#ff8080",
    "clarify": "#e0c060",
    "deflection": "#9a9a9a",
    "error": "#ff6060",
    "meta": "#7fb3ff",
    "internal": "#b79ae0",
    "log": "#c8a45c",
    # Human-in-the-loop decisions: an approval is a released ruling and a
    # denial is a refusal, so they share the "approved"/"denied" verdict
    # colours rather than inventing a third palette.
    "hitl_approved": "#6fbf73",
    "hitl_denied": "#ff8080",
    # A ruling still awaiting white cell approval. Distinct from every
    # verdict colour above on purpose: none of them may ever look like "this
    # went out" when it did not.
    "held": "#e0a030",
}


class ConsoleTab(ttk.Frame):
    def __init__(self, master) -> None:
        super().__init__(master, padding=0)
        self._text = tk.Text(
            self, wrap="word", state="disabled", relief="flat",
            background="#161616", foreground="#c8c8c8",
            insertbackground="#c8c8c8", font=("TkFixedFont", 10),
            padx=10, pady=8,
        )
        scroll = ttk.Scrollbar(self, orient="vertical", command=self._text.yview)
        self._text.configure(yscrollcommand=scroll.set)
        self._text.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")
        self.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)

        for name, colour in _COLOURS.items():
            self._text.tag_configure(name, foreground=colour)
        self._text.tag_configure("bold", font=("TkFixedFont", 10, "bold"))

    def _write(self, chunk: str, *tags: str) -> None:
        self._text.configure(state="normal")
        # Only follow the tail when the view is already at the bottom, so a
        # record being read does not jump away as new questions arrive.
        at_bottom = self._text.yview()[1] > 0.999
        self._text.insert("end", chunk, tags)
        self._text.configure(state="disabled")
        if at_bottom:
            self._text.see("end")

    def append_question(self, record: dict) -> None:
        stamp = str(record.get("timestamp") or "")[11:19]
        channel_id = record.get("channel_id")
        channel = "?" if channel_id is None else channel_id
        self._write(
            f"\n{stamp}  {record.get('user_name') or '?'}  #{channel}\n",
            "meta",
        )
        self._write(f"  Q: {record.get('question') or ''}\n")

        kind = record.get("kind") or ""
        verdict = record.get("verdict")
        held = bool(record.get("held"))
        if verdict:
            label, tag = verdict, verdict.lower()
        else:
            label, tag = kind.upper(), kind if kind in _COLOURS else "meta"
        confidence = record.get("confidence")
        suffix = f" · {confidence}" if confidence else ""
        if held:
            # This is the entire fix for I1: without it, a held ruling
            # renders identically to one the blue team actually received --
            # this operator's "one window into what the bot is actually
            # telling people" would show a delivered approval that was, in
            # fact, being withheld for white cell sign-off.
            label, tag = f"{label} — HELD", "held"
        self._write(f"  {label}{suffix}\n", tag, "bold")

        if held:
            self._write(
                "  Awaiting white cell approval — not sent to the blue team yet.\n",
                "held",
            )
            self._write(f"  Would say: {record.get('public_response') or ''}\n")
        else:
            self._write(f"  Public: {record.get('public_response') or ''}\n")

        reviewer = record.get("reviewer")
        if reviewer:
            self._write(f"  Reviewer: {reviewer}\n", "meta")

        reason = record.get("internal_reason")
        if reason:
            self._write(f"  Real:   {reason}\n", "internal")
        impacted = record.get("impacted") or []
        if impacted:
            self._write(f"  Hit:    {', '.join(impacted)}\n", "internal")
        error = record.get("error")
        if error:
            self._write(f"  Error:  {error}\n", "error")

    def append_log(self, text: str) -> None:
        self._write(f"  · {text}\n", "log")

    def clear(self) -> None:
        self._text.configure(state="normal")
        self._text.delete("1.0", "end")
        self._text.configure(state="disabled")

    def dump(self) -> str:
        """Full contents. Exists for tests; no production caller."""
        return self._text.get("1.0", "end")
