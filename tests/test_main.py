import os

import pytest

from rangecontrol.advisor.engine import Advisor
from rangecontrol.config import load_config
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


def test_parser_exposes_the_three_switches():
    args = build_parser().parse_args(["--yes", "--regenerate", "--dry-run"])
    assert args.yes and args.regenerate and args.dry_run


def test_parser_defaults_all_switches_off():
    args = build_parser().parse_args([])
    assert not args.yes and not args.regenerate and not args.dry_run


def test_loads_existing_range_md_without_generating(tmp_path):
    with_resources(tmp_path)
    (tmp_path / "Range.md").write_text(COMPLETE, encoding="utf-8")
    provider = StubProvider(completions=[])
    result = prepare(config_for(tmp_path), provider, regenerate=False)
    assert result.generated is False
    assert provider.calls == []


def test_corpus_is_built_even_when_range_md_exists(tmp_path):
    with_resources(tmp_path)
    (tmp_path / "Range.md").write_text(COMPLETE, encoding="utf-8")
    result = prepare(config_for(tmp_path), StubProvider(), regenerate=False)
    assert "segment A" in result.corpus.to_prompt_text()


def test_generates_range_md_when_absent(tmp_path):
    with_resources(tmp_path)
    provider = StubProvider(completions=[COMPLETE])
    result = prepare(config_for(tmp_path), provider, regenerate=False)
    assert result.generated is True
    assert (tmp_path / "Range.md").is_file()


def test_regenerate_overrides_an_existing_range_md(tmp_path):
    with_resources(tmp_path)
    (tmp_path / "Range.md").write_text("stale", encoding="utf-8")
    provider = StubProvider(completions=[COMPLETE])
    result = prepare(config_for(tmp_path), provider, regenerate=True)
    assert result.generated is True
    assert (tmp_path / "Range.md.bak").read_text(encoding="utf-8") == "stale"


def test_missing_range_md_and_resources_raises_a_clear_error(tmp_path):
    with pytest.raises(RuntimeError) as exc:
        prepare(config_for(tmp_path), StubProvider(), regenerate=False)
    message = str(exc.value)
    assert "Range.md" in message
    assert "resources" in message


def test_range_md_without_resources_is_allowed(tmp_path):
    (tmp_path / "Range.md").write_text(COMPLETE, encoding="utf-8")
    result = prepare(config_for(tmp_path), StubProvider(), regenerate=False)
    assert result.corpus.docs == ()


def test_dry_run_exits_zero_without_connecting(tmp_path, monkeypatch, capsys):
    with_resources(tmp_path)
    (tmp_path / "Range.md").write_text(COMPLETE, encoding="utf-8")
    monkeypatch.setenv("RANGE_DIR", str(tmp_path))
    for key, value in BASE_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(
        "rangecontrol.main.build_provider", lambda cfg: StubProvider()
    )
    monkeypatch.setattr(
        "rangecontrol.main.build_gate_provider", lambda cfg: StubProvider()
    )

    def explode(*args, **kwargs):
        raise AssertionError("dry-run must not connect to Discord")

    monkeypatch.setattr("rangecontrol.main.run_bot", explode)
    assert main(["--dry-run"]) == 0
    assert "ingest report" in capsys.readouterr().out.lower()


def test_dotenv_is_loaded_without_overriding_the_real_environment(tmp_path, monkeypatch):
    """load_dotenv must run ahead of load_config regardless of which CLI verb
    is used; the choice of ``--dry-run`` below is arbitrary -- any non-empty
    argv reaches this code, since an empty argv now opens the GUI instead
    (see test_no_arguments_launches_the_gui)."""
    import rangecontrol.main as main_module

    calls = []
    monkeypatch.setattr(
        main_module, "load_dotenv", lambda path, **kw: calls.append((path, kw))
    )
    monkeypatch.setattr(main_module, "load_config", _raise_config_error)
    main(["--dry-run"])
    assert len(calls) == 1
    path, kwargs = calls[0]
    # The app home's .env, so the CLI and the window read the same file;
    # interpolate=False so "${...}" in EXTRA_INSTRUCTIONS stays literal, as
    # the GUI's own parser already treats it.
    assert path.name == ".env"
    assert kwargs == {"override": False, "interpolate": False}


