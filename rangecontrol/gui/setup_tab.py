"""Configuration form and range status panel.

Holds no rules of its own: which fields exist, which are secret, and which
are required all come from gui.settings, so the window cannot drift from what
load_config accepts.
"""

from __future__ import annotations

import math
import tkinter as tk
from tkinter import ttk

from rangecontrol.gui.settings import FIELDS, VALID_PROVIDERS, is_configured, models_for
from rangecontrol.range_doc.report import LARGE_CONTEXT_CHARS, IngestReport

_EMPTY_RESOURCES_HINT = (
    "Put your range material in the resources folder to generate: network "
    "diagrams, MSELs, firewall exports, playbooks, PDFs, spreadsheets. "
    "Then click Refresh Resources."
)

# LLM_PROVIDER is a required field with no fallback in load_config: a blank
# combobox would leave is_configured() false forever, so Start Bot would be
# stuck disabled on a fresh window even after every other field is filled in.
# Defaulting to the first known provider keeps the gate meaningful instead of
# permanently closed.
_DEFAULT_PROVIDER = VALID_PROVIDERS[0]

# HUMAN_IN_THE_LOOP is a checkbox bound to a StringVar with onvalue/offvalue,
# so it round-trips through .env as text like every other field. A blank or
# unrecognised value on load must normalise to the explicit "false" rather
# than "": _parse_bool in config.py has no fallback for "" other than reading
# it as False anyway, but writing "" back to disk would look unset rather
# than deliberately off, and would not match onvalue/offvalue on the next
# load. This mirrors config.py's _TRUE set without importing it: the two are
# allowed to drift only in the direction of this widget accepting more
# spellings than it ever itself writes.
_TRUE_STRINGS = {"1", "true", "yes", "on"}


