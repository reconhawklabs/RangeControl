"""Application shell: notebook, status bar, queue pump, and first-run bootstrap.

This is the integration point for every other GUI module. It owns:

- ``bootstrap`` -- creating a fresh app home's ``resources/``, starter
  ``.env``, and ``PREGENERATE.md``.
- ``App`` -- the Tk window: two tabs, a persistent status bar, a debounced
  autosave for the settings form, and the 100ms queue pump that is the only
  sanctioned path from a worker thread into a widget.
- ``run_gui`` -- process entry point used by ``python -m rangecontrol`` and
  by the frozen binary.

Money-safety is the load-bearing invariant here: opening the window, loading
an existing ``.env``, and switching tabs must never reach a Provider. Only
the Generate/Regenerate button (behind a confirmation dialog) and an
already-running bot answering a real Discord question ever do.
"""

from __future__ import annotations

import contextlib
import logging
import os
import queue
import subprocess
import sys
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

from rangecontrol.advisor.engine import Advisor
from rangecontrol.config import (
    DEFAULT_MODELS,
    VALID_PROVIDERS,
    Config,
    ConfigError,
    load_config,
)
from rangecontrol.gui.bundled import read_bundled
from rangecontrol.gui.console_tab import ConsoleTab
from rangecontrol.gui.env_file import ENV_FILENAME, read_env, update_env
from rangecontrol.gui.events import (
    ERROR,
    INGEST,
    LOG,
    MODELS,
    PENDING,
    QUESTION,
    STATUS,
    QueueAuditLog,
    QueueLogHandler,
)
from rangecontrol.gui.paths import PathError, app_home, ensure_app_home
from rangecontrol.gui.runtime import (
    ModelsJob,
    RUNNING,
    STARTING,
    STOPPING,
    BotController,
    BotState,
    IngestJob,
    IngestOutcome,
)
from rangecontrol.gui.settings import to_config_mapping
from rangecontrol.gui.setup_tab import SetupTab
from rangecontrol.gui.statusbar import StatusBar
from rangecontrol.llm.base import LLMError
from rangecontrol.llm.factory import build_gate_provider, build_provider
from rangecontrol.main import STATE_DIR
from rangecontrol.range_doc.loader import load_range_md

logger = logging.getLogger(__name__)

PUMP_INTERVAL_MS = 100
SAVE_DEBOUNCE_MS = 1000

# BotState values for which the setup tab must present the bot as "running"
# for gating purposes (Generate disabled, Start Bot relabelled Stop Bot).
# Translated here, not inside SetupTab.set_bot_running: that widget takes a
# plain boolean on purpose and must not learn about BotState.
_ACTIVE_BOT_STATES = (STARTING, RUNNING, STOPPING)

# Config requires DISCORD_BOT_TOKEN and WHITE_CELL_CHANNEL_ID, but Generate is
# enabled as soon as resources/ has content -- SetupTab does not gate it on
# is_configured(). Neither field is read by prepare() or a provider factory,
# only by BotController.start(), so a harmless placeholder lets validation
# pass for an ingest-only Config without pretending the user filled them in.
_INGEST_PLACEHOLDER_TOKEN = "unset"
_INGEST_PLACEHOLDER_CHANNEL_ID = "0"

_STARTER_ENV = """\
# RangeControl configuration.
# Edited by the app; you can also change values here by hand.

LLM_PROVIDER=gemini
LLM_MODEL=
LLM_API_KEY=
DISCORD_BOT_TOKEN=
WHITE_CELL_CHANNEL_ID=
ALLOWED_CHANNEL_IDS=

# Advanced: a cheaper model for the intent gate. Blank uses LLM_MODEL.
LLM_GATE_MODEL=
"""

# Startup-load money safety: this message is what an operator sees for any
# resource that would need a fresh extraction while the app is merely
# opening. It names the reason (never wire a real provider in here) and the
# way out (Generate), so both the per-file "unreadable" line in the ingest
# report and any total ingest failure read the same way.
_STARTUP_REFUSAL_MESSAGE = (
    "Opening RangeControl never spends money, so this could not be freshly "
    "extracted from a cold cache. Click Generate to build it."
)