def test_provider_failure_during_startup_prints_cleanly(tmp_path, monkeypatch, capsys):
    """--regenerate --dry-run (not bare --dry-run): with no Range.md, a bare
    --dry-run is now refused outright before ever reaching the provider (see
    test_dry_run_refuses_to_generate_a_fresh_range below) -- this test's own
    job is to prove a genuine provider failure still prints cleanly once the
    provider is actually reached, and forcing a rebuild with --regenerate is
    the one way an explicit request to reach it under --dry-run remains."""
    from rangecontrol.llm.base import LLMError

    with_resources(tmp_path)
    for key, value in BASE_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("RANGE_DIR", str(tmp_path))
    monkeypatch.setattr(
        "rangecontrol.main.build_provider",
        lambda cfg: StubProvider(error=LLMError("Anthropic request failed: AuthenticationError")),
    )
    assert main(["--regenerate", "--dry-run"]) == 1
    err = capsys.readouterr().err
    assert err.startswith("Startup failed:")
    assert "Traceback" not in err


# --- I4: --dry-run must never generate a fresh Range.md ---------------------


def test_dry_run_refuses_to_generate_a_fresh_range(tmp_path, monkeypatch, capsys):
    """The trap that caused this project's one live-call incident: a fresh
    folder with resources/ but no Range.md yet, and a bare --dry-run. This
    must report the situation and exit, never reach build_provider."""
    with_resources(tmp_path)
    for key, value in BASE_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("RANGE_DIR", str(tmp_path))

    def boom(cfg):
        raise AssertionError("--dry-run must never construct a provider with no Range.md")

    monkeypatch.setattr("rangecontrol.main.build_provider", boom)

    assert main(["--dry-run"]) == 1
    err = capsys.readouterr().err
    assert "Range.md" in err
    assert "--dry-run" in err


def test_dry_run_reports_an_unreadable_range_md_cleanly(tmp_path, monkeypatch, capsys):
    """_dry_run_would_generate calls load_range_md() outside of prepare()'s
    own OSError handling -- an unreadable Range.md (a locked file, a
    permissions mistake) must not raise a raw traceback one module over
    from where C1 fixed the exact same class of bug."""
    if os.geteuid() == 0:
        pytest.skip("root ignores permission bits")

    with_resources(tmp_path)
    range_md = tmp_path / "Range.md"
    range_md.write_text(COMPLETE, encoding="utf-8")
    range_md.chmod(0o000)
    for key, value in BASE_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("RANGE_DIR", str(tmp_path))

    try:
        assert main(["--dry-run"]) == 1
    finally:
        range_md.chmod(0o644)

    err = capsys.readouterr().err
    assert err.startswith("Startup failed:")
    assert "Traceback" not in err


def test_dry_run_still_reports_normally_once_range_md_exists(tmp_path, monkeypatch, capsys):
    with_resources(tmp_path)
    (tmp_path / "Range.md").write_text(COMPLETE, encoding="utf-8")
    for key, value in BASE_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("RANGE_DIR", str(tmp_path))
    monkeypatch.setattr("rangecontrol.main.build_provider", lambda cfg: StubProvider())

    assert main(["--dry-run"]) == 0
    assert "ingest report" in capsys.readouterr().out.lower()


def test_dry_run_with_explicit_regenerate_still_forces_a_rebuild(tmp_path, monkeypatch):
    """An explicit --regenerate --dry-run is a deliberate request, not the
    "first run in a fresh folder" trap -- it must still reach the (stubbed,
    never-real) provider, exactly as it already did before this fix."""
    with_resources(tmp_path)
    for key, value in BASE_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("RANGE_DIR", str(tmp_path))
    monkeypatch.setattr(
        "rangecontrol.main.build_provider", lambda cfg: StubProvider(completions=[COMPLETE])
    )

    assert main(["--regenerate", "--dry-run"]) == 0


