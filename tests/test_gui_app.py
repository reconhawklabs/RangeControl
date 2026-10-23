import os
import shutil
import stat
import time

import pytest

from rangecontrol.advisor.engine import Advisor
from rangecontrol.gui.app import App, bootstrap, run_gui
from rangecontrol.gui.env_file import read_env, update_env
from rangecontrol.gui.events import ERROR, INGEST, LOG, PENDING, QUESTION, STATUS, Event
from rangecontrol.gui.paths import PathError
from rangecontrol.gui.runtime import (
    FAILED,
    RUNNING,
    STARTING,
    STOPPED,
    STOPPING,
    BotState,
    IngestOutcome,
    ModelsOutcome,
)
from rangecontrol.range_doc.loader import REQUIRED_SECTIONS
from rangecontrol.range_doc.report import IngestReport
from tests.support.images import write_png

COMPLETE = {
    "LLM_PROVIDER": "anthropic", "LLM_API_KEY": "k",
    "DISCORD_BOT_TOKEN": "tok", "WHITE_CELL_CHANNEL_ID": "1",
}


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
    A plain .txt resource never needs a provider, so it loads for free."""
    (tmp_path / "resources").mkdir(parents=True, exist_ok=True)
    (tmp_path / "resources" / "notes.txt").write_text("segment A", encoding="utf-8")
    complete = "\n".join(f"## {name}\n\ncontent\n" for name in REQUIRED_SECTIONS)
    (tmp_path / "Range.md").write_text(complete, encoding="utf-8")


def pump_until(app, predicate, timeout=10.0):
    """Drain the queue on a short poll loop until predicate() is true: the
    startup load runs on a real background thread."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.pump()
        if predicate():
            return
        time.sleep(0.02)
    assert predicate(), f"condition not met within {timeout}s"


def ingest_report(index_rows=3):
    return IngestReport(
        total_files=1, by_kind=(("text", 1),), unreadable=(),
        range_md_generated=True, range_md_gaps=(), index_rows=index_rows,
        inject_count=2, context_chars=100,
    )


def record_errors(monkeypatch):
    errors = []
    monkeypatch.setattr(
        "rangecontrol.gui.app.messagebox.showerror",
        lambda title, message: errors.append(message),
    )
    return errors


def refuse(*args, **kwargs):
    raise AssertionError("called without a user action that should allow it")


class _RecordingIngestJob:
    """Stands in for IngestJob so tests never touch a real provider."""

    instances: list = []

    def __init__(
        self, config, events, *, regenerate, provider_factory=None, generation=0
    ) -> None:
        self.config = config
        self.regenerate = regenerate
        self.started = False
        _RecordingIngestJob.instances.append(self)

    def start(self):
        self.started = True


class _NoopJob:
    def start(self):
        return None


# -- bootstrap and startup ----------------------------------------------------


def test_bootstrap_lays_out_a_home_and_never_clobbers_the_users_env(tmp_path):
    fresh = tmp_path / "fresh"
    bootstrap(fresh)
    bootstrap(fresh)  # idempotent
    assert (fresh / "resources").is_dir()
    # Handed to a coding agent, so it must be a real file; the template is an
    # input to the generator, not something the user edits.
    assert (fresh / "PREGENERATE.md").is_file()
    assert not (fresh / "RangeTemplate.md").exists()

    starter = read_env(fresh / ".env")
    assert starter["LLM_PROVIDER"] == "gemini"
    assert starter["LLM_API_KEY"] == "" and starter["DISCORD_BOT_TOKEN"] == ""
    assert "LLM_GATE_MODEL" in starter
    if os.name == "posix":
        assert stat.S_IMODE((fresh / ".env").stat().st_mode) == 0o600

    mine = tmp_path / "mine"
    mine.mkdir()
    (mine / ".env").write_text("LLM_API_KEY=mine\n", encoding="utf-8")
    bootstrap(mine)
    assert read_env(mine / ".env")["LLM_API_KEY"] == "mine"


