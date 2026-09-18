import os
import time

import pytest

from rangecontrol.advisor.engine import Advisor
from rangecontrol.gui.app import App, CONFIRM_REGENERATE, bootstrap, run_gui
from rangecontrol.gui.env_file import read_env, update_env
from rangecontrol.gui.events import Event, LOG, QUESTION, STATUS
from rangecontrol.gui.paths import PathError
from rangecontrol.gui.runtime import (
    FAILED,
    RUNNING,
    STARTING,
    STOPPED,
    STOPPING,
    BotState,
    IngestOutcome,
)
from rangecontrol.range_doc.loader import REQUIRED_SECTIONS
from tests.support.images import write_png


def make_app(tmp_path):
    """Build ``App`` on a bootstrapped home, skipping where there is no display."""
    import tkinter as tk

    bootstrap(tmp_path)
    try:
        return App(home=tmp_path)
    except tk.TclError as exc:  # pragma: no cover - exercised only headless
        pytest.skip(f"no usable display: {exc}")


def write_complete_range(tmp_path):
    """A fully populated app home: what the pre-generation workflow hands off.

    A plain .txt resource never needs a provider (see extract_text), so this
    alone is enough to prove a warm/no-provider-needed load works; tests that
    need a genuinely cold cache entry add their own extra resource on top.
    """
    (tmp_path / "resources").mkdir(parents=True, exist_ok=True)
    (tmp_path / "resources" / "notes.txt").write_text("segment A", encoding="utf-8")
    complete = "\n".join(f"## {name}\n\ncontent\n" for name in REQUIRED_SECTIONS)
    (tmp_path / "Range.md").write_text(complete, encoding="utf-8")


def pump_until(app, predicate, timeout=10.0):
    """Drain the queue on a short poll loop until predicate() is true.

    The startup ingest load runs on a real background thread (see
    App._load_existing_range), so its outcome arrives on app._events some
    unknown short time after construction -- a fixed number of app.pump()
    calls would be timing-dependent, exactly the kind of flake this repo
    tries hard to avoid.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.pump()
        if predicate():
            return
        time.sleep(0.02)
    assert predicate(), f"condition not met within {timeout}s"


# -- startup: load an existing Range.md without spending ----------------------


def test_generate_is_disabled_during_the_startup_load(tmp_path):
    """Before this fix, _outcome stayed None until the background job
    published its outcome, so Generate read as armed (and _on_generate would
    silently run prepare(regenerate=False)) for the entire in-between
    window. Nothing here calls app.pump(), so the real background job's
    outcome -- however fast it lands on the queue -- is never drained,
    proving set_busy(True) was applied synchronously before the worker
    thread was even started."""
    write_complete_range(tmp_path)
    app = make_app(tmp_path)
    try:
        assert str(app._setup_tab.generate_button.cget("state")) == "disabled"
        assert app._ingest_in_flight is True
    finally:
        pump_until(app, lambda: app._outcome is not None)
        app.destroy()


def test_generate_is_a_no_op_while_the_startup_load_is_in_flight(tmp_path, monkeypatch):
    write_complete_range(tmp_path)
    app = make_app(tmp_path)
    try:
        asked = []
        monkeypatch.setattr(
            "rangecontrol.gui.app.messagebox.askyesno",
            lambda *a, **k: asked.append(1) or True,
        )

        def boom(*args, **kwargs):
            raise AssertionError(
                "a second ingest job started while the startup load was in flight"
            )

        monkeypatch.setattr("rangecontrol.gui.app.IngestJob", boom)

        app._on_generate()  # must be a no-op: refused before the confirm dialog

        assert asked == []
    finally:
        pump_until(app, lambda: app._outcome is not None)
        app.destroy()


def test_an_existing_range_md_is_loaded_at_startup(tmp_path):
    """The pre-generation workflow hands us a Range.md; Start Bot must work."""
    write_complete_range(tmp_path)
    app = make_app(tmp_path)
    try:
        pump_until(app, lambda: app._outcome is not None)
        assert app._outcome.ok is True
        assert app._setup_tab.generate_button.cget("text") == "Regenerate"
    finally:
        app.destroy()


def test_startup_load_never_calls_a_provider(tmp_path, monkeypatch):
    """Opening the app must not spend money, even to populate the stats."""
    write_complete_range(tmp_path)
    called = []
    monkeypatch.setattr(
        "rangecontrol.gui.app.build_provider",
        lambda cfg: called.append(1) or object(),
    )
    app = make_app(tmp_path)
    try:
        pump_until(app, lambda: app._outcome is not None)
        assert called == []
    finally:
        app.destroy()


def test_an_uncached_image_does_not_spend_and_reports_clearly(tmp_path):
    """A cold cache must surface as a message, never as a silent API call."""
    write_complete_range(tmp_path)
    write_png((tmp_path / "resources" / "diagram.png"))
    app = make_app(tmp_path)
    try:
        pump_until(app, lambda: app._outcome is not None)
        assert "Generate" in app._setup_tab.status_text()
    finally:
        app.destroy()


def test_no_range_md_leaves_the_button_reading_generate(tmp_path):
    app = make_app(tmp_path)
    try:
        for _ in range(5):
            app.pump()
        assert app._outcome is None
        assert app._setup_tab.generate_button.cget("text") == "Generate"
    finally:
        app.destroy()


def test_an_unreadable_range_md_does_not_crash_the_app(tmp_path):
    """A locked file, an AV scan mid-read, or a network-drive hiccup must
    not take the whole window down -- that is exactly the "restarting
    mid-exercise" scenario this feature exists to rescue, not a new way to
    break it."""
    if os.geteuid() == 0:
        pytest.skip("root ignores permission bits")

    write_complete_range(tmp_path)
    range_md = tmp_path / "Range.md"
    range_md.chmod(0o000)
    try:
        app = make_app(tmp_path)  # must not raise
        try:
            assert app._outcome is None
            assert app._setup_tab.generate_button.cget("text") == "Generate"
        finally:
            app.destroy()
    finally:
        range_md.chmod(0o644)


def test_a_stale_ingest_outcome_does_not_overwrite_a_newer_one(tmp_path):
    """The startup load and a Generate click can both be in flight at once.
    Whichever request is newest must win, regardless of which one's worker
    thread happens to publish its outcome last."""
    from rangecontrol.range_doc.report import IngestReport

    app = make_app(tmp_path)
    try:
        # Simulate two ingest requests having been issued -- generation 2 is
        # the current (newest) one, e.g. Generate was clicked right after
        # the startup load began.
        app._ingest_generation = 2

        stale_report = IngestReport(
            total_files=1, by_kind=(("text", 1),), unreadable=(),
            range_md_generated=True, range_md_gaps=(), index_rows=1,
            inject_count=1, context_chars=10,
        )
        fresh_report = IngestReport(
            total_files=5, by_kind=(("text", 5),), unreadable=(),
            range_md_generated=True, range_md_gaps=(), index_rows=5,
            inject_count=5, context_chars=500,
        )

        fresh = IngestOutcome(
            ok=True, report=fresh_report, range_md="FRESH", corpus_text="FRESH",
            generation=2,
        )
        app._handle_ingest(fresh)  # the current request resolves first

        stale = IngestOutcome(
            ok=True, report=stale_report, range_md="STALE", corpus_text="STALE",
            generation=1,
        )
        app._handle_ingest(stale)  # a superseded request resolves after

        assert app._outcome is fresh
        assert app._outcome.range_md == "FRESH"
    finally:
        app.destroy()


def test_bootstrap_creates_the_resources_folder(tmp_path):
    bootstrap(tmp_path)
    assert (tmp_path / "resources").is_dir()


def test_bootstrap_writes_a_starter_env(tmp_path):
    bootstrap(tmp_path)
    env = tmp_path / ".env"
    assert env.is_file()
    assert "LLM_PROVIDER" in env.read_text(encoding="utf-8")


def test_bootstrap_writes_the_pregeneration_guide(tmp_path):
    """It exists to be handed to a coding agent, so it must be a real file."""
    bootstrap(tmp_path)
    assert (tmp_path / "PREGENERATE.md").is_file()


def test_bootstrap_never_overwrites_an_existing_env(tmp_path):
    (tmp_path / ".env").write_text("LLM_API_KEY=mine\n", encoding="utf-8")
    bootstrap(tmp_path)
    assert read_env(tmp_path / ".env")["LLM_API_KEY"] == "mine"


def test_bootstrap_is_idempotent(tmp_path):
    bootstrap(tmp_path)
    bootstrap(tmp_path)
    assert (tmp_path / "resources").is_dir()


def test_the_template_is_not_written_into_the_app_home(tmp_path):
    """It is an input to the generator, not something the user edits."""
    bootstrap(tmp_path)
    assert not (tmp_path / "RangeTemplate.md").exists()


def test_the_starter_env_matches_the_brief_verbatim(tmp_path):
    """Guards against silent drift from the brief's literal starter text."""
    literal_brief_text = (
        "# RangeControl configuration.\n"
        "# Edited by the app; you can also change values here by hand.\n"
        "\n"
        "LLM_PROVIDER=gemini\n"
        "LLM_MODEL=\n"
        "LLM_API_KEY=\n"
        "DISCORD_BOT_TOKEN=\n"
        "WHITE_CELL_CHANNEL_ID=\n"
        "ALLOWED_CHANNEL_IDS=\n"
        "\n"
        "# Advanced: a cheaper model for the intent gate. Blank uses LLM_MODEL.\n"
        "LLM_GATE_MODEL=\n"
    )
    bootstrap(tmp_path)
    assert (tmp_path / ".env").read_text(encoding="utf-8") == literal_brief_text