class SetupTab(ttk.Frame):
    def __init__(self, master, *, on_change, on_generate, on_start_stop,
                 on_open_resources, on_refresh=None,
                 on_fetch_models=None) -> None:
        super().__init__(master, padding=12)
        self._on_change = on_change
        self._vars: dict[str, tk.StringVar] = {}
        self._entries: dict[str, ttk.Widget] = {}
        self._report: IngestReport | None = None
        self._resources_present = False
        self._bot_running = False

        self._on_fetch_models = on_fetch_models
        # Per provider: an Anthropic key cannot reach Gemini models, so a
        # fetched list belongs to the provider it was fetched for.
        self._fetched_models: dict[str, tuple[str, ...]] = {}
        self._multiline_keys: set[str] = set()
        # Guards set_values against its own <<Modified>> events, which
        # would otherwise report a load as an operator edit.
        self._suspend_multiline = False
        self._build_fields()
        self._build_status_panel()
        self._build_buttons(on_generate, on_start_stop, on_open_resources,
                            on_refresh)
        self._sync_model_choices()
        self._refresh_buttons()

    def _build_fields(self) -> None:
        grid = ttk.Frame(self)
        grid.grid(row=0, column=0, sticky="ew")
        # uniform= keeps the two columns exactly equal, so neither can
        # starve the other as FIELDS grows.
        grid.columnconfigure(0, weight=1, uniform="fields")
        grid.columnconfigure(1, weight=1, uniform="fields")

        # Two columns, filled top-to-bottom then left-to-right. The row
        # count per column is derived from how many fields exist rather than
        # hardcoded, so the layout keeps working as FIELDS grows.
        rows_per_column = math.ceil(len(FIELDS) / 2)

        for index, field in enumerate(FIELDS):
            column, row = divmod(index, rows_per_column)
            cell = ttk.Frame(grid, padding=(0, 4, 14, 4))
            cell.grid(row=row, column=column, sticky="ew")
            cell.columnconfigure(0, weight=1)

            label = field.label + ("  (required)" if field.required else "")
            ttk.Label(cell, text=label).grid(row=0, column=0, sticky="w")

            if field.key == "LLM_PROVIDER":
                initial = _DEFAULT_PROVIDER
            elif field.key == "HUMAN_IN_THE_LOOP":
                initial = "false"
            else:
                initial = ""
            var = tk.StringVar(value=initial)
            var.trace_add("write", lambda *_: self._changed())
            self._vars[field.key] = var

            if field.key == "LLM_PROVIDER":
                widget = ttk.Combobox(cell, textvariable=var, state="readonly",
                                      values=VALID_PROVIDERS)
            elif field.key == "LLM_MODEL":
                widget = ttk.Combobox(cell, textvariable=var, values=())
            elif field.multiline:
                # A Text, not an Entry: guidance is a paragraph. Text carries
                # no control variable, so this field's StringVar goes unused
                # and values()/set_values() read the widget itself.
                widget = tk.Text(cell, height=4, wrap="word", undo=True,
                                 relief="solid", borderwidth=1)

                # <<Modified>> is the one event a Text fires for typed and
                # programmatic edits alike, and it latches - the flag has to be
                # cleared or it never fires again.
                def _edited(_event, w=widget):
                    w.edit_modified(False)
                    if not self._suspend_multiline:
                        self._changed()

                widget.bind("<<Modified>>", _edited)
                self._multiline_keys.add(field.key)
            elif field.key == "HUMAN_IN_THE_LOOP":
                # variable, not textvariable: a Checkbutton drives a plain
                # control variable through onvalue/offvalue, unlike Entry and
                # Combobox which display free text. It must stay enabled even
                # while the bot runs -- toggling mid-exercise is the entire
                # point of ModeSwitch -- so no set_bot_running() branch may
                # ever disable it the way Generate is disabled.
                widget = ttk.Checkbutton(cell, variable=var,
                                         onvalue="true", offvalue="false")
            else:
                widget = ttk.Entry(cell, textvariable=var,
                                   show="•" if field.secret else "")
            # A checkbox reads label / description / box: the description
            # explains what ticking it does, so it belongs above the thing you
            # tick, not orphaned underneath it. Text fields keep the usual
            # label / field / hint order, where the hint qualifies what you
            # just typed.
            checkbox = field.key == "HUMAN_IN_THE_LOOP"
            widget_row, hint_row = (2, 1) if checkbox else (1, 2)

            widget.grid(row=widget_row, column=0, sticky="w" if checkbox else "ew")
            self._entries[field.key] = widget

            if field.key == "LLM_MODEL":
                # Beside the field it fills, not in the button row: it belongs
                # to this control, and the hardcoded suggestions go stale the
                # day a provider ships a new model.
                self.fetch_models_button = ttk.Button(
                    cell, text="Fetch", width=7,
                    command=self._on_fetch_models or (lambda: None),
                )
                self.fetch_models_button.grid(row=widget_row, column=1,
                                              padx=(6, 0))

            if field.hint:
                # wraplength so a long hint folds inside the column instead of
                # stretching the window or running off its right edge.
                # wraplength is bound to the cell on <Configure> rather than
                # fixed: a fixed width overflows whenever the column is
                # narrower than it, and the neighbouring column then draws
                # over the overflow -- which clipped the Discord token hint.
                hint_label = ttk.Label(cell, text=field.hint, foreground="#888",
                                       wraplength=300, justify="left")
                cell.bind(
                    "<Configure>",
                    lambda event, lbl=hint_label: lbl.configure(
                        wraplength=max(160, event.width - 18)
                    ),
                )
                hint_label.grid(row=hint_row, column=0, columnspan=2,
                                sticky="ew")

    def _build_status_panel(self) -> None:
        self._status = ttk.LabelFrame(self, text="Range status", padding=10)
        self._status.grid(row=1, column=0, sticky="ew", pady=(14, 0))
        self._status_label = ttk.Label(self._status, text="No Range.md yet",
                                       justify="left")
        self._status_label.grid(row=0, column=0, sticky="w")

    def _build_buttons(self, on_generate, on_start_stop, on_open_resources,
                       on_refresh=None) -> None:
        row = ttk.Frame(self)
        row.grid(row=2, column=0, sticky="ew", pady=(14, 0))
        self.generate_button = ttk.Button(row, text="Generate", command=on_generate)
        self.start_button = ttk.Button(row, text="Start Bot", command=on_start_stop)
        open_button = ttk.Button(row, text="Open resources folder",
                                 command=on_open_resources)
        self.generate_button.grid(row=0, column=0)
        self.start_button.grid(row=0, column=1, padx=(8, 0))
        open_button.grid(row=0, column=2, padx=(8, 0))

        # Never gated. It is the way out of the state the hint describes, so
        # sharing Generate's gate would leave the user told to do something
        # and unable to make the window notice they did it.
        self.refresh_button = ttk.Button(
            row, text="Refresh Resources", command=on_refresh or (lambda: None)
        )
        self.refresh_button.grid(row=0, column=3, padx=(8, 0))

        # wraplength keeps a long hint inside the window instead of running off
        # the right edge; it is re-computed on <Configure> so it tracks resizes.
        self._hint = ttk.Label(row, text="", foreground="#888",
                               wraplength=560, justify="left")
        self._hint.grid(row=1, column=0, columnspan=4, sticky="ew", pady=(6, 0))
        row.columnconfigure(3, weight=1)
        self.bind("<Configure>", self._on_resize)
        self.columnconfigure(0, weight=1)

    def _sync_model_choices(self) -> None:
        """Re-apply the suggestion list for the currently selected provider.

        Runs on every variable write, so it must prefer a list fetched from
        the provider over the hardcoded one: otherwise picking a fetched model
        fires this and immediately throws that list away, leaving the operator
        looking at the two stale defaults again.
        """
        provider = self._vars["LLM_PROVIDER"].get().strip().lower()
        model = self._entries.get("LLM_MODEL")
        if isinstance(model, ttk.Combobox):
            fetched = self._fetched_models.get(provider)
            model.configure(values=fetched if fetched else models_for(provider))

    @property
    def hint_label(self):
        """The wrapped hint label. Exists for tests; no production caller."""
        return self._hint

    def _on_resize(self, event) -> None:
        # A little inset so the text never touches the frame's right edge.
        self._hint.configure(wraplength=max(240, event.width - 40))
        self._status_label.configure(wraplength=max(240, event.width - 60))

    def _changed(self) -> None:
        self._sync_model_choices()
        self._refresh_buttons()
        self._on_change()

    def _refresh_buttons(self) -> None:
        can_generate = self._resources_present and not self._bot_running
        self.generate_button.configure(
            state="normal" if can_generate else "disabled",
            text="Regenerate" if self._report is not None else "Generate",
        )
        ready = is_configured(self.values()) and self._report is not None
        # RUNNING, STARTING, and STOPPING all collapse to "not stopped" for
        # this label: the live nuance between them is StatusBar's job, and a
        # second "Stop Bot" click mid-STOPPING is a harmless re-request, not
        # a lie. Translating a richer BotState into this boolean is the
        # parent's job, not this widget's.
        self.start_button.configure(
            state="normal" if ready else "disabled",
            text="Stop Bot" if self._bot_running else "Start Bot",
        )

        missing_resources = not self._resources_present
        if missing_resources and self._bot_running:
            self._hint.configure(
                text=f"{_EMPTY_RESOURCES_HINT} Also stop the bot before regenerating."
            )
        elif missing_resources:
            self._hint.configure(text=_EMPTY_RESOURCES_HINT)
        elif self._bot_running:
            self._hint.configure(text="Stop the bot before regenerating.")
        else:
            self._hint.configure(text="")

    # -- public API -------------------------------------------------------

    def values(self) -> dict[str, str]:
        out = {key: var.get() for key, var in self._vars.items()}
        for key in self._multiline_keys:
            widget = self._entries.get(key)
            if widget is not None:
                # Text always appends a trailing newline of its own.
                out[key] = widget.get("1.0", "end-1c")
        return out

    def _set_multiline(self, key: str, text: str) -> None:
        widget = self._entries.get(key)
        if widget is None:
            return
        self._suspend_multiline = True
        try:
            widget.delete("1.0", "end")
            if text:
                widget.insert("1.0", text)
            widget.edit_modified(False)
        finally:
            self._suspend_multiline = False

    def set_values(self, values: dict[str, str]) -> None:
        for key, var in self._vars.items():
            value = values.get(key, "")
            if key in self._multiline_keys:
                self._set_multiline(key, value)
                continue
            if key == "LLM_PROVIDER" and not value.strip():
                value = _DEFAULT_PROVIDER
            elif key == "HUMAN_IN_THE_LOOP":
                value = "true" if value.strip().lower() in _TRUE_STRINGS else "false"
            var.set(value)

    def entry_for(self, key: str):
        return self._entries[key]

    def model_choices(self) -> tuple[str, ...]:
        return tuple(self._entries["LLM_MODEL"].cget("values"))

    def set_model_choices(self, models: tuple[str, ...]) -> None:
        """Replace the suggestions with what the provider actually offers.

        The typed value is left alone: an operator who already entered a model
        must not have it changed underneath them by a background fetch.
        """
        provider = self._vars["LLM_PROVIDER"].get().strip().lower()
        self._fetched_models[provider] = tuple(models)
        self._sync_model_choices()

    def set_fetch_busy(self, busy: bool) -> None:
        self.fetch_models_button.configure(
            state="disabled" if busy else "normal",
            text="…" if busy else "Fetch",
        )

    def generate_hint(self) -> str:
        return self._hint.cget("text")

    def status_text(self) -> str:
        return self._status_label.cget("text")

    def set_resources_present(self, present: bool) -> None:
        self._resources_present = present
        self._refresh_buttons()

    def set_bot_running(self, running: bool) -> None:
        self._bot_running = running
        self._refresh_buttons()

    def set_busy(self, busy: bool, text: str = "") -> None:
        if busy:
            self.generate_button.configure(state="disabled")
            self.start_button.configure(state="disabled")
            self._hint.configure(text=text)
        else:
            self._refresh_buttons()

    def set_report(self, report: IngestReport | None, error: str = "",
                   error_label: str = "Ingest failed") -> None:
        """Render the ingest report.

        This panel is the persistent replacement for the CLI's one-shot
        [y/N] gate (see ``range_doc.report.confirm``), so every signal
        ``IngestReport.has_concerns()`` checks must show up here as a ⚠
        line, not just the two (unreadable files, missing sections) that
        happened to already have one -- an empty dependency index or zero
        parsed injects looked identical to a healthy range otherwise. None
        of this gates Start Bot: the CLI's own gate still let an operator
        past a concern with an explicit "y", so this only has to make each
        one impossible to miss, not block on it.
        """
        self._report = report
        if error:
            # The label is a parameter because this panel now reports more
            # than ingests: "Ingest failed: enter an API key" would be a lie
            # about what the operator was doing.
            self._status_label.configure(text=f"{error_label}: {error}")
        elif report is None:
            self._status_label.configure(text="No Range.md yet")
        else:
            lines = [
                f"Range.md loaded    Resources {report.total_files}    "
                f"Injects {report.inject_count}",
                f"Dependency index {report.index_rows} rows    "
                f"Ruling context ~{report.approx_context_tokens():,} tokens/question",
            ]
            lines += [
                f"⚠ unreadable: {name} — {reason}"
                for name, reason in report.unreadable
            ]
            lines += [f"⚠ missing section: {gap}" for gap in report.range_md_gaps]
            if report.index_rows == 0:
                lines.append(
                    "⚠ Protected Dependency Index is empty — rulings will be guesswork"
                )
            if report.inject_count == 0:
                lines.append(
                    "⚠ no injects were parsed from the MSEL & Inject Catalog"
                )
            if report.context_chars > LARGE_CONTEXT_CHARS:
                lines.append(
                    f"⚠ ruling context is ~{report.approx_context_tokens():,} tokens "
                    "— expect cost and latency per ruling; consider trimming resources/"
                )
            self._status_label.configure(text="\n".join(lines))
        self._refresh_buttons()