def test_a_fresh_home_opens_without_spending_and_notices_dropped_in_material(
    tmp_path, monkeypatch
):
    """Opening the window must never touch a provider. The reported bug: files
    dragged into resources/ left Generate dull until a restart."""
    for name in ("build_provider", "build_gate_provider", "IngestJob"):
        monkeypatch.setattr(f"rangecontrol.gui.app.{name}", refuse)

    app = make_app(tmp_path)
    try:
        for _ in range(3):
            app.update()
            app.pump()
        assert app._outcome is None
        assert app._status_bar.dump().strip() == "○ Bot stopped"
        assert app._setup_tab.values()["LLM_PROVIDER"] != ""
        assert app._setup_tab.generate_button.cget("text") == "Generate"
        assert str(app._setup_tab.generate_button.cget("state")) == "disabled"
        assert str(app._setup_tab.start_button.cget("state")) == "disabled"

        (tmp_path / "resources" / "notes.txt").write_text("segment A", encoding="utf-8")
        app._on_refresh_resources()
        app.update()
        assert str(app._setup_tab.generate_button.cget("state")) == "normal"

        # A Range.md handed over mid-session is picked up, but never by a
        # second ingest racing one already in flight.
        write_complete_range(tmp_path)
        app._ingest_in_flight = True
        app._on_refresh_resources()  # IngestJob still refuses: must not start one
        app._ingest_in_flight = False

        monkeypatch.undo()
        app._on_refresh_resources()
        pump_until(app, lambda: app._outcome is not None)
        assert app._outcome.ok
        assert app._setup_tab.generate_button.cget("text") == "Regenerate"
    finally:
        app.destroy()


def test_an_existing_range_md_loads_at_startup_without_spending(tmp_path, monkeypatch):
    """The pre-generation workflow hands us a Range.md; Start Bot must work.
    A cold-cache image must surface as a message, never a silent API call."""
    write_complete_range(tmp_path)
    write_png(tmp_path / "resources" / "diagram.png")
    built = []
    monkeypatch.setattr(
        "rangecontrol.gui.app.build_provider", lambda cfg: built.append(1) or object()
    )
    app = make_app(tmp_path)
    try:
        # Busy before the worker thread even starts, and Generate refuses to
        # start a second job (before its confirm dialog) until the load lands.
        assert str(app._setup_tab.generate_button.cget("state")) == "disabled"
        assert app._ingest_in_flight is True
        asked = []
        with monkeypatch.context() as m:
            m.setattr("rangecontrol.gui.app.messagebox.askyesno",
                      lambda *a, **k: asked.append(1) or True)
            m.setattr("rangecontrol.gui.app.IngestJob", refuse)
            app._on_generate()
        assert asked == []

        pump_until(app, lambda: app._outcome is not None)
        assert app._outcome.ok is True
        assert app._setup_tab.generate_button.cget("text") == "Regenerate"
        assert built == []
        status = app._setup_tab.status_text()
        assert "diagram.png" in status
        assert "Generate" in status
    finally:
        app.destroy()