# -- App construction and first-run wiring ------------------------------------


def test_app_opens_on_setup_with_two_tabs(tmp_path):
    app = make_app(tmp_path)
    try:
        notebook = app._setup_tab.master
        tab_ids = notebook.tabs()
        assert len(tab_ids) == 2
        assert notebook.tab(tab_ids[0], "text") == "Setup"
        assert notebook.tab(tab_ids[1], "text") == "Console"
        # Opens on Setup: index 0 is selected by default.
        assert notebook.index(notebook.select()) == 0
    finally:
        app.destroy()


def test_status_bar_reads_bot_stopped_on_a_fresh_window(tmp_path):
    app = make_app(tmp_path)
    try:
        assert "stopped" in app._status_bar.dump().lower()
    finally:
        app.destroy()


def test_console_is_empty_on_a_fresh_window(tmp_path):
    app = make_app(tmp_path)
    try:
        assert app._console_tab.dump().strip() == ""
    finally:
        app.destroy()


def test_generate_is_enabled_once_resources_have_content(tmp_path):
    app = make_app(tmp_path)
    try:
        (tmp_path / "resources" / "note.txt").write_text("segment A", encoding="utf-8")
        app._refresh_resources_present()
        assert str(app._setup_tab.generate_button.cget("state")) == "normal"
    finally:
        app.destroy()


def test_start_bot_is_disabled_until_fields_are_filled(tmp_path):
    app = make_app(tmp_path)
    try:
        assert str(app._setup_tab.start_button.cget("state")) == "disabled"
    finally:
        app.destroy()


# -- carried-forward item 1: BotState -> set_bot_running boolean --------------


@pytest.mark.parametrize("state", [RUNNING, STARTING, STOPPING])
def test_active_bot_states_map_to_bot_running_true(tmp_path, state):
    app = make_app(tmp_path)
    try:
        app._events.put(Event(kind=STATUS, payload=BotState(state)))
        app.pump()
        assert app._setup_tab._bot_running is True
    finally:
        app.destroy()


@pytest.mark.parametrize("state", [STOPPED, FAILED])
def test_inactive_bot_states_map_to_bot_running_false(tmp_path, state):
    app = make_app(tmp_path)
    try:
        # Flip true first so the transition to false is actually exercised.
        app._events.put(Event(kind=STATUS, payload=BotState(RUNNING)))
        app.pump()
        app._events.put(Event(kind=STATUS, payload=BotState(state)))
        app.pump()
        assert app._setup_tab._bot_running is False
    finally:
        app.destroy()


# -- carried-forward item 2: loading .env must not rewrite it -----------------


def test_loading_an_existing_env_does_not_schedule_a_save(tmp_path):
    bootstrap(tmp_path)
    update_env(tmp_path / ".env", {"LLM_API_KEY": "a-real-key", "LLM_PROVIDER": "anthropic"})
    before = (tmp_path / ".env").read_bytes()

    app = None
    try:
        import tkinter as tk

        try:
            app = App(home=tmp_path)
        except tk.TclError as exc:
            pytest.skip(f"no usable display: {exc}")

        assert app._save_after_id is None
        after = (tmp_path / ".env").read_bytes()
        assert after == before
        assert app._setup_tab.values()["LLM_API_KEY"] == "a-real-key"
    finally:
        if app is not None:
            app.destroy()