def test_accept_path_builds_the_advisor_and_runs(tmp_path, monkeypatch):
    with_resources(tmp_path)
    (tmp_path / "Range.md").write_text(COMPLETE, encoding="utf-8")
    for key, value in BASE_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("RANGE_DIR", str(tmp_path))
    monkeypatch.setattr("rangecontrol.main.build_provider", lambda cfg: StubProvider())
    monkeypatch.setattr("rangecontrol.main.build_gate_provider", lambda cfg: StubProvider())

    started: dict = {}
    monkeypatch.setattr(
        "rangecontrol.main.run_bot",
        lambda config, advisor, audit: started.update(
            config=config, advisor=advisor, audit=audit
        ),
    )

    assert main(["--yes"]) == 0
    assert isinstance(started["advisor"], Advisor)
    assert started["config"].discord_bot_token == "tok"
    assert started["audit"].path_for_today().parent.name == ".rangecontrol"


def test_cache_status_subcommand_exits_zero(tmp_path, monkeypatch, capsys):
    with_resources(tmp_path)
    for key, value in BASE_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("RANGE_DIR", str(tmp_path))
    assert main(["cache-status"]) == 0
    assert "Resources discovered" in capsys.readouterr().out


def test_verify_bundle_flag_exits_zero(tmp_path, monkeypatch, capsys):
    with_resources(tmp_path)
    for key, value in BASE_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("RANGE_DIR", str(tmp_path))
    assert main(["--verify-bundle"]) == 0
    assert "RangeTemplate.md" in capsys.readouterr().out


def test_verify_bundle_flag_is_hidden_from_help(capsys):
    """No task has decided end users should see this as a public verb: it
    exists solely so smoke.sh has a code path that genuinely calls
    load_template(). A prior attempt suppressed it via a subparser's
    help=argparse.SUPPRESS, which this interpreter's argparse does not fully
    honor for subparsers (it still lists the name in the usage choices and
    prints a literal "==SUPPRESS==" line) — pinning the flag form instead,
    where SUPPRESS is fully honored.
    """
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--help"])
    out = capsys.readouterr().out
    assert "verify-bundle" not in out
    assert "verify_bundle" not in out


def test_verify_bundle_works_with_an_empty_environment(monkeypatch, capsys):
    """verify-bundle is the "is this installation intact" check; gating it
    behind a full Config (Discord token, LLM provider, API key, channel ID)
    would mean it can never run on a broken or freshly-unpacked install,
    which is exactly when an operator most needs it.
    """
    monkeypatch.setattr(os, "environ", {})
    assert main(["--verify-bundle"]) == 0
    assert "RangeTemplate.md" in capsys.readouterr().out


def test_verify_bundle_flag_reports_a_missing_template(
    tmp_path, monkeypatch, capsys
):
    with_resources(tmp_path)
    for key, value in BASE_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("RANGE_DIR", str(tmp_path))

    def _missing():
        raise FileNotFoundError("RangeTemplate.md is missing from the bundle")

    monkeypatch.setattr("rangecontrol.main.load_template", _missing)
    assert main(["--verify-bundle"]) == 1
    assert "missing from the bundle" in capsys.readouterr().err


def test_verify_bundle_constructs_every_provider(capsys):
    """A missed lazy SDK import must fail here, not on the first real question."""
    assert main(["--verify-bundle"]) == 0
    out = capsys.readouterr().out
    for provider in ("anthropic", "gemini"):
        assert provider in out


def test_verify_bundle_reports_a_broken_provider(monkeypatch, capsys):
    from rangecontrol.llm.base import LLMError

    def explode(config):
        raise LLMError("no module named anthropic")

    monkeypatch.setattr("rangecontrol.main.build_provider", explode)
    assert main(["--verify-bundle"]) == 1
    assert "anthropic" in capsys.readouterr().err.lower()


def test_bad_config_exits_one(monkeypatch, capsys):
    """A config failure exits 1 regardless of which CLI verb triggered it;
    ``--dry-run`` here stands in for "any argument", since an empty argv now
    opens the GUI instead of reaching load_config at all."""
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
    monkeypatch.setattr("rangecontrol.main.load_config", _raise_config_error)
    assert main(["--dry-run"]) == 1
    assert "DISCORD_BOT_TOKEN" in capsys.readouterr().err


def _raise_config_error(*args, **kwargs):
    from rangecontrol.config import ConfigError

    raise ConfigError("DISCORD_BOT_TOKEN is required but not set.")


