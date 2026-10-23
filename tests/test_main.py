import os

import pytest

from rangecontrol.advisor.engine import Advisor
from rangecontrol.config import load_config
from rangecontrol.llm.base import LLMError
from rangecontrol.main import _decide_regenerate, build_parser, main, prepare
from rangecontrol.range_doc.loader import REQUIRED_SECTIONS
from tests.support.stub_provider import StubProvider

COMPLETE = "\n".join(f"## {name}\n\ncontent\n" for name in REQUIRED_SECTIONS)

BASE_ENV = {
    "DISCORD_BOT_TOKEN": "tok",
    "LLM_PROVIDER": "anthropic",
    "LLM_API_KEY": "key",
    "WHITE_CELL_CHANNEL_ID": "1",
}


def config_for(tmp_path):
    return load_config({**BASE_ENV, "RANGE_DIR": str(tmp_path)})


def with_resources(tmp_path):
    (tmp_path / "resources").mkdir()
    (tmp_path / "resources" / "notes.txt").write_text("segment A", encoding="utf-8")
    return tmp_path


def write_range_md(tmp_path, text=COMPLETE):
    (tmp_path / "Range.md").write_text(text, encoding="utf-8")


def forbid(what):
    def fail(*args, **kwargs):
        pytest.fail(f"{what} must not run here")

    return fail


class FakeSSL:
    """Stands in for main's ``ssl`` module so the smoke check does not depend
    on how this host lays out its CA store (a capath-only store reports zero
    certificates until a handshake loads them)."""

    def __init__(self, ca_count):
        self._ca_count = ca_count

    def create_default_context(self):
        return self

    def get_ca_certs(self):
        return [{"subject": ()}] * self._ca_count


@pytest.fixture
def cli_env(tmp_path, monkeypatch):
    """A configured range directory for driving ``main()`` end to end."""
    for key, value in BASE_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("RANGE_DIR", str(tmp_path))
    return tmp_path


def test_prepare_uses_an_existing_range_md_and_generates_only_when_needed(tmp_path):
    with pytest.raises(RuntimeError) as exc:
        prepare(config_for(tmp_path), StubProvider(), regenerate=False)
    assert "Range.md" in str(exc.value)
    assert "resources" in str(exc.value)

    # A hand-written Range.md is enough on its own.
    write_range_md(tmp_path)
    assert prepare(config_for(tmp_path), StubProvider(), regenerate=False).corpus.docs == ()

    # An existing Range.md costs no provider calls, but the corpus is still built.
    with_resources(tmp_path)
    provider = StubProvider()
    result = prepare(config_for(tmp_path), provider, regenerate=False)
    assert result.generated is False
    assert provider.calls == []
    assert "segment A" in result.corpus.to_prompt_text()

    # --regenerate rebuilds over it, keeping the old one as a backup.
    write_range_md(tmp_path, "stale")
    result = prepare(config_for(tmp_path), StubProvider(completions=[COMPLETE]), regenerate=True)
    assert result.generated is True
    assert (tmp_path / "Range.md.bak").read_text(encoding="utf-8") == "stale"

    (tmp_path / "Range.md").unlink()
    result = prepare(config_for(tmp_path), StubProvider(completions=[COMPLETE]), regenerate=False)
    assert result.generated is True
    assert (tmp_path / "Range.md").is_file()