class _RefusingProvider:
    """Satisfies the Provider protocol but refuses every call.

    Used only to load an already-present Range.md at startup. Everything
    already in the content-addressed cache is returned for free by
    build_corpus without ever reaching this object; anything that would
    need a fresh extraction hits this instead of a real provider, so it
    fails loudly rather than silently spending an API call the operator
    never asked for.
    """

    def complete(
        self, *, system: str, user: str, schema: dict | None = None,
        max_tokens: int = 2000,
    ) -> str:
        # Defensive, not load-bearing: prepare() only calls Provider.complete
        # to generate a fresh Range.md, and _load_existing_range only ever
        # reaches this provider when Range.md already exists, so this branch
        # is never exercised on this path today. Implemented anyway so this
        # class genuinely satisfies the Provider protocol rather than only
        # the one method this path happens to hit.
        raise LLMError(_STARTUP_REFUSAL_MESSAGE)

    def describe_image(self, *, data: bytes, mime_type: str, prompt: str) -> str:
        raise LLMError(_STARTUP_REFUSAL_MESSAGE)


def _refusing_provider_factory(config: Config) -> _RefusingProvider:
    return _RefusingProvider()


CONFIRM_REGENERATE = (
    "Regenerate the range document from resources/?\n\n"
    "This re-reads every resource through the AI, which costs API calls and "
    "takes time. Cached extractions are deleted. Your current Range.md is "
    "backed up first."
)


def bootstrap(home: Path) -> None:
    """Create the files a fresh folder needs. Never overwrites user data."""
    home = Path(home)
    (home / "resources").mkdir(parents=True, exist_ok=True)

    env = home / ENV_FILENAME
    if not env.exists():
        env.write_text(_STARTER_ENV, encoding="utf-8")
        os.chmod(env, 0o600)

    guide = home / "PREGENERATE.md"
    if not guide.exists():
        guide.write_text(read_bundled("PREGENERATE.md"), encoding="utf-8")


def _open_in_file_manager(path: Path) -> None:
    """Best-effort: open ``path`` in the OS file manager.

    Never a reason to interrupt the operator, so any failure is logged, not
    raised or shown as a dialog.
    """
    try:
        if sys.platform == "darwin":
            subprocess.run(["open", str(path)], check=False)
        elif sys.platform.startswith("win"):
            os.startfile(str(path))  # type: ignore[attr-defined]
        else:
            subprocess.run(["xdg-open", str(path)], check=False)
    except OSError as exc:
        logger.warning("could not open %s: %s", path, exc)