def test_discord_connection_failure_prints_cleanly(tmp_path, monkeypatch, capsys):
    """A rejected token or missing intent must read like any other startup failure."""
    with_resources(tmp_path)
    (tmp_path / "Range.md").write_text(COMPLETE, encoding="utf-8")
    for key, value in BASE_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("RANGE_DIR", str(tmp_path))
    monkeypatch.setattr("rangecontrol.main.build_provider", lambda cfg: StubProvider())
    monkeypatch.setattr("rangecontrol.main.build_gate_provider", lambda cfg: StubProvider())

    def reject(config, advisor, audit):
        raise RuntimeError("Discord rejected DISCORD_BOT_TOKEN. Use the token from ...")

    monkeypatch.setattr("rangecontrol.main.run_bot", reject)

    assert main(["--yes"]) == 1
    err = capsys.readouterr().err
    assert err.startswith("Startup failed:")
    assert "DISCORD_BOT_TOKEN" in err
    assert "Traceback" not in err


# --- startup purge offer ---------------------------------------------------


def _stub_env(tmp_path, monkeypatch):
    for key, value in BASE_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("RANGE_DIR", str(tmp_path))
    monkeypatch.setattr(
        "rangecontrol.main.build_provider",
        lambda cfg: StubProvider(completions=[COMPLETE]),
    )
    monkeypatch.setattr(
        "rangecontrol.main.build_gate_provider", lambda cfg: StubProvider()
    )
    monkeypatch.setattr("rangecontrol.main.run_bot", lambda *a, **k: None)
    # The purge-offer tests below exercise the full startup path (no
    # --dry-run, since --dry-run now never offers to purge -- see
    # test_dry_run_never_offers_to_purge), which reaches confirm()'s real
    # input() prompt unless stubbed. --dry-run tests never reach this: they
    # return before confirm() runs.
    monkeypatch.setattr("rangecontrol.main.confirm", lambda *a, **k: True)


def _record_prepare(monkeypatch):
    """Capture the regenerate flag main() decides on."""
    seen = {}
    real = prepare

    def spy(config, provider, *, regenerate):
        seen["regenerate"] = regenerate
        return real(config, provider, regenerate=regenerate)

    monkeypatch.setattr("rangecontrol.main.prepare", spy)
    return seen


def _asked(monkeypatch, answer=""):
    """Force an interactive terminal and answer the purge prompt."""
    asked = {"count": 0}
    monkeypatch.setattr("rangecontrol.main.sys.stdin.isatty", lambda: True)

    def fake_prompt(state, *args, **kwargs):
        asked["count"] += 1
        asked["state"] = state
        return answer == "y"

    monkeypatch.setattr("rangecontrol.main.prompt_purge", fake_prompt)
    return asked


def test_startup_offers_to_purge_when_state_already_exists(tmp_path, monkeypatch):
    """This feature only fires on the branch of _decide_regenerate reached
    when --regenerate, --yes, and --dry-run are all unset -- previously
    exercised via ``main([])``, the bare "start the bot" invocation. Now that
    an empty argv opens the GUI instead of reaching the CLI at all, there is
    no argv left that produces that exact Namespace through main(): every
    other flag either sets one of those three or dispatches to a subcommand.
    _decide_regenerate is the function that actually implements the
    purge-offer decision, so calling it directly with the same
    all-defaults Namespace argparse would produce is the narrowest way to
    keep testing this behaviour.
    """
    with_resources(tmp_path)
    (tmp_path / "Range.md").write_text(COMPLETE, encoding="utf-8")
    config = config_for(tmp_path)
    args = build_parser().parse_args([])
    asked = _asked(monkeypatch, answer="n")

    assert _decide_regenerate(args, config) is False
    assert asked["count"] == 1


def test_accepting_the_purge_offer_regenerates(tmp_path, monkeypatch):
    """See test_startup_offers_to_purge_when_state_already_exists for why
    this calls _decide_regenerate directly instead of main([])."""
    with_resources(tmp_path)
    (tmp_path / "Range.md").write_text(COMPLETE, encoding="utf-8")
    config = config_for(tmp_path)
    args = build_parser().parse_args([])
    _asked(monkeypatch, answer="y")

    assert _decide_regenerate(args, config) is True