def test_an_unreadable_env_degrades_to_a_warning_until_it_is_written_again(
    tmp_path, monkeypatch
):
    """The binary ships console=False: a crash before the window exists shows
    nothing. The warning must outlive an unrelated successful startup ingest,
    and clear once a save proves the file usable."""
    if os.geteuid() == 0:
        pytest.skip("root ignores permission bits")

    bootstrap(tmp_path)
    write_complete_range(tmp_path)
    env = tmp_path / ".env"
    env.chmod(0o000)
    try:
        app = make_app(tmp_path)  # must not raise
    finally:
        env.chmod(0o600)
    try:
        assert app._setup_tab.values()["LLM_API_KEY"] == ""
        assert ".env" in app._setup_tab.status_text()
        assert ".env" in app._status_bar.dump()
        pump_until(app, lambda: app._outcome is not None)
        assert app._outcome.ok is True
        assert ".env" in app._status_bar.dump()

        # A transient lock when a button re-reads .env falls back to the form.
        def locked(path):
            raise OSError("locked")

        app._setup_tab.set_values({"LLM_PROVIDER": "gemini", "LLM_API_KEY": "k"})
        with monkeypatch.context() as m:
            m.setattr("rangecontrol.gui.app.read_env", locked)
            mapping = app._config_mapping(app._setup_tab.values())
        assert mapping["LLM_API_KEY"] == "k"
        assert "locked" in app._status_bar.dump()

        app._save_fields()
        assert ".env" not in app._status_bar.dump()
    finally:
        app.destroy()


def test_an_unreadable_range_md_or_resources_folder_does_not_crash_startup(tmp_path):
    """Restarting mid-exercise after a lock or AV scan must not be a new way
    to lose the window."""
    if os.geteuid() == 0:
        pytest.skip("root ignores permission bits")

    range_home = tmp_path / "range"
    range_home.mkdir()
    write_complete_range(range_home)
    range_md = range_home / "Range.md"
    range_md.chmod(0o000)
    try:
        app = make_app(range_home)  # must not raise
        try:
            assert app._outcome is None
            assert app._setup_tab.generate_button.cget("text") == "Generate"
        finally:
            app.destroy()
    finally:
        range_md.chmod(0o644)

    resources_home = tmp_path / "resources-locked"
    bootstrap(resources_home)
    resources = resources_home / "resources"
    resources.chmod(0o000)
    try:
        app = make_app(resources_home)  # must not raise
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


# -- ingest outcomes, autosave, and the event pump -----------------------------


def test_the_newest_successful_ingest_is_the_one_the_bot_uses(tmp_path):
    """A failed regenerate keeps the still-valid range; a superseded request
    that resolves late must not overwrite the current one."""
    app = make_app(tmp_path)
    try:
        app._setup_tab.set_values(COMPLETE)
        good = IngestOutcome(ok=True, report=ingest_report(index_rows=3),
                             range_md="RANGE", corpus_text="TEXT")
        app._events.put(Event(kind=INGEST, payload=good))
        app.pump()
        assert app._outcome is good
        assert str(app._setup_tab.start_button.cget("state")) == "normal"
        assert "3" in app._status_bar.dump()

        app._events.put(Event(kind=INGEST, payload=IngestOutcome(
            ok=False, error="Anthropic request failed")))
        app.pump()
        assert app._outcome is good
        assert "Anthropic request failed" in app._setup_tab.status_text()

        app._ingest_generation = 2  # the startup load and a Generate both in flight
        fresh = IngestOutcome(ok=True, report=ingest_report(), range_md="FRESH",
                              corpus_text="FRESH", generation=2)
        stale = IngestOutcome(ok=True, report=ingest_report(), range_md="STALE",
                              corpus_text="STALE", generation=1)
        app._handle_ingest(fresh)
        app._handle_ingest(stale)
        assert app._outcome is fresh
    finally:
        app.destroy()