class App(tk.Tk):
    """The main window. ``home`` must already exist -- see ``bootstrap``."""

    def __init__(self, *, home: Path) -> None:
        super().__init__()
        self._home = Path(home)
        self._events: "queue.Queue" = queue.Queue()
        self._controller = BotController(self._events)
        self._outcome: IngestOutcome | None = None
        # Counts every ingest request this window has issued (startup load
        # and every Generate/Regenerate click). Stamped onto each IngestJob
        # so _handle_ingest can tell a stale outcome from the current one --
        # see _handle_ingest's guard for why that matters.
        self._ingest_generation = 0
        # True while a request stamped with the current generation is still
        # running on a worker thread -- covers both the startup load and a
        # user-initiated Generate. Set right before an IngestJob starts,
        # cleared in _handle_ingest once its outcome (of either kind) has
        # arrived, so a second Generate click cannot start a second job on
        # top of one already in flight (see _on_generate).
        self._ingest_in_flight = False
        # Which of the two triggers produced the in-flight/most-recent
        # ingest request: startup's _load_existing_range, or a user clicking
        # Generate. _handle_ingest reads this to word a failure accurately --
        # an operator who never touched Generate must never be told
        # "Generate failed".
        self._ingest_is_startup_load = False
        # Set once, at most, during _load_initial_state: an unreadable .env
        # or resources/ is a standing fact about this whole window session
        # (nothing re-reads .env until the operator edits and saves a field,
        # and a locked resources/ does not fix itself), not something a
        # later successful ingest of a cached Range.md resolves. Without
        # this, _handle_ingest's own success branch -- which must still
        # clear an unrelated Generate-failed warning -- would also wipe
        # this one back to blank the moment the startup Range.md load
        # (a separate, independent IngestJob) happens to finish.
        self._startup_config_warning = ""
        self._save_after_id: str | None = None
        self._pump_after_id: str | None = None
        # True only for the duration of set_values() at load time: SetupTab
        # fires on_change once per field it populates, and without this a
        # fresh window would immediately schedule (and eventually write) a
        # save of values the user never typed.
        self._loading = False
        self._last_saved: dict[str, str] = {}

        self.title("RangeControl")
        self.geometry("980x700")
        self.minsize(840, 560)

        self._log_handler = QueueLogHandler(self._events)
        self._log_handler.setLevel(logging.WARNING)
        logging.getLogger("rangecontrol").addHandler(self._log_handler)

        notebook = ttk.Notebook(self)
        self._setup_tab = SetupTab(
            notebook,
            on_change=self._on_field_changed,
            on_generate=self._on_generate,
            on_start_stop=self._on_start_stop,
            on_open_resources=self._on_open_resources,
            on_refresh=self._on_refresh_resources,
            on_fetch_models=self._on_fetch_models,
        )
        self._console_tab = ConsoleTab(notebook)
        notebook.add(self._setup_tab, text="Setup")
        notebook.add(self._console_tab, text="Console")
        notebook.pack(fill="both", expand=True)

        self._status_bar = StatusBar(self)
        self._status_bar.pack(fill="x", side="bottom")

        self._load_initial_state()

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._pump_after_id = self.after(PUMP_INTERVAL_MS, self.pump)

    # -- startup ------------------------------------------------------

    def _load_initial_state(self) -> None:
        bootstrap(self._home)  # idempotent; harmless if App is built directly

        # Opening the window must survive an unreadable .env exactly the way
        # it already survives an unreadable Range.md (see _load_existing_range
        # below): a locked file, an AV scan mid-read, a permissions mistake on
        # a shared folder. The binary ships console=False, so an uncaught
        # OSError here is not a traceback anyone sees -- it is a window that
        # silently never opens. Falling back to empty settings is honest: the
        # operator did not lose their configuration, it just could not be
        # read this time, and every field is still editable and re-savable.
        startup_errors: list[str] = []
        try:
            values = read_env(self._home / ENV_FILENAME)
        except OSError as exc:
            logger.warning(
                "could not read %s in %s: %s", ENV_FILENAME, self._home, exc
            )
            values = {}
            startup_errors.append(f"Could not read {ENV_FILENAME}: {exc}")

        self._loading = True
        try:
            self._setup_tab.set_values(values)
        finally:
            self._loading = False
        self._last_saved = self._setup_tab.values()

        resources_error = self._refresh_resources_present()
        if resources_error:
            startup_errors.append(resources_error)

        self._status_bar.set_bot_state(self._controller.state)

        if startup_errors:
            # Recorded before the generation-0 _handle_ingest call below so
            # its own success branch (see _handle_ingest) can re-assert this
            # instead of clearing it -- a later, independent startup ingest
            # of Range.md succeeding says nothing about whether this
            # unrelated read ever worked.
            self._startup_config_warning = " ".join(startup_errors)
            # Reported through the same panel/status-bar path as every other
            # startup failure below (see _handle_ingest) -- generation 0
            # matches the counter's initial value, so this is never mistaken
            # for a stale outcome from a later request.
            self._ingest_is_startup_load = True
            self._handle_ingest(
                IngestOutcome(
                    ok=False,
                    error=" ".join(startup_errors),
                    generation=self._ingest_generation,
                )
            )

        self._load_existing_range()

    def _load_existing_range(self) -> None:
        """Load a Range.md that already exists in the app home, for free.

        Covers both the documented pre-generation workflow (an external
        coding agent builds Range.md and the cache, then the operator "just
        starts the bot") and restarting mid-exercise after a crash. Without
        this, Start Bot stays disabled and the button reads "Generate" even
        though nothing needs generating, and the only way forward was a
        Regenerate that clears the cache and spends API calls.

        Runs a real IngestJob with regenerate=False so every resource
        already in the content-addressed cache loads at no cost, but the
        provider it is handed refuses every call: opening the window must
        never spend money, so anything that would require a fresh
        extraction fails loudly (per-resource, in the ingest report) rather
        than quietly billing the user. A home with no Range.md at all takes
        none of this path and behaves exactly as before.

        The Config carries the real range_dir with placeholder credentials,
        the same shape main.py's --verify-bundle path uses: nothing here
        ever authenticates, because the refusing provider never calls out,
        so a user who has not yet filled in an API key must still be able
        to load a pre-generated range.

        Reading Range.md itself must not be allowed to raise out of here:
        this runs synchronously during __init__, on the Tk main thread,
        before the window's own protocol handlers exist to clean anything
        up -- an uncaught OSError (a locked file, an antivirus scan mid-
        read, a network-drive hiccup, another process holding it open)
        would kill the window before it ever appears, leaking the Tk root
        `super().__init__()` already created. That is exactly the
        "restarting mid-exercise" scenario this method exists to rescue,
        so it is handled the same way every sibling read of this file
        already is (prepare() and IngestJob.run() both catch OSError):
        reported through the existing ingest-failure path rather than left
        to crash.

        Marks itself busy for the whole synchronous lookup plus however long
        the background IngestJob (if one starts) takes: without this, a
        Generate click landing in that window reads _outcome as still None
        and silently runs prepare(regenerate=False) instead of the rebuild
        its own confirmation dialog just promised (see _on_generate's guard).
        """
        self._setup_tab.set_busy(True, "Loading range…")
        try:
            existing = load_range_md(self._home)
        except OSError as exc:
            logger.warning("could not read Range.md in %s: %s", self._home, exc)
            self._ingest_is_startup_load = True
            self._handle_ingest(
                IngestOutcome(
                    ok=False,
                    error=f"Could not read Range.md: {exc}. Generate will rebuild it.",
                    generation=self._ingest_generation,
                )
            )
            return
        if existing is None:
            self._setup_tab.set_busy(False)
            return

        config = Config(
            discord_bot_token="unset",
            llm_provider=VALID_PROVIDERS[0],
            llm_api_key="unset",
            llm_model=DEFAULT_MODELS[VALID_PROVIDERS[0]],
            llm_gate_model=DEFAULT_MODELS[VALID_PROVIDERS[0]],
            white_cell_channel_id=0,
            allowed_channel_ids=(),
            range_dir=self._home,
            human_in_the_loop=False,
            extra_instructions="",
        )
        self._ingest_is_startup_load = True
        self._ingest_in_flight = True
        self._ingest_generation += 1
        IngestJob(
            config, self._events, regenerate=False,
            provider_factory=_refusing_provider_factory,
            generation=self._ingest_generation,
        ).start()

    def _refresh_resources_present(self) -> str | None:
        """Update the resources-present flag.

        Returns an error message on an unreadable resources/ directory
        rather than raising: a locked or permission-denied folder must not
        crash the window before it ever opens, any more than an unreadable
        .env or Range.md may (see the siblings around this method).
        _load_initial_state folds the message into the same startup-error
        report those two already produce; every other caller (after every
        ingest outcome) discards it, which is harmless -- the resources hint
        already reflects "not present" either way.
        """
        resources_dir = self._home / "resources"
        try:
            present = resources_dir.is_dir() and any(resources_dir.iterdir())
        except OSError as exc:
            logger.warning(
                "could not read resources/ in %s: %s", self._home, exc
            )
            self._setup_tab.set_resources_present(False)
            return f"Could not read resources/: {exc}"
        self._setup_tab.set_resources_present(present)
        return None

    # -- autosave -------------------------------------------------------

    def _on_field_changed(self) -> None:
        if self._loading:
            return
        # Propagated on every edit, not just the checkbox's own: SetupTab's
        # on_change carries no information about which field fired, and
        # re-sending the current value is idempotent (ModeSwitch.set is a
        # plain assignment). BotController.set_human_in_the_loop is a no-op
        # when no bot is running, so this is exactly as cheap as gating it
        # would be, without needing a second, field-specific callback.
        self._controller.set_human_in_the_loop(
            self._setup_tab.values().get("HUMAN_IN_THE_LOOP") == "true"
        )
        if self._save_after_id is not None:
            self.after_cancel(self._save_after_id)
        self._save_after_id = self.after(SAVE_DEBOUNCE_MS, self._save_fields)

    def _save_fields(self) -> None:
        self._save_after_id = None
        values = self._setup_tab.values()
        if values == self._last_saved:
            return
        try:
            update_env(self._home / ENV_FILENAME, values)
        except OSError as exc:
            logger.warning("could not save %s: %s", ENV_FILENAME, exc)
            self._status_bar.set_warning(f"Could not save settings: {exc}")
            return
        self._last_saved = values

    # -- queue pump -------------------------------------------------------

    def pump(self) -> None:
        """Drain the event queue once, dispatching by kind.

        Each event is dispatched inside its own try/except: a rendering bug
        in one handler (a malformed record, an unexpected None) must not
        raise past this loop, because that would kill the after() chain and
        freeze every future GUI update while the window keeps looking alive.
        Rescheduling below always runs, regardless of what happened above.
        """
        while True:
            try:
                event = self._events.get_nowait()
            except queue.Empty:
                break
            try:
                self._dispatch(event)
            except Exception:  # noqa: BLE001 - one bad event must not stop the pump
                logger.exception(
                    "GUI event handler failed for kind=%r", getattr(event, "kind", "?")
                )
        self._pump_after_id = self.after(PUMP_INTERVAL_MS, self.pump)

    def _dispatch(self, event) -> None:
        if event.kind == QUESTION:
            self._console_tab.append_question(event.payload)
        elif event.kind == LOG:
            self._console_tab.append_log(event.payload)
        elif event.kind == STATUS:
            self._handle_status(event.payload)
        elif event.kind == INGEST:
            self._handle_ingest(event.payload)
        elif event.kind == ERROR:
            self._status_bar.set_warning(str(event.payload))
        elif event.kind == MODELS:
            self._handle_models(event.payload)
        elif event.kind == PENDING:
            self._status_bar.set_pending(event.payload)
        else:
            logger.warning("unrecognised GUI event kind: %r", event.kind)

    def _handle_status(self, state: BotState) -> None:
        self._status_bar.set_bot_state(state)
        self._setup_tab.set_bot_running(state.state in _ACTIVE_BOT_STATES)

    def _handle_ingest(self, outcome: IngestOutcome) -> None:
        # The startup load and a user-initiated Generate can both be in
        # flight at once (see _load_existing_range and _on_generate): each
        # stamps the request counter's value at the moment it started, and
        # whichever request is newest is authoritative regardless of which
        # one's worker thread happens to finish -- and therefore publish --
        # last. An outcome from any earlier request is discarded here,
        # silently: it was superseded before it ever arrived, so there is
        # nothing new for the operator to see.
        if outcome.generation != self._ingest_generation:
            return
        self._ingest_in_flight = False
        self._setup_tab.set_busy(False)
        if outcome.ok:
            self._outcome = outcome
            self._setup_tab.set_report(outcome.report, error="")
            self._status_bar.set_report(outcome.report)
            # Re-assert rather than unconditionally clear: an unrelated
            # ingest succeeding (this is exactly the startup Range.md load
            # succeeding after an unreadable .env, see _load_initial_state)
            # does not mean the operator's configuration became readable.
            # Blank when nothing is standing, which is every other case.
            self._status_bar.set_warning(self._startup_config_warning)
        else:
            # Keep whatever report a previous success produced: a failed
            # regenerate must not discard a still-valid Range.md/corpus.
            report = self._outcome.report if self._outcome is not None else None
            self._setup_tab.set_report(report, error=outcome.error)
            # Wording must match what actually happened: an operator who
            # never touched Generate (the startup load failed instead) must
            # never read "Generate failed" -- there was no Generate to fail.
            if self._ingest_is_startup_load:
                self._status_bar.set_warning(f"Could not load the range: {outcome.error}")
            else:
                self._status_bar.set_warning(f"Generate failed: {outcome.error}")
        self._refresh_resources_present()

    # -- button handlers -------------------------------------------------

    def _config_mapping(self, values: dict[str, str]) -> dict[str, str]:
        """Merge on-disk-only settings (e.g. LLM_GATE_MODEL) with the form.

        SetupTab.values() only returns the seven FIELDS keys -- LLM_GATE_MODEL
        is deliberately absent from the form (see gui/settings.py) so a user
        who set it by hand must not have it silently discarded every time
        this reads the form. Re-reading .env here rather than caching what
        was loaded at startup also means an edit made to the file while the
        window is open is honoured. Form values win over whatever is on
        disk for the keys the form does track.
        """
        merged = {**read_env(self._home / ENV_FILENAME), **values}
        return to_config_mapping(merged, self._home)

    def _ingest_config(self, values: dict[str, str]) -> Config | None:
        mapping = self._config_mapping(values)
        if not mapping.get("DISCORD_BOT_TOKEN", "").strip():
            mapping = {**mapping, "DISCORD_BOT_TOKEN": _INGEST_PLACEHOLDER_TOKEN}
        if not mapping.get("WHITE_CELL_CHANNEL_ID", "").strip():
            mapping = {
                **mapping, "WHITE_CELL_CHANNEL_ID": _INGEST_PLACEHOLDER_CHANNEL_ID
            }
        try:
            return load_config(mapping)
        except ConfigError as exc:
            messagebox.showerror("RangeControl", str(exc))
            return None

    def _on_generate(self) -> None:
        """Confirm, then start the only code path in this app that spends
        money: an IngestJob talks to the configured LLM provider.

        Refuses outright while an ingest is already in flight -- the startup
        load in particular runs with regenerate hard-coded to False and
        _outcome still None, so a Generate landing in that window would
        silently run prepare(regenerate=False) instead of the rebuild its
        own confirmation dialog just promised. The button is already
        disabled for the same duration (see _load_existing_range's
        set_busy), so this is defense in depth, not the primary guard.
        """
        if self._ingest_in_flight:
            return
        if not messagebox.askyesno("RangeControl", CONFIRM_REGENERATE):
            return

        config = self._ingest_config(self._setup_tab.values())
        if config is None:
            return

        regenerate = self._outcome is not None
        self._ingest_is_startup_load = False
        self._ingest_in_flight = True
        self._setup_tab.set_busy(True, "Generating…")
        self._ingest_generation += 1
        IngestJob(
            config, self._events, regenerate=regenerate,
            generation=self._ingest_generation,
        ).start()

    def _on_start_stop(self) -> None:
        if self._controller.state.state in _ACTIVE_BOT_STATES:
            self._controller.stop()
            return

        if self._outcome is None or not self._outcome.ok:
            messagebox.showerror(
                "RangeControl", "Generate the range document before starting the bot."
            )
            return

        mapping = self._config_mapping(self._setup_tab.values())
        try:
            config = load_config(mapping)
        except ConfigError as exc:
            messagebox.showerror("RangeControl", str(exc))
            return

        try:
            provider = build_provider(config)
            gate_provider = build_gate_provider(config)
        except Exception as exc:  # noqa: BLE001 - surface any construction failure
            messagebox.showerror("RangeControl", f"Could not start the bot: {exc}")
            return

        advisor = Advisor(
            provider,
            gate_provider,
            self._outcome.range_md,
            self._outcome.corpus_text,
            # Operator guidance goes only to the ruling stage. The intent gate
            # stays context-free -- that is what makes a reconnaissance
            # question safe for it to classify.
            extra_instructions=config.extra_instructions,
        )
        audit = QueueAuditLog(self._home / STATE_DIR, self._events)
        try:
            self._controller.start(config=config, advisor=advisor, audit=audit)
        except RuntimeError as exc:
            messagebox.showerror("RangeControl", str(exc))

    def _on_refresh_resources(self) -> None:
        """Re-scan the app home after the user has changed it.

        Dropping files into resources/ used to leave Generate dull until a
        restart, because the folder was only ever read during startup. The
        hint asks the user to put material there, so the window has to be
        able to notice that they did.

        Also picks up a Range.md handed over while the window is open — the
        pre-generation workflow produces one externally — but only when no
        ingest is already running, so a refresh cannot race a Generate the
        user started a moment earlier.
        """
        self._refresh_resources_present()
        if self._outcome is None and not self._ingest_in_flight:
            self._load_existing_range()

    def _on_fetch_models(self) -> None:
        """Ask the provider which models this key can actually reach.

        The hardcoded suggestions go stale the day a provider ships a new
        model, and a stale list is worse than none — an operator picks a name
        that no longer exists and only finds out mid-exercise. This is a
        metadata call, not a generation, so it consumes no tokens.
        """
        values = self._setup_tab.values()
        if not values.get("LLM_API_KEY", "").strip():
            self._setup_tab.set_report(
                self._outcome.report if self._outcome else None,
                error="the provider decides which models that key can reach.",
                error_label="Enter an API key first",
            )
            return

        try:
            config = load_config(self._config_mapping(values))
        except ConfigError as exc:
            self._setup_tab.set_report(
                self._outcome.report if self._outcome else None,
                error=str(exc), error_label="Cannot fetch models",
            )
            return

        self._setup_tab.set_fetch_busy(True)
        ModelsJob(config, self._events).start()

    def _handle_models(self, outcome) -> None:
        self._setup_tab.set_fetch_busy(False)
        if not outcome.ok:
            self._setup_tab.set_report(
                self._outcome.report if self._outcome else None,
                error=outcome.error, error_label="Could not fetch models",
            )
            return
        self._setup_tab.set_model_choices(outcome.models)

    def _on_open_resources(self) -> None:
        resources_dir = self._home / "resources"
        resources_dir.mkdir(parents=True, exist_ok=True)
        _open_in_file_manager(resources_dir)

    # -- shutdown ---------------------------------------------------------

    def _on_close(self) -> None:
        try:
            self._controller.stop()
        finally:
            if self._save_after_id is not None:
                self.after_cancel(self._save_after_id)
                self._save_after_id = None
                self._save_fields()  # flush any not-yet-written edit
            if self._pump_after_id is not None:
                self.after_cancel(self._pump_after_id)
                self._pump_after_id = None
            logging.getLogger("rangecontrol").removeHandler(self._log_handler)
            self.destroy()