def test_dry_run_never_offers_to_purge(tmp_path, monkeypatch):
    """--dry-run's contract is to ingest, print the report, and exit without
    changing anything or spending API calls. Regression guard for the bug
    where _decide_regenerate ran (and could prompt to purge, then regenerate
    through the provider on "yes") before main()'s ``if args.dry_run``
    early return was ever reached -- simulating an interactive terminal on
    purpose, since that is exactly the condition under which the old code
    would have prompted.
    """
    with_resources(tmp_path)
    (tmp_path / "Range.md").write_text(COMPLETE, encoding="utf-8")
    _stub_env(tmp_path, monkeypatch)
    seen = _record_prepare(monkeypatch)
    monkeypatch.setattr("rangecontrol.main.sys.stdin.isatty", lambda: True)

    def fail_if_called(*args, **kwargs):
        pytest.fail("prompt_purge must not be called under --dry-run")

    monkeypatch.setattr("rangecontrol.main.prompt_purge", fail_if_called)

    assert main(["--dry-run"]) == 0
    assert seen["regenerate"] is False


def test_no_offer_when_there_is_nothing_to_purge(tmp_path, monkeypatch):
    """A first run has no previous state, so there is no question to ask."""
    with_resources(tmp_path)
    _stub_env(tmp_path, monkeypatch)
    asked = _asked(monkeypatch)

    main(["--dry-run"])
    assert asked["count"] == 0


def test_no_offer_when_regenerate_was_already_requested(tmp_path, monkeypatch):
    with_resources(tmp_path)
    (tmp_path / "Range.md").write_text(COMPLETE, encoding="utf-8")
    _stub_env(tmp_path, monkeypatch)
    seen = _record_prepare(monkeypatch)
    asked = _asked(monkeypatch)

    main(["--regenerate", "--dry-run"])
    assert asked["count"] == 0
    assert seen["regenerate"] is True


def test_no_offer_under_yes(tmp_path, monkeypatch):
    """--yes means unattended, and unattended must never discard state."""
    with_resources(tmp_path)
    (tmp_path / "Range.md").write_text(COMPLETE, encoding="utf-8")
    _stub_env(tmp_path, monkeypatch)
    seen = _record_prepare(monkeypatch)
    asked = _asked(monkeypatch, answer="y")

    main(["--yes"])
    assert asked["count"] == 0
    assert seen["regenerate"] is False


def test_no_offer_when_stdin_is_not_a_terminal(tmp_path, monkeypatch):
    """Under systemd or a container there is nobody to answer the prompt."""
    with_resources(tmp_path)
    (tmp_path / "Range.md").write_text(COMPLETE, encoding="utf-8")
    _stub_env(tmp_path, monkeypatch)
    monkeypatch.setattr("rangecontrol.main.sys.stdin.isatty", lambda: False)
    called = {"n": 0}
    monkeypatch.setattr(
        "rangecontrol.main.prompt_purge",
        lambda *a, **k: called.__setitem__("n", called["n"] + 1) or True,
    )

    main(["--dry-run"])
    assert called["n"] == 0


# --- dual-mode entry point --------------------------------------------------


def test_no_arguments_launches_the_gui(monkeypatch):
    called = {"n": 0}
    monkeypatch.setattr("rangecontrol.gui.app.run_gui",
                        lambda: called.__setitem__("n", called["n"] + 1) or 0)
    assert main([]) == 0
    assert called["n"] == 1


def test_arguments_keep_the_cli_behaviour(tmp_path, monkeypatch, capsys):
    """PREGENERATE.md drives cache-status; the binary must still answer it."""
    monkeypatch.setattr("rangecontrol.gui.app.run_gui",
                        lambda: pytest.fail("GUI must not launch with arguments"))
    with_resources(tmp_path)
    for key, value in BASE_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("RANGE_DIR", str(tmp_path))
    assert main(["cache-status"]) == 0
    assert "cache" in capsys.readouterr().out.lower()


def test_vendor_loggers_are_quietened():
    import logging

    from rangecontrol.main import quieten_vendor_logs

    quieten_vendor_logs()
    for name in ("google_genai", "httpx", "anthropic", "discord"):
        assert logging.getLogger(name).level >= logging.WARNING