def test_dry_run_reports_without_connecting_or_offering_a_purge(cli_env, monkeypatch, capsys):
    """--dry-run ingests, prints the report and exits. Even at an interactive
    terminal it must not offer a purge: the old code did, and a "yes" then
    regenerated through the provider."""
    with_resources(cli_env)
    write_range_md(cli_env)
    monkeypatch.setattr("rangecontrol.main.build_provider", lambda cfg: StubProvider())
    monkeypatch.setattr("rangecontrol.main.run_bot", forbid("Connecting to Discord"))
    monkeypatch.setattr("rangecontrol.gui.app.run_gui", forbid("The GUI"))
    monkeypatch.setattr("rangecontrol.main.sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("rangecontrol.main.prompt_purge", forbid("A purge prompt"))

    assert main(["--dry-run"]) == 0
    assert "ingest report" in capsys.readouterr().out.lower()


def test_dry_run_refuses_to_generate_a_fresh_range(cli_env, monkeypatch, capsys):
    """The trap that caused this project's one live-call incident: a fresh
    folder with resources/ but no Range.md yet, and a bare --dry-run. This
    must report the situation and exit, never reach build_provider."""
    with_resources(cli_env)
    monkeypatch.setattr("rangecontrol.main.build_provider", forbid("Building a provider"))

    assert main(["--dry-run"]) == 1
    err = capsys.readouterr().err
    assert "Range.md" in err
    assert "--dry-run" in err


def test_verify_bundle_passes_on_an_empty_environment_and_stays_hidden(monkeypatch, capsys):
    """The smoke check answers "is this installation intact" on a broken or
    freshly unpacked install, so it cannot require a Config. A missed lazy SDK
    import must fail here, not on the first real question."""
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--help"])
    assert "verify-bundle" not in capsys.readouterr().out

    monkeypatch.setattr("rangecontrol.main.ssl", FakeSSL(ca_count=140))
    monkeypatch.setattr(os, "environ", {})
    assert main(["--verify-bundle"]) == 0
    out = capsys.readouterr().out
    assert "RangeTemplate.md" in out
    assert "GUI module imported" in out
    assert "CA store loaded (140 certificates)" in out
    for provider in ("anthropic", "gemini"):
        assert f"OK: {provider} provider constructed" in out


def test_verify_bundle_fails_on_a_missing_template_ca_store_or_provider(monkeypatch, capsys):
    """A binary built on one distribution can see no certificates on another;
    that must surface here, not as a TLS failure when the bot connects."""

    def missing():
        raise FileNotFoundError("RangeTemplate.md is missing from the bundle")

    with monkeypatch.context() as patch:
        patch.setattr("rangecontrol.main.load_template", missing)
        assert main(["--verify-bundle"]) == 1
    assert "missing from the bundle" in capsys.readouterr().err

    with monkeypatch.context() as patch:
        patch.setattr("rangecontrol.main.ssl", FakeSSL(ca_count=0))
        assert main(["--verify-bundle"]) == 1
    assert "no CA certificates are visible" in capsys.readouterr().err

    monkeypatch.setattr("rangecontrol.main.ssl", FakeSSL(ca_count=140))

    def explode(config):
        raise LLMError("no module named anthropic")

    monkeypatch.setattr("rangecontrol.main.build_provider", explode)
    assert main(["--verify-bundle"]) == 1
    assert "anthropic provider failed to construct" in capsys.readouterr().err


def test_accept_path_builds_the_advisor_and_runs(cli_env, monkeypatch):
    with_resources(cli_env)
    write_range_md(cli_env)
    monkeypatch.setattr("rangecontrol.main.build_provider", lambda cfg: StubProvider())
    monkeypatch.setattr("rangecontrol.main.build_gate_provider", lambda cfg: StubProvider())
    started: dict = {}
    monkeypatch.setattr(
        "rangecontrol.main.run_bot",
        lambda config, advisor, audit: started.update(config=config, advisor=advisor, audit=audit),
    )

    assert main(["--yes"]) == 0
    assert isinstance(started["advisor"], Advisor)
    assert started["config"].discord_bot_token == "tok"
    assert started["audit"].path_for_today().parent.name == ".rangecontrol"


def test_startup_failures_print_cleanly_and_exit_one(cli_env, monkeypatch, capsys):
    """Missing config, an LLM outage or bad key, a rejected Discord token and
    an unreadable Range.md are all misconfiguration, not crashes."""

    def fails_cleanly(argv, prefix, needle):
        assert main(argv) == 1
        err = capsys.readouterr().err
        assert err.startswith(prefix)
        assert needle in err
        assert "Traceback" not in err

    with_resources(cli_env)
    monkeypatch.setattr("rangecontrol.main.load_dotenv", lambda *a, **k: None)

    with monkeypatch.context() as patch:
        patch.delenv("DISCORD_BOT_TOKEN")
        fails_cleanly(["--dry-run"], "Configuration error:", "DISCORD_BOT_TOKEN")

    with monkeypatch.context() as patch:
        patch.setattr(
            "rangecontrol.main.build_provider",
            lambda cfg: StubProvider(error=LLMError("Anthropic request failed: AuthenticationError")),
        )
        fails_cleanly(["--regenerate", "--dry-run"], "Startup failed:", "AuthenticationError")

    write_range_md(cli_env)
    with monkeypatch.context() as patch:
        patch.setattr("rangecontrol.main.build_provider", lambda cfg: StubProvider())
        patch.setattr("rangecontrol.main.build_gate_provider", lambda cfg: StubProvider())

        def reject(config, advisor, audit):
            raise RuntimeError("Discord rejected DISCORD_BOT_TOKEN. Use the token from ...")

        patch.setattr("rangecontrol.main.run_bot", reject)
        fails_cleanly(["--yes"], "Startup failed:", "DISCORD_BOT_TOKEN")

    if os.geteuid() != 0:  # root ignores permission bits
        range_md = cli_env / "Range.md"
        range_md.chmod(0o000)
        try:
            fails_cleanly(["--dry-run"], "Startup failed:", "Range.md")
        finally:
            range_md.chmod(0o644)


def test_a_purge_is_offered_only_to_an_interactive_operator_with_state(tmp_path, monkeypatch):
    """Between exercises the operator is asked whether to discard the last
    run's state. Unattended runs (--yes, systemd, containers) must never
    discard it, --dry-run must never spend calls, and a first run has
    nothing to ask about."""
    with_resources(tmp_path)
    config = config_for(tmp_path)
    parse = build_parser().parse_args
    asked = []

    def answering(reply):
        monkeypatch.setattr(
            "rangecontrol.main.prompt_purge",
            lambda state, *a, **k: asked.append(state) or reply,
        )

    monkeypatch.setattr("rangecontrol.main.sys.stdin.isatty", lambda: True)
    answering(True)
    assert _decide_regenerate(parse([]), config) is False
    assert asked == []

    write_range_md(tmp_path)
    answering(False)
    assert _decide_regenerate(parse([]), config) is False
    answering(True)
    assert _decide_regenerate(parse([]), config) is True
    assert len(asked) == 2

    assert _decide_regenerate(parse(["--yes"]), config) is False
    assert _decide_regenerate(parse(["--dry-run"]), config) is False
    assert _decide_regenerate(parse(["--regenerate"]), config) is True
    monkeypatch.setattr("rangecontrol.main.sys.stdin.isatty", lambda: False)
    assert _decide_regenerate(parse([]), config) is False
    assert len(asked) == 2