def test_editing_a_field_schedules_a_pending_save(tmp_path):
    app = make_app(tmp_path)
    try:
        assert app._save_after_id is None
        app._setup_tab._vars["LLM_API_KEY"].set("typed-value")
        assert app._save_after_id is not None

        app.after_cancel(app._save_after_id)
        app._save_after_id = None
        app._save_fields()
        assert read_env(tmp_path / ".env")["LLM_API_KEY"] == "typed-value"
    finally:
        app.destroy()


def test_a_burst_of_edits_writes_once(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    try:
        cancels = []
        real_after_cancel = app.after_cancel

        def spy_cancel(job_id):
            cancels.append(job_id)
            return real_after_cancel(job_id)

        monkeypatch.setattr(app, "after_cancel", spy_cancel)

        app._setup_tab._vars["LLM_API_KEY"].set("a")
        app._setup_tab._vars["LLM_API_KEY"].set("ab")
        app._setup_tab._vars["LLM_API_KEY"].set("abc")

        assert len(cancels) == 2  # the first two pending saves were cancelled

        app.after_cancel(app._save_after_id)
        app._save_after_id = None
        app._save_fields()
        assert read_env(tmp_path / ".env")["LLM_API_KEY"] == "abc"
    finally:
        app.destroy()


def test_unchanged_values_are_not_rewritten(tmp_path):
    app = make_app(tmp_path)
    try:
        before = (tmp_path / ".env").read_bytes()
        # Re-saving exactly what was loaded must be a no-op.
        app._save_fields()
        after = (tmp_path / ".env").read_bytes()
        assert after == before
    finally:
        app.destroy()


# -- carried-forward item 3: the pump must survive a raising handler ----------


def test_pump_survives_a_raising_handler_and_keeps_processing(tmp_path):
    app = make_app(tmp_path)
    try:
        # append_question calls .get() on the payload; None raises AttributeError.
        app._events.put(Event(kind=QUESTION, payload=None))
        app._events.put(Event(kind=LOG, payload="still alive"))
        app.pump()  # must not raise
        assert "still alive" in app._console_tab.dump()
    finally:
        app.destroy()


def test_pump_always_reschedules_itself_even_after_a_failure(tmp_path):
    app = make_app(tmp_path)
    try:
        calls = []
        app.after = lambda ms, cb: calls.append((ms, cb)) or "fake-id"
        app._events.put(Event(kind=QUESTION, payload=None))
        app.pump()
        assert calls
        assert calls[0][1] == app.pump
    finally:
        # app.after was overridden with a plain function; restore the bound
        # method before destroy() so Tk's own teardown can still use it.
        del app.after
        app.destroy()


def test_unknown_event_kind_does_not_crash_the_pump(tmp_path):
    app = make_app(tmp_path)
    try:
        app._events.put(Event(kind="mystery", payload="whatever"))
        app._events.put(Event(kind=LOG, payload="after the mystery event"))
        app.pump()
        assert "after the mystery event" in app._console_tab.dump()
    finally:
        app.destroy()


# -- money safety: opening the app must never touch a provider ---------------


def test_constructing_the_app_never_touches_a_provider(tmp_path, monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("a provider was constructed without a user action")

    monkeypatch.setattr("rangecontrol.gui.app.build_provider", boom)
    monkeypatch.setattr("rangecontrol.gui.app.build_gate_provider", boom)
    monkeypatch.setattr("rangecontrol.gui.app.IngestJob", boom)

    app = make_app(tmp_path)
    try:
        for _ in range(3):
            app.pump()
    finally:
        app.destroy()


# -- Generate: confirmation gate ----------------------------------------------


def test_declining_the_confirmation_does_not_start_an_ingest_job(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    try:
        (tmp_path / "resources" / "note.txt").write_text("segment A", encoding="utf-8")
        app._refresh_resources_present()

        monkeypatch.setattr("rangecontrol.gui.app.messagebox.askyesno", lambda *a, **k: False)

        def boom(*args, **kwargs):
            raise AssertionError("IngestJob started despite a declined confirmation")

        monkeypatch.setattr("rangecontrol.gui.app.IngestJob", boom)

        app._on_generate()  # must return early, before ever touching IngestJob
    finally:
        app.destroy()


def test_confirming_generate_asks_the_exact_brief_text(tmp_path, monkeypatch):
    """Guards against silent drift from the brief's literal confirmation text,
    not just self-consistency with the CONFIRM_REGENERATE constant."""
    literal_brief_text = (
        "Regenerate the range document from resources/?\n\n"
        "This re-reads every resource through the AI, which costs API calls and "
        "takes time. Cached extractions are deleted. Your current Range.md is "
        "backed up first."
    )
    assert CONFIRM_REGENERATE == literal_brief_text

    app = make_app(tmp_path)
    try:
        seen = {}

        def fake_askyesno(title, message):
            seen["title"] = title
            seen["message"] = message
            return False

        monkeypatch.setattr("rangecontrol.gui.app.messagebox.askyesno", fake_askyesno)
        app._on_generate()
        assert seen["message"] == literal_brief_text
    finally:
        app.destroy()


class _RecordingIngestJob:
    """Stands in for IngestJob so tests never touch a real provider."""

    instances: list = []

    def __init__(
        self, config, events, *, regenerate, provider_factory=None, generation=0
    ) -> None:
        self.config = config
        self.events = events
        self.regenerate = regenerate
        self.generation = generation
        self.started = False
        _RecordingIngestJob.instances.append(self)

    def start(self):
        self.started = True


def test_confirmed_generate_builds_a_config_and_starts_ingest(tmp_path, monkeypatch):
    _RecordingIngestJob.instances = []
    app = make_app(tmp_path)
    try:
        (tmp_path / "resources" / "note.txt").write_text("segment A", encoding="utf-8")
        app._refresh_resources_present()
        app._setup_tab.set_values({"LLM_PROVIDER": "anthropic", "LLM_API_KEY": "k"})

        monkeypatch.setattr("rangecontrol.gui.app.messagebox.askyesno", lambda *a, **k: True)
        monkeypatch.setattr("rangecontrol.gui.app.IngestJob", _RecordingIngestJob)

        app._on_generate()

        assert len(_RecordingIngestJob.instances) == 1
        job = _RecordingIngestJob.instances[0]
        assert job.started is True
        assert job.regenerate is False  # first-ever generate, nothing to force
        assert job.config.llm_api_key == "k"
        assert job.config.llm_provider == "anthropic"
        # Discord fields are irrelevant to ingest and were left blank by the
        # user; a harmless placeholder must not leak into anything visible.
        assert job.config.discord_bot_token == "unset"
    finally:
        app.destroy()


def test_generate_reads_llm_gate_model_from_env_not_just_the_form(tmp_path, monkeypatch):
    """LLM_GATE_MODEL is deliberately not one of SetupTab's FIELDS (see
    gui/settings.py), so it can only reach Config by being re-read from disk
    and merged with the form values. A user who set a cheaper gate model by
    hand must not have it silently discarded on every Generate."""
    _RecordingIngestJob.instances = []
    app = make_app(tmp_path)
    try:
        (tmp_path / "resources" / "note.txt").write_text("segment A", encoding="utf-8")
        app._refresh_resources_present()
        app._setup_tab.set_values({"LLM_PROVIDER": "anthropic", "LLM_API_KEY": "k"})
        update_env(tmp_path / ".env", {"LLM_GATE_MODEL": "cheap-model"})

        monkeypatch.setattr("rangecontrol.gui.app.messagebox.askyesno", lambda *a, **k: True)
        monkeypatch.setattr("rangecontrol.gui.app.IngestJob", _RecordingIngestJob)

        app._on_generate()

        job = _RecordingIngestJob.instances[0]
        assert job.config.llm_gate_model == "cheap-model"
    finally:
        app.destroy()


def test_start_reads_llm_gate_model_from_env_not_just_the_form(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    try:
        app._setup_tab.set_values({
            "LLM_PROVIDER": "anthropic", "LLM_API_KEY": "k",
            "DISCORD_BOT_TOKEN": "tok", "WHITE_CELL_CHANNEL_ID": "1",
        })
        update_env(tmp_path / ".env", {"LLM_GATE_MODEL": "cheap-model"})
        app._outcome = IngestOutcome(ok=True, range_md="RANGE", corpus_text="CORPUS")

        seen = {}

        def fake_build_provider(cfg):
            seen["provider_cfg"] = cfg
            return object()

        def fake_build_gate_provider(cfg):
            seen["gate_cfg"] = cfg
            return object()

        def fake_start(*, config, advisor, audit):
            seen["started_config"] = config

        monkeypatch.setattr("rangecontrol.gui.app.build_provider", fake_build_provider)
        monkeypatch.setattr(
            "rangecontrol.gui.app.build_gate_provider", fake_build_gate_provider
        )
        monkeypatch.setattr(app._controller, "start", fake_start)

        app._on_start_stop()

        assert seen["gate_cfg"].llm_gate_model == "cheap-model"
        assert seen["started_config"].llm_gate_model == "cheap-model"
    finally:
        app.destroy()


def test_generate_after_a_successful_outcome_forces_regenerate_true(tmp_path, monkeypatch):
    _RecordingIngestJob.instances = []
    app = make_app(tmp_path)
    try:
        (tmp_path / "resources" / "note.txt").write_text("segment A", encoding="utf-8")
        app._refresh_resources_present()
        app._setup_tab.set_values({"LLM_PROVIDER": "anthropic", "LLM_API_KEY": "k"})
        app._outcome = IngestOutcome(ok=True, range_md="RANGE", corpus_text="CORPUS")

        monkeypatch.setattr("rangecontrol.gui.app.messagebox.askyesno", lambda *a, **k: True)
        monkeypatch.setattr("rangecontrol.gui.app.IngestJob", _RecordingIngestJob)

        app._on_generate()

        job = _RecordingIngestJob.instances[0]
        assert job.regenerate is True
    finally:
        app.destroy()


def test_generate_with_an_invalid_provider_shows_an_error_not_a_crash(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    try:
        (tmp_path / "resources" / "note.txt").write_text("segment A", encoding="utf-8")
        app._refresh_resources_present()
        app._setup_tab.set_values({"LLM_PROVIDER": "anthropic", "LLM_API_KEY": ""})

        monkeypatch.setattr("rangecontrol.gui.app.messagebox.askyesno", lambda *a, **k: True)
        errors = []
        monkeypatch.setattr(
            "rangecontrol.gui.app.messagebox.showerror",
            lambda title, message: errors.append(message),
        )

        def boom(*args, **kwargs):
            raise AssertionError("IngestJob started with an invalid Config")

        monkeypatch.setattr("rangecontrol.gui.app.IngestJob", boom)

        app._on_generate()
        assert errors and "LLM_API_KEY" in errors[0]
    finally:
        app.destroy()


# -- ingest outcome handling ---------------------------------------------------


def test_a_successful_ingest_event_enables_start_and_updates_the_status_bar(tmp_path):
    from rangecontrol.range_doc.report import IngestReport

    app = make_app(tmp_path)
    try:
        app._setup_tab.set_values({
            "LLM_PROVIDER": "anthropic", "LLM_API_KEY": "k",
            "DISCORD_BOT_TOKEN": "t", "WHITE_CELL_CHANNEL_ID": "1",
        })
        report = IngestReport(
            total_files=1, by_kind=(("text", 1),), unreadable=(),
            range_md_generated=True, range_md_gaps=(), index_rows=3,
            inject_count=2, context_chars=100,
        )
        outcome = IngestOutcome(ok=True, report=report, range_md="RANGE", corpus_text="TEXT")
        app._events.put(Event(kind="ingest", payload=outcome))
        app.pump()

        assert app._outcome is outcome
        assert str(app._setup_tab.start_button.cget("state")) == "normal"
        assert "3" in app._status_bar.dump()
    finally:
        app.destroy()


def test_a_failed_ingest_preserves_a_prior_successful_outcome(tmp_path):
    from rangecontrol.range_doc.report import IngestReport

    app = make_app(tmp_path)
    try:
        report = IngestReport(
            total_files=1, by_kind=(("text", 1),), unreadable=(),
            range_md_generated=True, range_md_gaps=(), index_rows=3,
            inject_count=2, context_chars=100,
        )
        good = IngestOutcome(ok=True, report=report, range_md="RANGE", corpus_text="TEXT")
        app._events.put(Event(kind="ingest", payload=good))
        app.pump()

        bad = IngestOutcome(ok=False, error="Anthropic request failed")
        app._events.put(Event(kind="ingest", payload=bad))
        app.pump()

        assert app._outcome is good  # the failed attempt did not overwrite it
        assert "Anthropic request failed" in app._setup_tab.status_text()
    finally:
        app.destroy()


# -- Start/Stop ----------------------------------------------------------------


def test_start_without_a_successful_outcome_shows_an_error_and_does_not_start(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    try:
        errors = []
        monkeypatch.setattr(
            "rangecontrol.gui.app.messagebox.showerror",
            lambda title, message: errors.append(message),
        )

        def boom(**kwargs):
            raise AssertionError("the bot controller started with no ingest outcome")

        monkeypatch.setattr(app._controller, "start", boom)

        app._on_start_stop()
        assert errors
    finally:
        app.destroy()


def test_start_builds_the_advisor_and_audit_from_the_last_successful_outcome(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    try:
        app._setup_tab.set_values({
            "LLM_PROVIDER": "anthropic", "LLM_API_KEY": "k",
            "DISCORD_BOT_TOKEN": "tok", "WHITE_CELL_CHANNEL_ID": "1",
        })
        app._outcome = IngestOutcome(ok=True, range_md="RANGE", corpus_text="CORPUS")

        monkeypatch.setattr("rangecontrol.gui.app.build_provider", lambda cfg: object())
        monkeypatch.setattr("rangecontrol.gui.app.build_gate_provider", lambda cfg: object())

        started = {}

        def fake_start(*, config, advisor, audit):
            started["config"] = config
            started["advisor"] = advisor
            started["audit"] = audit

        monkeypatch.setattr(app._controller, "start", fake_start)

        app._on_start_stop()

        assert started["config"].discord_bot_token == "tok"
        assert isinstance(started["advisor"], Advisor)
    finally:
        app.destroy()


def test_start_stop_toggles_to_stop_when_the_bot_is_active(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    try:
        # Publishing a STATUS event only updates the widgets (what a real
        # controller would cause) -- _on_start_stop reads the controller's
        # own state directly, so that must reflect RUNNING too.
        app._controller._state = BotState(RUNNING)
        app._events.put(Event(kind=STATUS, payload=BotState(RUNNING)))
        app.pump()

        stopped = []
        monkeypatch.setattr(app._controller, "stop", lambda timeout=10.0: stopped.append(True))

        app._on_start_stop()
        assert stopped == [True]
    finally:
        app.destroy()


# -- shutdown: WM_DELETE_WINDOW must stop the bot before destroying the window -


def test_close_stops_the_bot_before_destroying_the_window(tmp_path, monkeypatch):
    import tkinter as tk

    app = make_app(tmp_path)
    order = []
    monkeypatch.setattr(app._controller, "stop", lambda timeout=10.0: order.append("stop"))
    monkeypatch.setattr(app, "destroy", lambda: order.append("destroy"))
    try:
        app._on_close()
        assert order == ["stop", "destroy"]
    finally:
        # The real window is still alive (destroy() was faked out) -- clean it up.
        tk.Tk.destroy(app)


# -- run_gui: PathError must be reported before any window opens --------------


def test_run_gui_reports_a_path_error_without_opening_the_main_window(tmp_path, monkeypatch):
    import tkinter as tk

    monkeypatch.setattr("rangecontrol.gui.app.app_home", lambda: tmp_path)

    def boom(home):
        raise PathError("cannot write there")

    monkeypatch.setattr("rangecontrol.gui.app.ensure_app_home", boom)

    errors = []
    monkeypatch.setattr(
        "rangecontrol.gui.app.messagebox.showerror",
        lambda title, message: errors.append(message),
    )
    app_created = []
    monkeypatch.setattr("rangecontrol.gui.app.App", lambda **kwargs: app_created.append(kwargs))

    # run_gui() builds a plain tk.Tk() root just to host the error dialog on
    # this path; guard it the same way every other widget test in this file
    # skips rather than fails where there is no usable display.
    try:
        result = run_gui()
    except tk.TclError as exc:
        pytest.skip(f"no usable display: {exc}")

    assert result == 1
    assert errors and "cannot write there" in errors[0]
    assert app_created == []


def test_run_gui_bootstraps_and_launches_the_app(tmp_path, monkeypatch):
    monkeypatch.setattr("rangecontrol.gui.app.app_home", lambda: tmp_path)

    calls = []

    class FakeApp:
        def __init__(self, *, home):
            calls.append(home)

        def mainloop(self):
            calls.append("mainloop")

    monkeypatch.setattr("rangecontrol.gui.app.App", FakeApp)

    result = run_gui()

    assert result == 0
    assert calls == [tmp_path, "mainloop"]
    assert (tmp_path / ".env").is_file()
    assert (tmp_path / "resources").is_dir()


# -- C1: an unreadable .env or resources/ must never crash startup ------------


def test_an_unreadable_env_file_does_not_crash_the_app(tmp_path):
    """Windows operators double-click a console=False binary: a crash before
    the window exists shows nothing at all, not even a dialog. An unreadable
    .env must degrade to empty settings, exactly like an unreadable Range.md
    already degrades to "nothing to load"."""
    if os.geteuid() == 0:
        pytest.skip("root ignores permission bits")

    bootstrap(tmp_path)
    env = tmp_path / ".env"
    env.chmod(0o000)
    try:
        app = make_app(tmp_path)  # must not raise
        try:
            assert app._setup_tab.values()["LLM_API_KEY"] == ""
            assert ".env" in app._setup_tab.status_text()
            assert app._status_bar.dump().strip() != ""
        finally:
            app.destroy()
    finally:
        env.chmod(0o600)


def test_the_env_read_warning_survives_a_successful_startup_ingest(tmp_path):
    """Regression: _load_initial_state reports the .env failure through
    _handle_ingest, but the real startup IngestJob for Range.md (started
    right after, in _load_existing_range) can still succeed on its own --
    the resources/notes.txt file needs no provider. When it does,
    _handle_ingest's success branch used to call set_warning(""),
    silently erasing the .env warning about a second after it appeared and
    leaving the operator with a healthy-looking panel and no statement
    anywhere that their settings could not be read."""
    if os.geteuid() == 0:
        pytest.skip("root ignores permission bits")

    bootstrap(tmp_path)
    write_complete_range(tmp_path)
    env = tmp_path / ".env"
    env.chmod(0o000)
    try:
        app = make_app(tmp_path)
        try:
            assert ".env" in app._status_bar.dump()
            pump_until(app, lambda: app._outcome is not None)
            assert app._outcome.ok is True  # the range itself loaded fine
            assert ".env" in app._status_bar.dump()
        finally:
            app.destroy()
    finally:
        env.chmod(0o600)


def test_an_unreadable_resources_folder_does_not_crash_the_app(tmp_path):
    if os.geteuid() == 0:
        pytest.skip("root ignores permission bits")

    bootstrap(tmp_path)
    resources = tmp_path / "resources"
    resources.chmod(0o000)
    try:
        app = make_app(tmp_path)  # must not raise
        try:
            assert str(app._setup_tab.generate_button.cget("state")) == "disabled"
            assert (
                "resources" in app._setup_tab.status_text().lower()
                or "resources" in app._status_bar.dump().lower()
            )
        finally:
            app.destroy()
    finally:
        resources.chmod(0o700)


def test_a_readable_home_is_unaffected_by_the_new_guards(tmp_path):
    """Control: a normal, readable home must open exactly as before."""
    app = make_app(tmp_path)
    try:
        assert app._setup_tab.values()["LLM_PROVIDER"] != ""
        assert app._status_bar.dump().strip() == "○ Bot stopped"
    finally:
        app.destroy()


def test_run_gui_reports_a_construction_failure_instead_of_crashing_silently(
    tmp_path, monkeypatch
):
    """No startup exception may ever be silent: run_gui is the last line of
    defense for anything the app.py:241/325 guards above do not catch."""
    import tkinter as tk

    monkeypatch.setattr("rangecontrol.gui.app.app_home", lambda: tmp_path)

    def boom(*, home):
        raise RuntimeError("boom during construction")

    monkeypatch.setattr("rangecontrol.gui.app.App", boom)

    errors = []
    monkeypatch.setattr(
        "rangecontrol.gui.app.messagebox.showerror",
        lambda title, message: errors.append(message),
    )

    try:
        result = run_gui()
    except tk.TclError as exc:
        pytest.skip(f"no usable display: {exc}")

    assert result == 1
    assert errors and "boom during construction" in errors[0]


def test_run_gui_destroys_a_partially_built_root_on_construction_failure(
    tmp_path, monkeypatch
):
    """App.__init__ calls tk.Tk.__init__ (creating a real window) before any
    of its own code runs. A failure partway through __init__ must not leave
    that window behind with no Python reference to it."""
    import tkinter as tk

    monkeypatch.setattr("rangecontrol.gui.app.app_home", lambda: tmp_path)

    def boom(self) -> None:
        raise RuntimeError("boom mid-init")

    monkeypatch.setattr("rangecontrol.gui.app.App._load_initial_state", boom)

    errors = []
    monkeypatch.setattr(
        "rangecontrol.gui.app.messagebox.showerror",
        lambda title, message: errors.append(message),
    )

    try:
        result = run_gui()
    except tk.TclError as exc:
        pytest.skip(f"no usable display: {exc}")

    assert result == 1
    assert errors and "boom mid-init" in errors[0]
    assert tk._default_root is None


def test_run_gui_reports_a_mainloop_failure_and_destroys_the_window(
    tmp_path, monkeypatch
):
    import tkinter as tk

    monkeypatch.setattr("rangecontrol.gui.app.app_home", lambda: tmp_path)

    destroyed = []

    class FakeApp:
        def __init__(self, *, home):
            pass

        def mainloop(self):
            raise RuntimeError("mainloop exploded")

        def destroy(self):
            destroyed.append(True)

    monkeypatch.setattr("rangecontrol.gui.app.App", FakeApp)

    errors = []
    monkeypatch.setattr(
        "rangecontrol.gui.app.messagebox.showerror",
        lambda title, message: errors.append(message),
    )

    try:
        result = run_gui()
    except tk.TclError as exc:
        pytest.skip(f"no usable display: {exc}")

    assert result == 1
    assert errors and "mainloop exploded" in errors[0]
    assert destroyed == [True]


# -- error-handling branches ---------------------------------------------------


def test_save_failure_is_reported_not_raised(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    try:
        def boom(path, changes):
            raise OSError("disk full")

        monkeypatch.setattr("rangecontrol.gui.app.update_env", boom)
        app._setup_tab._vars["LLM_API_KEY"].set("typed")
        app._save_fields()  # must not raise
        assert "disk full" in app._status_bar.dump()
    finally:
        app.destroy()


def test_error_event_sets_the_status_bar_warning(tmp_path):
    app = make_app(tmp_path)
    try:
        app._events.put(Event(kind="error", payload="white cell channel unreachable"))
        app.pump()
        assert "white cell channel unreachable" in app._status_bar.dump()
    finally:
        app.destroy()


def test_pending_event_updates_the_status_bar(tmp_path):
    from rangecontrol.gui.events import PENDING

    app = make_app(tmp_path)
    try:
        app._events.put(Event(kind=PENDING, payload=2))
        app.pump()
        assert "2" in app._status_bar.dump()
    finally:
        app.destroy()


def test_start_with_an_invalid_channel_id_shows_a_config_error(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    try:
        app._setup_tab.set_values({
            "LLM_PROVIDER": "anthropic", "LLM_API_KEY": "k",
            "DISCORD_BOT_TOKEN": "tok", "WHITE_CELL_CHANNEL_ID": "not-a-number",
        })
        app._outcome = IngestOutcome(ok=True, range_md="RANGE", corpus_text="CORPUS")

        errors = []
        monkeypatch.setattr(
            "rangecontrol.gui.app.messagebox.showerror",
            lambda title, message: errors.append(message),
        )

        def boom(**kwargs):
            raise AssertionError("the bot started with an invalid Config")

        monkeypatch.setattr(app._controller, "start", boom)

        app._on_start_stop()
        assert errors and "numeric" in errors[0].lower()
    finally:
        app.destroy()


def test_start_reports_a_provider_construction_failure(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    try:
        app._setup_tab.set_values({
            "LLM_PROVIDER": "anthropic", "LLM_API_KEY": "k",
            "DISCORD_BOT_TOKEN": "tok", "WHITE_CELL_CHANNEL_ID": "1",
        })
        app._outcome = IngestOutcome(ok=True, range_md="RANGE", corpus_text="CORPUS")

        def boom(cfg):
            raise RuntimeError("no network")

        monkeypatch.setattr("rangecontrol.gui.app.build_provider", boom)

        errors = []
        monkeypatch.setattr(
            "rangecontrol.gui.app.messagebox.showerror",
            lambda title, message: errors.append(message),
        )

        app._on_start_stop()
        assert errors and "no network" in errors[0]
    finally:
        app.destroy()


def test_start_reports_the_controller_rejecting_a_second_start(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    try:
        app._setup_tab.set_values({
            "LLM_PROVIDER": "anthropic", "LLM_API_KEY": "k",
            "DISCORD_BOT_TOKEN": "tok", "WHITE_CELL_CHANNEL_ID": "1",
        })
        app._outcome = IngestOutcome(ok=True, range_md="RANGE", corpus_text="CORPUS")

        monkeypatch.setattr("rangecontrol.gui.app.build_provider", lambda cfg: object())
        monkeypatch.setattr("rangecontrol.gui.app.build_gate_provider", lambda cfg: object())

        def boom(**kwargs):
            raise RuntimeError("the bot is already running")

        monkeypatch.setattr(app._controller, "start", boom)

        errors = []
        monkeypatch.setattr(
            "rangecontrol.gui.app.messagebox.showerror",
            lambda title, message: errors.append(message),
        )

        app._on_start_stop()
        assert errors and "already running" in errors[0]
    finally:
        app.destroy()


# -- open resources folder -----------------------------------------------------


def test_open_resources_creates_the_folder_and_invokes_the_file_manager(tmp_path, monkeypatch):
    import shutil

    app = make_app(tmp_path)
    try:
        shutil.rmtree(tmp_path / "resources")
        calls = []
        monkeypatch.setattr(
            "rangecontrol.gui.app.subprocess.run",
            lambda args, **kwargs: calls.append(args),
        )
        monkeypatch.setattr("rangecontrol.gui.app.sys.platform", "linux")

        app._on_open_resources()

        assert (tmp_path / "resources").is_dir()
        assert calls == [["xdg-open", str(tmp_path / "resources")]]
    finally:
        app.destroy()


def test_open_resources_uses_open_on_macos(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    try:
        calls = []
        monkeypatch.setattr(
            "rangecontrol.gui.app.subprocess.run",
            lambda args, **kwargs: calls.append(args),
        )
        monkeypatch.setattr("rangecontrol.gui.app.sys.platform", "darwin")

        app._on_open_resources()
        assert calls == [["open", str(tmp_path / "resources")]]
    finally:
        app.destroy()


def test_open_resources_uses_startfile_on_windows(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    try:
        calls = []
        monkeypatch.setattr("rangecontrol.gui.app.sys.platform", "win32")
        monkeypatch.setattr(
            "rangecontrol.gui.app.os.startfile", lambda p: calls.append(p), raising=False
        )

        app._on_open_resources()
        assert calls == [str(tmp_path / "resources")]
    finally:
        app.destroy()


def test_open_resources_swallows_a_missing_file_manager(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    try:
        def boom(args, **kwargs):
            raise OSError("no such program")

        monkeypatch.setattr("rangecontrol.gui.app.subprocess.run", boom)
        monkeypatch.setattr("rangecontrol.gui.app.sys.platform", "linux")

        app._on_open_resources()  # must not raise
    finally:
        app.destroy()


# -- shutdown flushes a pending save -------------------------------------------


# -- live human-in-the-loop toggle ---------------------------------------------


def test_toggling_the_checkbox_reaches_a_live_bots_mode_switch(tmp_path):
    from rangecontrol.bot.mode import ModeSwitch

    app = make_app(tmp_path)
    try:
        class FakeBot:
            def __init__(self):
                self.mode = ModeSwitch(False)

        bot = FakeBot()
        app._controller._bot = bot

        app._setup_tab._vars["HUMAN_IN_THE_LOOP"].set("true")
        assert bot.mode.enabled is True

        app._setup_tab._vars["HUMAN_IN_THE_LOOP"].set("false")
        assert bot.mode.enabled is False
    finally:
        app._controller._bot = None
        app.destroy()


def test_toggling_the_checkbox_is_harmless_with_no_bot_running(tmp_path):
    app = make_app(tmp_path)
    try:
        app._setup_tab._vars["HUMAN_IN_THE_LOOP"].set("true")  # must not raise
    finally:
        app.destroy()


def test_close_flushes_a_pending_save_before_destroying(tmp_path, monkeypatch):
    import tkinter as tk

    app = make_app(tmp_path)
    monkeypatch.setattr(app._controller, "stop", lambda timeout=10.0: None)
    monkeypatch.setattr(app, "destroy", lambda: None)
    try:
        app._setup_tab._vars["LLM_API_KEY"].set("saved-on-close")
        assert app._save_after_id is not None

        app._on_close()

        assert app._save_after_id is None
        assert read_env(tmp_path / ".env")["LLM_API_KEY"] == "saved-on-close"
    finally:
        tk.Tk.destroy(app)


# --- rescanning resources/ without a restart -------------------------------


def test_refresh_picks_up_files_dropped_in_after_launch(tmp_path):
    """The exact reported bug: drag files in, Generate stayed dull until restart."""
    app = make_app(tmp_path)
    try:
        for _ in range(3):
            app.update()
            app.pump()
        assert str(app._setup_tab.generate_button.cget("state")) == "disabled"

        (tmp_path / "resources" / "notes.txt").write_text("segment A", encoding="utf-8")
        app._on_refresh_resources()
        app.update()

        assert str(app._setup_tab.generate_button.cget("state")) == "normal"
    finally:
        app.destroy()


def test_refresh_notices_a_range_md_dropped_in_after_launch(tmp_path):
    """Someone can hand over a pre-generated range while the window is open."""
    app = make_app(tmp_path)
    try:
        for _ in range(3):
            app.update()
            app.pump()
        assert app._outcome is None

        write_complete_range(tmp_path)
        app._on_refresh_resources()
        pump_until(app, lambda: app._outcome is not None)

        assert app._outcome.ok
        assert app._setup_tab.generate_button.cget("text") == "Regenerate"
    finally:
        app.destroy()


def test_refresh_does_not_start_a_second_ingest_while_one_is_running(tmp_path):
    """Two concurrent ingests would race to set the outcome."""
    write_complete_range(tmp_path)
    app = make_app(tmp_path)
    try:
        app._ingest_in_flight = True
        started = []
        app._start_ingest = lambda *a, **k: started.append(1)
        app._on_refresh_resources()
        assert started == []
    finally:
        app.destroy()


# --- fetching the model list from the provider -----------------------------


def test_fetch_without_an_api_key_says_so_and_makes_no_call(tmp_path, monkeypatch):
    """The provider decides what a key can reach, so there is nothing to ask."""
    app = make_app(tmp_path)
    try:
        started = []
        monkeypatch.setattr("rangecontrol.gui.app.ModelsJob",
                            lambda *a, **k: started.append(1))
        app._setup_tab.set_values({"LLM_PROVIDER": "gemini", "LLM_API_KEY": ""})
        app._on_fetch_models()
        assert started == []
        assert "API key" in app._setup_tab.status_text()
    finally:
        app.destroy()


def test_a_successful_fetch_replaces_the_suggestions(tmp_path):
    from rangecontrol.gui.runtime import ModelsOutcome

    app = make_app(tmp_path)
    try:
        app._handle_models(ModelsOutcome(ok=True, models=("gemini-9-flash", "gemini-9-pro")))
        app.update()
        assert app._setup_tab.model_choices() == ("gemini-9-flash", "gemini-9-pro")
    finally:
        app.destroy()


def test_a_fetch_never_overwrites_what_the_operator_typed(tmp_path):
    """Replacing a typed model out from under someone is worse than a stale list."""
    from rangecontrol.gui.runtime import ModelsOutcome

    app = make_app(tmp_path)
    try:
        app._setup_tab.set_values({"LLM_PROVIDER": "gemini", "LLM_MODEL": "my-custom-model"})
        app._handle_models(ModelsOutcome(ok=True, models=("gemini-9-flash",)))
        app.update()
        assert app._setup_tab.values()["LLM_MODEL"] == "my-custom-model"
    finally:
        app.destroy()


def test_a_failed_fetch_explains_itself_and_re_enables_the_button(tmp_path):
    from rangecontrol.gui.runtime import ModelsOutcome

    app = make_app(tmp_path)
    try:
        app._setup_tab.set_fetch_busy(True)
        app._handle_models(ModelsOutcome(ok=False, error="LLMError: bad key"))
        app.update()
        assert "bad key" in app._setup_tab.status_text()
        assert str(app._setup_tab.fetch_models_button.cget("state")) != "disabled"
    finally:
        app.destroy()


def test_fetch_needs_only_a_provider_and_an_api_key(tmp_path, monkeypatch):
    """Listing models is a metadata call against the AI provider. Requiring
    the Discord token and channel IDs first blocks the very first thing an
    operator does on a fresh install: pick a model."""
    app = make_app(tmp_path)
    try:
        started = []
        monkeypatch.setattr(
            "rangecontrol.gui.app.ModelsJob",
            lambda config, events: started.append(config) or _NoopJob(),
        )
        app._setup_tab.set_values({
            "LLM_PROVIDER": "anthropic", "LLM_API_KEY": "sk-test",
            "DISCORD_BOT_TOKEN": "", "WHITE_CELL_CHANNEL_ID": "",
        })
        app._on_fetch_models()
        assert len(started) == 1
        assert started[0].llm_provider == "anthropic"
        assert started[0].llm_api_key == "sk-test"
    finally:
        app.destroy()


class _NoopJob:
    def start(self):
        return None


def test_a_tk_callback_exception_reaches_the_operator(tmp_path):
    """The Windows binary ships console=False, so Tk's default handler
    (print to stderr) shows nothing at all."""
    app = make_app(tmp_path)
    try:
        try:
            raise RuntimeError("widget exploded")
        except RuntimeError as exc:
            app.report_callback_exception(type(exc), exc, exc.__traceback__)
        assert "widget exploded" in app._status_bar.dump()
    finally:
        app.destroy()


def test_an_unreadable_env_does_not_break_the_buttons(tmp_path, monkeypatch):
    """Generate, Start, and Fetch all re-read .env; a transient lock there
    must degrade to the form's values, not raise out of a button handler."""
    app = make_app(tmp_path)
    try:
        def boom(path):
            raise OSError("locked")
        monkeypatch.setattr("rangecontrol.gui.app.read_env", boom)
        app._setup_tab.set_values({"LLM_PROVIDER": "gemini", "LLM_API_KEY": "k"})
        mapping = app._config_mapping(app._setup_tab.values())
        assert mapping["LLM_API_KEY"] == "k"
        assert "locked" in app._status_bar.dump()
    finally:
        app.destroy()


def test_the_env_read_warning_clears_once_env_is_written_again(tmp_path):
    """A successful autosave proves the file is usable again; keeping the
    startup warning after that tells the operator their settings are broken
    when they are not."""
    if os.geteuid() == 0:
        pytest.skip("root ignores permission bits")

    bootstrap(tmp_path)
    env = tmp_path / ".env"
    env.chmod(0o000)
    try:
        app = make_app(tmp_path)
    finally:
        env.chmod(0o600)
    try:
        assert ".env" in app._status_bar.dump()
        app._setup_tab.set_values({"LLM_PROVIDER": "gemini", "LLM_API_KEY": "k"})
        app._save_fields()
        assert ".env" not in app._status_bar.dump()
    finally:
        app.destroy()


def test_editing_a_setting_while_the_bot_runs_says_a_restart_is_needed(tmp_path):
    app = make_app(tmp_path)
    try:
        app._handle_status(BotState(RUNNING))
        app._setup_tab._vars["LLM_MODEL"].set("some-other-model")
        assert "restart" in app._status_bar.dump().lower()
    finally:
        app.destroy()


def test_toggling_the_checkbox_while_running_does_not_ask_for_a_restart(tmp_path):
    app = make_app(tmp_path)
    try:
        app._handle_status(BotState(RUNNING))
        app._setup_tab._vars["HUMAN_IN_THE_LOOP"].set("true")
        assert "restart" not in app._status_bar.dump().lower()
    finally:
        app.destroy()


def test_editing_the_denied_reply_reaches_a_live_bot_without_a_restart(tmp_path):
    from rangecontrol.bot.mode import ModeSwitch

    app = make_app(tmp_path)
    try:
        class LiveBot:
            mode = ModeSwitch(False)
            denied_text = "old"
        live = LiveBot()
        app._controller._bot = live
        app._handle_status(BotState(RUNNING))
        app._setup_tab._vars["HUMAN_IN_THE_LOOP"].set("true")  # unlocks the box
        # Typed, not set_values(): programmatic loads are deliberately not
        # edits, and a Text widget reports typing via a queued <<Modified>>.
        widget = app._setup_tab.entry_for("HITL_DENIED_TEXT")
        widget.delete("1.0", "end")
        widget.insert("1.0", "new wording")
        app.update()
        assert live.denied_text == "new wording"
        assert "restart" not in app._status_bar.dump().lower()
    finally:
        app._controller._bot = None
        app.destroy()


def test_the_window_opens_tall_enough_to_show_every_control(tmp_path):
    """The fixed 980x700 stopped fitting once the form gained fields; the
    buttons at the bottom were off-screen until the operator dragged the
    window taller. The opening size must follow the content."""
    from rangecontrol.gui.app import _HEIGHT_SLACK, _REFIT_DELAY_MS

    app = make_app(tmp_path)
    try:
        app.update()  # map the window so the hints wrap to their real width
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:  # let the second measuring pass run
            app.update()
            time.sleep(_REFIT_DELAY_MS / 1000)
            if time.monotonic() > deadline - 1.5:
                break
        width, height = (int(v) for v in app.geometry().split("+")[0].split("x"))
        # A headless test screen can be smaller than the content; the window
        # is then capped to the screen, which is the most it can do.
        wanted_height = min(
            app.winfo_reqheight() + _HEIGHT_SLACK, app.winfo_screenheight() - 80
        )
        assert height >= wanted_height
        assert width >= min(app.winfo_reqwidth(), app.winfo_screenwidth())
        min_width, min_height = app.minsize()
        assert min_height >= wanted_height
    finally:
        app.destroy()
