import queue

import pytest

from rangecontrol.config import load_config
from rangecontrol.gui.events import INGEST
from rangecontrol.gui.runtime import IngestJob
from rangecontrol.llm.base import LLMError
from rangecontrol.range_doc.loader import REQUIRED_SECTIONS
from tests.support.stub_provider import StubProvider

COMPLETE = "\n".join(f"## {name}\n\ncontent\n" for name in REQUIRED_SECTIONS)


def config_for(tmp_path):
    return load_config({
        "DISCORD_BOT_TOKEN": "tok",
        "LLM_PROVIDER": "anthropic",
        "LLM_API_KEY": "key",
        "WHITE_CELL_CHANNEL_ID": "1",
        "RANGE_DIR": str(tmp_path),
    })


def with_resources(tmp_path):
    (tmp_path / "resources").mkdir()
    (tmp_path / "resources" / "notes.txt").write_text("segment A", encoding="utf-8")
    return tmp_path


def test_successful_ingest_publishes_the_outcome(tmp_path):
    with_resources(tmp_path)
    (tmp_path / "Range.md").write_text(COMPLETE, encoding="utf-8")
    events = queue.Queue()
    IngestJob(config_for(tmp_path), events, regenerate=False,
              provider_factory=lambda cfg: StubProvider()).run()

    event = events.get_nowait()
    assert event.kind == INGEST
    assert event.payload.ok is True
    assert event.payload.report.total_files == 1
    assert "segment A" in event.payload.corpus_text


def test_provider_failure_is_reported_not_raised(tmp_path):
    """A worker thread that raises takes down the app silently."""
    with_resources(tmp_path)
    events = queue.Queue()
    IngestJob(
        config_for(tmp_path), events, regenerate=False,
        provider_factory=lambda cfg: StubProvider(error=LLMError("bad key")),
    ).run()

    event = events.get_nowait()
    assert event.kind == INGEST
    assert event.payload.ok is False
    assert "bad key" in event.payload.error


def test_missing_resources_is_reported_cleanly(tmp_path):
    events = queue.Queue()
    IngestJob(config_for(tmp_path), events, regenerate=False,
              provider_factory=lambda cfg: StubProvider()).run()
    event = events.get_nowait()
    assert event.payload.ok is False
    assert "resources" in event.payload.error


def test_regenerate_clears_the_cache(tmp_path):
    with_resources(tmp_path)
    (tmp_path / "Range.md").write_text(COMPLETE, encoding="utf-8")
    cache = tmp_path / ".rangecontrol" / "cache"
    cache.mkdir(parents=True)
    (cache / "1-stale.txt").write_text("stale", encoding="utf-8")

    events = queue.Queue()
    IngestJob(
        config_for(tmp_path), events, regenerate=True,
        provider_factory=lambda cfg: StubProvider(completions=[COMPLETE]),
    ).run()

    assert not (cache / "1-stale.txt").exists()
    assert events.get_nowait().payload.ok is True


def test_bare_exception_is_reported_not_raised(tmp_path):
    """The trailing `except Exception` clause is this task's central
    guarantee -- exercise it directly rather than only via LLMError."""
    with_resources(tmp_path)
    events = queue.Queue()

    def boom(cfg):
        raise Exception("kaboom")

    IngestJob(config_for(tmp_path), events, regenerate=False,
              provider_factory=boom).run()

    event = events.get_nowait()
    assert event.kind == INGEST
    assert event.payload.ok is False
    assert "Exception: kaboom" in event.payload.error


def test_unexpected_exception_from_prepare_is_reported_not_raised(tmp_path, monkeypatch):
    """A failure inside prepare() itself, not just the provider factory,
    must still resolve to a clean outcome via the broad except clause."""
    with_resources(tmp_path)
    events = queue.Queue()

    def broken_prepare(config, provider, *, regenerate):
        raise TypeError("not a range")

    monkeypatch.setattr("rangecontrol.gui.runtime.prepare", broken_prepare)

    IngestJob(config_for(tmp_path), events, regenerate=False,
              provider_factory=lambda cfg: StubProvider()).run()

    event = events.get_nowait()
    assert event.kind == INGEST
    assert event.payload.ok is False
    assert "TypeError: not a range" in event.payload.error


def test_system_exit_is_reported_then_reraised(tmp_path):
    """A genuine shutdown signal must still inform the GUI before it keeps
    unwinding -- publish-then-reraise, not swallow-or-crash-silently."""
    with_resources(tmp_path)
    events = queue.Queue()

    def quit_now(cfg):
        raise SystemExit("shutting down")

    with pytest.raises(SystemExit):
        IngestJob(config_for(tmp_path), events, regenerate=False,
                  provider_factory=quit_now).run()

    event = events.get_nowait()
    assert event.kind == INGEST
    assert event.payload.ok is False
    assert "SystemExit: shutting down" in event.payload.error


def test_start_returns_a_running_thread(tmp_path):
    with_resources(tmp_path)
    (tmp_path / "Range.md").write_text(COMPLETE, encoding="utf-8")
    events = queue.Queue()
    thread = IngestJob(config_for(tmp_path), events, regenerate=False,
                       provider_factory=lambda cfg: StubProvider()).start()
    thread.join(timeout=10)
    assert not thread.is_alive()
    assert events.get_nowait().kind == INGEST


# --- fetching the model list -----------------------------------------------


def test_models_job_publishes_what_the_provider_returned(tmp_path):
    events = queue.Queue()
    from rangecontrol.gui.events import MODELS
    from rangecontrol.gui.runtime import ModelsJob

    ModelsJob(config_for(tmp_path), events,
              lister=lambda cfg: ("gemini-9-flash", "gemini-9-pro")).run()
    event = events.get_nowait()
    assert event.kind == MODELS
    assert event.payload.ok is True
    assert event.payload.models == ("gemini-9-flash", "gemini-9-pro")


def test_models_job_reports_a_failure_rather_than_raising(tmp_path):
    """A dropdown that silently fails to repopulate explains nothing."""
    events = queue.Queue()
    from rangecontrol.gui.runtime import ModelsJob

    def boom(cfg):
        raise LLMError("Could not list Gemini models: AuthenticationError")

    ModelsJob(config_for(tmp_path), events, lister=boom).run()
    outcome = events.get_nowait().payload
    assert outcome.ok is False
    assert "AuthenticationError" in outcome.error


def test_models_job_thread_finishes(tmp_path):
    events = queue.Queue()
    from rangecontrol.gui.runtime import ModelsJob

    thread = ModelsJob(config_for(tmp_path), events, lister=lambda cfg: ("m",)).start()
    thread.join(timeout=10)
    assert not thread.is_alive()
    assert events.get_nowait().payload.models == ("m",)
