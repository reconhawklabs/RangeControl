import queue

import pytest

from rangecontrol.config import load_config
from rangecontrol.gui.events import INGEST, MODELS
from rangecontrol.gui.runtime import IngestJob, ModelsJob
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


def test_ingest_then_regenerate_publish_their_outcomes(tmp_path):
    with_resources(tmp_path)
    (tmp_path / "Range.md").write_text(COMPLETE, encoding="utf-8")
    events = queue.Queue()
    thread = IngestJob(config_for(tmp_path), events, regenerate=False,
                       provider_factory=lambda cfg: StubProvider()).start()
    thread.join(timeout=10)
    assert not thread.is_alive()

    event = events.get_nowait()
    assert event.kind == INGEST
    assert event.payload.ok is True
    assert event.payload.report.total_files == 1
    assert "segment A" in event.payload.corpus_text

    # Regenerate throws away cached extractions before rebuilding.
    cache = tmp_path / ".rangecontrol" / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    (cache / "1-stale.txt").write_text("stale", encoding="utf-8")
    IngestJob(
        config_for(tmp_path), events, regenerate=True,
        provider_factory=lambda cfg: StubProvider(completions=[COMPLETE]),
    ).run()
    assert not (cache / "1-stale.txt").exists()
    assert events.get_nowait().payload.ok is True


def test_any_failure_is_published_as_an_outcome_not_raised(tmp_path, monkeypatch):
    """A worker thread that raises dies silently and the GUI waits forever."""

    def kaboom(cfg):
        raise Exception("kaboom")

    def broken_prepare(config, provider, *, regenerate):
        raise TypeError("not a range")

    cases = [
        ("no-resources", lambda cfg: StubProvider(), None, "resources"),
        ("bad-key", lambda cfg: StubProvider(error=LLMError("bad key")), None, "bad key"),
        ("bare-exception", kaboom, None, "Exception: kaboom"),
        ("prepare-raises", lambda cfg: StubProvider(), broken_prepare, "TypeError: not a range"),
    ]
    for name, factory, prepare, expected in cases:
        home = tmp_path / name
        home.mkdir()
        if name != "no-resources":
            with_resources(home)
        events = queue.Queue()
        with monkeypatch.context() as m:
            if prepare is not None:
                m.setattr("rangecontrol.gui.runtime.prepare", prepare)
            IngestJob(config_for(home), events, regenerate=False,
                      provider_factory=factory).run()

        event = events.get_nowait()
        assert event.kind == INGEST, name
        assert event.payload.ok is False, name
        assert expected in event.payload.error, name


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


def test_models_job_publishes_the_list_or_the_reason_it_failed(tmp_path):
    """A dropdown that silently fails to repopulate explains nothing."""
    events = queue.Queue()
    thread = ModelsJob(config_for(tmp_path), events,
                       lister=lambda cfg: ("gemini-9-flash", "gemini-9-pro")).start()
    thread.join(timeout=10)
    assert not thread.is_alive()
    event = events.get_nowait()
    assert event.kind == MODELS
    assert event.payload.ok is True
    assert event.payload.models == ("gemini-9-flash", "gemini-9-pro")

    def boom(cfg):
        raise LLMError("Could not list Gemini models: AuthenticationError")

    ModelsJob(config_for(tmp_path), events, lister=boom).run()
    outcome = events.get_nowait().payload
    assert outcome.ok is False
    assert "AuthenticationError" in outcome.error