def _show_fatal_error(message: str) -> None:
    """Report a startup failure the same way the PathError branch always
    has: a plain error dialog on a throwaway root, never a traceback the
    packaged (console=False) binary has nowhere to print."""
    root = tk.Tk()
    root.withdraw()
    messagebox.showerror("RangeControl", message)
    root.destroy()


def _destroy_orphaned_root() -> None:
    """Best-effort cleanup of a Tk root left behind by a failed ``App()``.

    ``App.__init__`` calls ``tk.Tk.__init__`` -- creating the interpreter and
    a real window -- before any of its own code runs, so an exception
    anywhere inside ``__init__`` (including in code added after this
    comment) leaves that window behind with no Python reference to it.
    ``tkinter._default_root`` is how Tkinter itself tracks "the root nothing
    else was told to use", which is exactly this window in a single-window
    app like this one.
    """
    root = getattr(tk, "_default_root", None)
    if root is not None:
        with contextlib.suppress(Exception):
            root.destroy()


def run_gui() -> int:
    """Process entry point for the packaged binary and ``python -m rangecontrol``.

    Nothing on this path may fail silently: the binary ships console=False,
    so an uncaught exception here is not a traceback anyone sees -- it is a
    double-click that produces no window and no error, ever. Constructing
    ``App`` and running its mainloop are each wrapped separately so a
    failure in either is reported the same way ``ensure_app_home``'s
    ``PathError`` already is, rather than only the specific per-field
    guards added elsewhere in this module (see ``_load_initial_state`` and
    ``_load_existing_range``) ever being asked to catch everything.
    """
    logging.basicConfig(
        level=logging.INFO, format="%(levelname)s %(name)s: %(message)s"
    )

    home = app_home()
    try:
        ensure_app_home(home)
        bootstrap(home)
    except PathError as exc:
        _show_fatal_error(str(exc))
        return 1

    try:
        app = App(home=home)
    except Exception as exc:  # noqa: BLE001 - nothing may ever fail silently here
        logger.exception("failed to construct the main window")
        _destroy_orphaned_root()
        _show_fatal_error(f"RangeControl could not start: {exc}")
        return 1

    try:
        app.mainloop()
    except Exception as exc:  # noqa: BLE001 - ditto, once the window exists
        logger.exception("the main loop failed")
        with contextlib.suppress(Exception):
            app.destroy()
        _show_fatal_error(f"RangeControl encountered a fatal error: {exc}")
        return 1
    return 0