def test_settings_autosave_to_env_debounced_and_flushed_on_close(tmp_path, monkeypatch):
    """Loading is not an edit, a burst of typing writes once, a failed write is
    reported, and closing stops the bot and flushes before the window goes."""
    import tkinter as tk

    bootstrap(tmp_path)
    env = tmp_path / ".env"
    update_env(env, {"LLM_API_KEY": "a-real-key", "LLM_PROVIDER": "anthropic"})
    before = env.read_bytes()

    app = make_app(tmp_path)
    try:
        assert app._save_after_id is None
        assert env.read_bytes() == before
        assert app._setup_tab.values()["LLM_API_KEY"] == "a-real-key"
        app._save_fields()  # re-saving exactly what was loaded is a no-op
        assert env.read_bytes() == before

        def disk_full(path, changes):
            raise OSError("disk full")

        app._setup_tab._vars["LLM_API_KEY"].set("typed")
        app.after_cancel(app._save_after_id)
        with monkeypatch.context() as m:
            m.setattr("rangecontrol.gui.app.update_env", disk_full)
            app._save_fields()  # must not raise
        assert "disk full" in app._status_bar.dump()

        cancels = []
        real_after_cancel = app.after_cancel
        with monkeypatch.context() as m:
            m.setattr(app, "after_cancel",
                      lambda job: cancels.append(job) or real_after_cancel(job))
            for typed in ("a", "ab", "abc"):
                app._setup_tab._vars["LLM_API_KEY"].set(typed)
        assert len(cancels) == 2  # the first two pending saves were cancelled
        assert app._save_after_id is not None

        order = []
        monkeypatch.setattr(app._controller, "stop",
                            lambda timeout=10.0: order.append("stop"))
        monkeypatch.setattr(
            app, "destroy", lambda: order.append(("destroy", read_env(env)["LLM_API_KEY"]))
        )
        app._on_close()
        assert order == ["stop", ("destroy", "abc")]
        assert app._save_after_id is None
    finally:
        tk.Tk.destroy(app)


def test_queued_events_reach_the_window_and_a_bad_one_never_stops_the_pump(tmp_path):
    """One raising handler must not kill the after() chain and freeze every
    later update while the window keeps looking alive."""
    app = make_app(tmp_path)
    try:
        rescheduled = []
        app.after = lambda ms, cb: rescheduled.append(cb) or "fake-id"
        app._events.put(Event(kind=QUESTION, payload=None))  # handler raises
        app._events.put(Event(kind="mystery", payload="whatever"))
        app._events.put(Event(kind=LOG, payload="still alive"))
        app._events.put(Event(kind=PENDING, payload=2))
        app._events.put(Event(kind=ERROR, payload="white cell channel unreachable"))
        app.pump()  # must not raise

        assert rescheduled == [app.pump]
        assert "still alive" in app._console_tab.dump()
        assert "2" in app._status_bar.dump()
        assert "white cell channel unreachable" in app._status_bar.dump()

        # The console=False binary has no stderr for Tk's default handler.
        try:
            raise RuntimeError("widget exploded")
        except RuntimeError as exc:
            app.report_callback_exception(type(exc), exc, exc.__traceback__)
        assert "widget exploded" in app._status_bar.dump()
    finally:
        del app.after  # restore the bound method for Tk's own teardown
        app.destroy()


# -- Generate, Start/Stop, live settings ---------------------------------------


def test_generate_confirms_validates_then_starts_one_ingest(tmp_path, monkeypatch):
    """Generate is the only path that spends money."""
    _RecordingIngestJob.instances = []
    app = make_app(tmp_path)
    try:
        (tmp_path / "resources" / "note.txt").write_text("segment A", encoding="utf-8")
        app._refresh_resources_present()
        monkeypatch.setattr("rangecontrol.gui.app.IngestJob", _RecordingIngestJob)
        errors = record_errors(monkeypatch)
        answer = {"yes": False}
        monkeypatch.setattr("rangecontrol.gui.app.messagebox.askyesno",
                            lambda *a, **k: answer["yes"])

        app._setup_tab.set_values({"LLM_PROVIDER": "anthropic", "LLM_API_KEY": "k"})
        app._on_generate()  # declined
        assert _RecordingIngestJob.instances == []

        answer["yes"] = True
        app._setup_tab.set_values({"LLM_PROVIDER": "anthropic", "LLM_API_KEY": ""})
        app._on_generate()  # an invalid config is a dialog, not a crash
        assert _RecordingIngestJob.instances == []
        assert errors and "LLM_API_KEY" in errors[0]

        # LLM_GATE_MODEL is not on the form, so it must come from .env.
        app._setup_tab.set_values({"LLM_PROVIDER": "anthropic", "LLM_API_KEY": "k"})
        update_env(tmp_path / ".env", {"LLM_GATE_MODEL": "cheap-model"})
        app._on_generate()
        assert len(_RecordingIngestJob.instances) == 1
        job = _RecordingIngestJob.instances[0]
        assert job.started is True
        assert job.regenerate is False  # first-ever generate, nothing to force
        assert job.config.llm_api_key == "k"
        assert job.config.llm_provider == "anthropic"
        assert job.config.llm_gate_model == "cheap-model"
        # Discord fields are irrelevant to ingest and were left blank.
        assert job.config.discord_bot_token == "unset"

        # Once a range exists, Generate means Regenerate.
        app._ingest_in_flight = False
        app._outcome = IngestOutcome(ok=True, range_md="RANGE", corpus_text="CORPUS")
        app._on_generate()
        assert _RecordingIngestJob.instances[-1].regenerate is True
    finally:
        app.destroy()


def test_start_is_refused_with_a_reason_for_every_bad_precondition(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    try:
        errors = record_errors(monkeypatch)
        monkeypatch.setattr(app._controller, "start", refuse)
        app._setup_tab.set_values(COMPLETE)

        app._on_start_stop()  # nothing ingested yet
        assert len(errors) == 1

        app._outcome = IngestOutcome(ok=True, range_md="RANGE", corpus_text="CORPUS")
        app._setup_tab.set_values({**COMPLETE, "WHITE_CELL_CHANNEL_ID": "not-a-number"})
        app._on_start_stop()
        assert len(errors) == 2 and "numeric" in errors[-1].lower()

        def no_network(cfg):
            raise RuntimeError("no network")

        app._setup_tab.set_values(COMPLETE)
        monkeypatch.setattr("rangecontrol.gui.app.build_provider", no_network)
        app._on_start_stop()
        assert len(errors) == 3 and "no network" in errors[-1]

        def already_running(**kwargs):
            raise RuntimeError("the bot is already running")

        monkeypatch.setattr("rangecontrol.gui.app.build_provider", lambda cfg: object())
        monkeypatch.setattr("rangecontrol.gui.app.build_gate_provider", lambda cfg: object())
        monkeypatch.setattr(app._controller, "start", already_running)
        app._on_start_stop()
        assert len(errors) == 4 and "already running" in errors[-1]
    finally:
        app.destroy()


def test_start_hands_the_bot_its_config_and_the_button_follows_bot_state(
    tmp_path, monkeypatch
):
    app = make_app(tmp_path)
    try:
        app._setup_tab.set_values(COMPLETE)
        update_env(tmp_path / ".env", {"LLM_GATE_MODEL": "cheap-model"})
        app._outcome = IngestOutcome(ok=True, range_md="RANGE", corpus_text="CORPUS")

        seen = {}

        def fake_build_gate_provider(cfg):
            seen["gate_cfg"] = cfg
            return object()

        def fake_start(*, config, advisor, audit):
            seen.update(config=config, advisor=advisor, audit=audit)

        monkeypatch.setattr("rangecontrol.gui.app.build_provider", lambda cfg: object())
        monkeypatch.setattr("rangecontrol.gui.app.build_gate_provider",
                            fake_build_gate_provider)
        monkeypatch.setattr(app._controller, "start", fake_start)
        app._on_start_stop()

        assert seen["config"].discord_bot_token == "tok"
        assert seen["config"].llm_gate_model == "cheap-model"
        assert seen["gate_cfg"].llm_gate_model == "cheap-model"
        assert isinstance(seen["advisor"], Advisor)

        # Every active state reads as "running" to the Start/Stop button.
        for bot_state, active in ((RUNNING, True), (STOPPED, False), (STARTING, True),
                                  (FAILED, False), (STOPPING, True)):
            app._events.put(Event(kind=STATUS, payload=BotState(bot_state)))
            app.pump()
            expected = "Stop Bot" if active else "Start Bot"
            assert app._setup_tab.start_button.cget("text") == expected, bot_state

        # While the bot is active, the same button stops it.
        app._controller._state = BotState(RUNNING)
        stopped = []
        monkeypatch.setattr(app._controller, "stop",
                            lambda timeout=10.0: stopped.append(True))
        app._on_start_stop()
        assert stopped == [True]
    finally:
        app.destroy()


def test_live_settings_reach_a_running_bot_and_others_ask_for_a_restart(tmp_path):
    """Human-in-the-loop and the denial wording apply live, because a restart
    mid-exercise drops every pending approval. Anything else needs a restart,
    and the operator is told so."""
    from rangecontrol.bot.mode import ModeSwitch

    app = make_app(tmp_path)
    try:
        app._setup_tab._vars["HUMAN_IN_THE_LOOP"].set("true")  # no bot: harmless
        app._setup_tab._vars["HUMAN_IN_THE_LOOP"].set("false")

        class LiveBot:
            def __init__(self):
                self.mode = ModeSwitch(False)
                self.denied_text = "old"

        live = LiveBot()
        app._controller._bot = live
        app._handle_status(BotState(RUNNING))

        app._setup_tab._vars["HUMAN_IN_THE_LOOP"].set("true")  # unlocks the box
        assert live.mode.enabled is True
        # Typed, not set_values(): programmatic loads are deliberately not
        # edits, and a Text widget reports typing via a queued <<Modified>>.
        widget = app._setup_tab.entry_for("HITL_DENIED_TEXT")
        widget.delete("1.0", "end")
        widget.insert("1.0", "new wording")
        app.update()
        assert live.denied_text == "new wording"
        app._setup_tab._vars["HUMAN_IN_THE_LOOP"].set("false")
        assert live.mode.enabled is False
        assert "restart" not in app._status_bar.dump().lower()

        app._setup_tab._vars["LLM_MODEL"].set("some-other-model")
        assert "restart" in app._status_bar.dump().lower()
    finally:
        app._controller._bot = None
        app.destroy()


def test_fetch_models_needs_only_a_key_and_never_clobbers_a_typed_model(
    tmp_path, monkeypatch
):
    """Picking a model is the first thing a fresh install does, so the Discord
    fields cannot be required yet; and the provider decides what a key reaches."""
    app = make_app(tmp_path)
    try:
        started = []
        monkeypatch.setattr("rangecontrol.gui.app.ModelsJob",
                            lambda config, events: started.append(config) or _NoopJob())

        app._setup_tab.set_values({"LLM_PROVIDER": "gemini", "LLM_API_KEY": ""})
        app._on_fetch_models()
        assert started == []
        assert "API key" in app._setup_tab.status_text()

        app._setup_tab.set_values({
            "LLM_PROVIDER": "anthropic", "LLM_API_KEY": "sk-test",
            "DISCORD_BOT_TOKEN": "", "WHITE_CELL_CHANNEL_ID": "",
        })
        app._on_fetch_models()
        assert len(started) == 1
        assert started[0].llm_provider == "anthropic"
        assert started[0].llm_api_key == "sk-test"
        button = app._setup_tab.fetch_models_button
        assert str(button.cget("state")) == "disabled"

        app._handle_models(ModelsOutcome(ok=False, error="LLMError: bad key"))
        app.update()
        assert "bad key" in app._setup_tab.status_text()
        assert str(button.cget("state")) != "disabled"

        app._setup_tab.set_values({"LLM_PROVIDER": "gemini", "LLM_MODEL": "my-custom-model"})
        app._handle_models(ModelsOutcome(ok=True, models=("gemini-9-flash", "gemini-9-pro")))
        app.update()
        assert app._setup_tab.model_choices() == ("gemini-9-flash", "gemini-9-pro")
        assert app._setup_tab.values()["LLM_MODEL"] == "my-custom-model"
    finally:
        app.destroy()


def test_open_resources_creates_the_folder_and_opens_it_on_every_platform(
    tmp_path, monkeypatch
):
    app = make_app(tmp_path)
    try:
        resources = tmp_path / "resources"
        calls = []
        monkeypatch.setattr("rangecontrol.gui.app.subprocess.run",
                            lambda args, **kwargs: calls.append(args))
        monkeypatch.setattr("rangecontrol.gui.app.os.startfile",
                            lambda p: calls.append(p), raising=False)
        for platform, expected in (
            ("linux", ["xdg-open", str(resources)]),
            ("darwin", ["open", str(resources)]),
            ("win32", str(resources)),
        ):
            shutil.rmtree(resources, ignore_errors=True)
            calls.clear()
            monkeypatch.setattr("rangecontrol.gui.app.sys.platform", platform)
            app._on_open_resources()
            assert resources.is_dir(), platform
            assert calls == [expected], platform

        def no_file_manager(args, **kwargs):
            raise OSError("no such program")

        monkeypatch.setattr("rangecontrol.gui.app.subprocess.run", no_file_manager)
        monkeypatch.setattr("rangecontrol.gui.app.sys.platform", "linux")
        app._on_open_resources()  # logged, never raised
    finally:
        app.destroy()


# -- run_gui: nothing on the startup path may fail silently --------------------


def test_run_gui_launches_the_app_or_reports_why_it_could_not(tmp_path, monkeypatch):
    """The binary ships console=False: an uncaught startup error is a
    double-click that produces no window and no message, ever."""
    import tkinter as tk

    monkeypatch.setattr("rangecontrol.gui.app.app_home", lambda: tmp_path)

    launched = []

    class LaunchedApp:
        def __init__(self, *, home):
            launched.append(home)

        def mainloop(self):
            launched.append("mainloop")

    with monkeypatch.context() as m:
        m.setattr("rangecontrol.gui.app.App", LaunchedApp)
        assert run_gui() == 0
    assert launched == [tmp_path, "mainloop"]
    assert (tmp_path / ".env").is_file()
    assert (tmp_path / "resources").is_dir()

    errors = record_errors(monkeypatch)

    def fails_reporting(*patches):
        errors.clear()
        with monkeypatch.context() as m:
            for target, value in patches:
                m.setattr(target, value)
            try:
                result = run_gui()
            except tk.TclError as exc:
                pytest.skip(f"no usable display: {exc}")
        assert result == 1
        assert errors
        return errors[0]

    def path_error(home):
        raise PathError("cannot write there")

    built = []
    message = fails_reporting(
        ("rangecontrol.gui.app.ensure_app_home", path_error),
        ("rangecontrol.gui.app.App", lambda **kwargs: built.append(kwargs)),
    )
    assert "cannot write there" in message
    assert built == []  # reported before any main window

    def construction_fails(*, home):
        raise RuntimeError("boom during construction")

    assert "boom during construction" in fails_reporting(
        ("rangecontrol.gui.app.App", construction_fails))

    # App.__init__ creates a real Tk root before its own code runs; a failure
    # partway through must not leave that window behind.
    def init_fails(self):
        raise RuntimeError("boom mid-init")

    assert "boom mid-init" in fails_reporting(
        ("rangecontrol.gui.app.App._load_initial_state", init_fails))
    assert tk._default_root is None

    destroyed = []

    class CrashingApp:
        def __init__(self, *, home):
            pass

        def mainloop(self):
            raise RuntimeError("mainloop exploded")

        def destroy(self):
            destroyed.append(True)

    assert "mainloop exploded" in fails_reporting(("rangecontrol.gui.app.App", CrashingApp))
    assert destroyed == [True]
