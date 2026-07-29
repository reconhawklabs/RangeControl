"""CLI and startup orchestration."""

from __future__ import annotations

import argparse
import logging
import os
import ssl
import sys
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from rangecontrol.advisor.engine import Advisor
from rangecontrol.audit.log import AuditLog
from rangecontrol.config import (
    Config,
    ConfigError,
    DEFAULT_MODELS,
    VALID_PROVIDERS,
    load_config,
)
from rangecontrol.ingest.cache import ExtractionCache
from rangecontrol.ingest.corpus import Corpus
from rangecontrol.ingest.discovery import build_corpus
from rangecontrol.llm.base import LLMError, Provider
from rangecontrol.llm.factory import build_gate_provider, build_provider
from rangecontrol.gui.paths import bundle_root, is_frozen
from rangecontrol.pregen import cache_put, cache_status, format_status
from rangecontrol.purge import inspect_existing, prompt_purge
from rangecontrol.range_doc.generator import generate_range_md, write_range_md
from rangecontrol.range_doc.loader import load_range_md, load_template
from rangecontrol.range_doc.report import IngestReport, build_report, confirm

logger = logging.getLogger(__name__)

STATE_DIR = ".rangecontrol"

_VENDOR_LOGGERS = ("google_genai", "httpx", "httpcore", "anthropic", "discord")


def quieten_vendor_logs() -> None:
    """Stop per-request SDK chatter burying RangeControl's own messages.

    google-genai logs "AFC is enabled" on every call and httpx logs every
    request, so a single question produces four lines of noise. None of it is
    actionable, and in the GUI it would flood the console feed.
    """
    for name in _VENDOR_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)


@dataclass(frozen=True)
class StartupResult:
    range_md: str
    corpus: Corpus
    report: IngestReport
    generated: bool


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rangecontrol",
        description="Change-request advisor for cyber security range exercises",
    )
    parser.add_argument(
        "--yes", action="store_true", help="skip the ingest confirmation gate"
    )
    parser.add_argument(
        "--regenerate",
        action="store_true",
        help="force a fresh Range.md, ignoring an existing file and the cache",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="ingest and print the report, then exit without connecting to Discord",
    )
    # Packaging tooling, not a product feature: exists only so smoke.sh has a
    # code path that genuinely calls load_template(), because neither
    # --dry-run nor cache-status reach it when a Range.md is already present.
    # A plain flag rather than a subcommand: argparse.SUPPRESS on a subparser
    # does not fully hide it (the name still appears in the usage line's
    # choices, and Python prints a literal "==SUPPRESS==" help line for it)
    # — verified against this interpreter before choosing this shape instead.
    # As a top-level flag, SUPPRESS hides it completely, which is the point:
    # no task has decided end users should see or rely on this as a public
    # verb.
    parser.add_argument(
        "--verify-bundle", action="store_true", help=argparse.SUPPRESS
    )

    sub = parser.add_subparsers(dest="command")

    status = sub.add_parser(
        "cache-status", help="report which resources already have a cache entry"
    )
    status.set_defaults(command="cache-status")

    put = sub.add_parser(
        "cache-put", help="record externally-extracted text for one resource"
    )
    put.add_argument("resource", type=Path)
    group = put.add_mutually_exclusive_group(required=True)
    group.add_argument("--text", help="extracted text (single line)")
    group.add_argument("--text-file", type=Path, help="file holding the extracted text")
    put.set_defaults(command="cache-put")

    return parser


def _paths(config: Config) -> tuple[Path, Path, Path]:
    root = config.range_dir
    return root / "resources", root / STATE_DIR / "cache", root / STATE_DIR


def prepare(config: Config, provider: Provider, *, regenerate: bool) -> StartupResult:
    """Load or generate Range.md and build the corpus."""
    resources_dir, cache_dir, _ = _paths(config)

    cache = ExtractionCache(cache_dir)
    if regenerate:
        cache.clear()

    existing = None if regenerate else load_range_md(config.range_dir)
    if existing is None and not resources_dir.is_dir():
        raise RuntimeError(
            f"Neither Range.md nor a resources/ directory was found in "
            f"{config.range_dir.resolve()}.\n"
            "Place your range material in resources/, or supply a Range.md by hand."
        )

    corpus = build_corpus(resources_dir, provider, cache)

    if existing is not None:
        return StartupResult(
            range_md=existing,
            corpus=corpus,
            report=build_report(corpus, existing, generated=False),
            generated=False,
        )

    generated = generate_range_md(corpus, provider)
    write_range_md(config.range_dir, generated)
    return StartupResult(
        range_md=generated,
        corpus=corpus,
        report=build_report(corpus, generated, generated=True),
        generated=True,
    )


def run_bot(config: Config, advisor: Advisor, audit: AuditLog) -> None:  # pragma: no cover
    """Connect to Discord and block. Excluded from coverage — needs a gateway.

    Connection failures are translated to RuntimeError with actionable text so
    ``main`` reports them the same way it reports every other startup failure.
    A rejected token and a missing privileged intent are both misconfiguration,
    not crashes, and a stack trace tells an operator nothing about the fix.
    """
    import discord

    from rangecontrol.bot.client import RangeControlBot

    try:
        RangeControlBot(config=config, advisor=advisor, audit=audit).run(
            config.discord_bot_token
        )
    except discord.LoginFailure as exc:
        raise RuntimeError(
            "Discord rejected DISCORD_BOT_TOKEN. Use the token from "
            "Developer Portal -> your application -> Bot -> Reset Token. "
            "The Application ID, Public Key, and Client Secret will not work."
        ) from exc
    except discord.PrivilegedIntentsRequired as exc:
        raise RuntimeError(
            "Discord refused the Message Content intent, which RangeControl "
            "needs to read @mentions. Enable it under Developer Portal -> "
            "your application -> Bot -> Privileged Gateway Intents -> "
            "Message Content Intent, then start again."
        ) from exc


def _run_cache_command(args, config: Config) -> int:
    resources_dir, cache_dir, _ = _paths(config)
    if args.command == "cache-status":
        print(format_status(cache_status(resources_dir, cache_dir)))
        return 0

    text = (
        args.text_file.read_text(encoding="utf-8")
        if args.text_file is not None
        else args.text
    )
    recorded = cache_put(resources_dir, cache_dir, args.resource, text)
    print(f"Cached {recorded}")
    return 0


def _decide_regenerate(args, config: Config) -> bool:
    """Resolve whether this run rebuilds Range.md and the cache from scratch.

    ``--regenerate`` decides it outright. Otherwise the operator is offered the
    choice, but only when there is state to discard and only when someone is
    there to answer: ``--yes``, ``--dry-run``, and a non-terminal stdin all
    mean no purge should happen. This function only ever decides whether to
    *ask about discarding existing state* -- it does not by itself guarantee
    ``--dry-run`` never spends an API call. That guarantee (no Range.md yet,
    so there is nothing to report and nothing should be generated to make
    one) is enforced separately, in ``main()``, by ``_dry_run_would_generate``
    ahead of ever calling ``prepare()``.
    """
    if args.regenerate:
        return True
    if args.yes or args.dry_run or not sys.stdin.isatty():
        return False

    _, cache_dir, _ = _paths(config)
    state = inspect_existing(config.range_dir, cache_dir)
    if not state.exists():
        return False
    return prompt_purge(state)


def _dry_run_would_generate(args, config: Config) -> bool:
    """True when this ``--dry-run`` would fall through to generate_range_md.

    Mirrors ``prepare()``'s own ``existing is None`` decision without
    duplicating its cache/corpus-building side effects: this must be
    knowable before ``build_provider`` or ``prepare`` ever runs, since
    generating a fresh Range.md is exactly the API spend ``--dry-run``
    promises never to cause.

    Deliberately excludes the ``--regenerate`` case: ``--regenerate
    --dry-run`` is an explicit, unambiguous request to rebuild from
    scratch, not the "first run in a fresh folder" trap this guard exists
    to catch, so it is left to behave as it always has.
    """
    return not args.regenerate and load_range_md(config.range_dir) is None


# Where mainstream distributions keep their CA bundle. Probed in order, and
# preferred over the copy inside the binary: the system store is maintained by
# the OS package manager, so it keeps getting new roots and dropping expired
# ones long after this binary was built. The bundled copy is frozen at build
# time and will eventually go stale — root CAs do expire, and issuers rotate.
_SYSTEM_CA_FILES = (
    "/etc/ssl/certs/ca-certificates.crt",              # Debian, Ubuntu, Fedora, Arch
    "/etc/pki/tls/certs/ca-bundle.crt",                # RHEL, Rocky, Alma, Fedora
    "/etc/ssl/ca-bundle.pem",                          # openSUSE
    "/etc/ssl/cert.pem",                               # Alpine, BSD, macOS ports
    "/etc/pki/tls/cert.pem",                           # older RHEL
    "/etc/ca-certificates/extracted/tls-ca-bundle.pem",  # Arch
)


def _system_ca_file() -> str | None:
    """First maintained CA bundle this platform actually has, or None."""
    for candidate in _SYSTEM_CA_FILES:
        if os.path.isfile(candidate):
            return candidate
    return None


def _bundled_ca_file() -> str | None:
    """Path to the CA bundle shipped inside the binary, or None if absent."""
    candidate = bundle_root() / "certifi" / "cacert.pem"
    if candidate.is_file():
        return str(candidate)
    try:
        import certifi
    except ImportError:
        return None
    where = certifi.where()
    return where if os.path.exists(where) else None


def _platform_store_works() -> bool:
    """True when a default SSL context actually loads certificates.

    Measured, not inferred from OpenSSL's compiled-in paths. On Windows those
    paths are placeholders that never exist, yet create_default_context() loads
    the Windows certificate store just fine — inferring from the paths there
    would discard an enterprise CA in favour of certifi and break exactly the
    environments that need their own root most.
    """
    try:
        return bool(ssl.create_default_context().get_ca_certs())
    except Exception:  # noqa: BLE001 - a probe must never stop startup
        return False


def ensure_ca_bundle() -> None:
    """Point OpenSSL at the bundled CA store when the platform's is missing.

    The Linux binary is built in Debian bookworm so it links a glibc old enough
    to run on Ubuntu 22.04 and RHEL 9. That bakes Debian's OpenSSL default CA
    paths (/usr/lib/ssl/...) into the bundle — and those do not exist on
    Fedora, RHEL, Rocky, Alma, openSUSE or Arch, where the default context then
    loads zero certificates and every connection to Discord fails with
    CERTIFICATE_VERIFY_FAILED.

    certifi's bundle ships inside the binary, so fall back to it. Only when
    frozen, only when the platform's own store is genuinely unusable, and never
    over an operator's own SSL_CERT_FILE — someone pointing at a corporate CA
    must keep it.
    """
    if not is_frozen() or os.environ.get("SSL_CERT_FILE"):
        return
    if _platform_store_works():
        return

    # Prefer the platform's own store. It is the one that keeps receiving new
    # roots and dropping expired ones through ordinary system updates, so a
    # binary built today still trusts the right issuers years from now.
    system = _system_ca_file()
    if system is not None:
        os.environ["SSL_CERT_FILE"] = system
        logger.info("Using the system CA store (%s)", system)
        return

    bundled = _bundled_ca_file()
    if bundled is None:
        # Not fatal: better to fail later with a clear TLS error than to
        # refuse to start over a CA store the operator may not even need.
        logger.warning(
            "No system CA store found and no bundled one either; TLS "
            "connections will likely fail."
        )
        return

    # Last resort. Frozen at build time, so it ages: if this line ever appears
    # on a machine that later fails TLS, the fix is a newer build or an
    # explicit SSL_CERT_FILE.
    os.environ["SSL_CERT_FILE"] = bundled
    logger.warning(
        "No system CA store found; falling back to the bundle inside this "
        "binary (%s). It was frozen at build time and will age - prefer "
        "installing your distribution's ca-certificates package.",
        bundled,
    )


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    quieten_vendor_logs()

    # Before anything can construct an SSL context - the GUI, the bot, or a
    # provider client. Setting it afterwards would be too late.
    ensure_ca_bundle()

    # argv defaults to None from the console-script entry point, so resolve it
    # once here rather than letting argparse do it later - the GUI decision
    # needs to know whether any argument was given.
    effective = sys.argv[1:] if argv is None else argv

    # No arguments means a double-click, so open the window. Any argument at
    # all keeps the original CLI, which PREGENERATE.md and existing scripts
    # depend on.
    if not effective:
        from rangecontrol.gui.app import run_gui

        return run_gui()

    args = build_parser().parse_args(effective)

    if args.verify_bundle:
        # Ahead of load_config() on purpose: the whole point of this check is
        # to answer "is this installation intact" without first requiring a
        # Discord token, LLM provider, API key, and channel ID to already be
        # configured correctly.
        try:
            load_template()
        except FileNotFoundError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1
        print("OK: RangeTemplate.md loaded from the bundle.")

        # Importing the GUI module exercises the Tk import and every hidden
        # import rangecontrol.spec collects for it, without opening a window
        # or needing a display -- run_gui() itself is never called, only
        # imported. This is the check packaging/smoke.sh's --help check
        # cannot be: --help takes the argparse branch in main() below and
        # never reaches `from rangecontrol.gui.app import run_gui` at all
        # (confirmed experimentally: stubbing tkinter to None in sys.modules
        # and calling main(["--help"]) still exits 0), so a missing Tk or a
        # missed gui/ hidden import would otherwise only ever surface in
        # front of a user double-clicking the icon.
        try:
            from rangecontrol.gui.app import run_gui  # noqa: F401
        except Exception as exc:  # noqa: BLE001 - any import failure counts
            print(f"Error: GUI module failed to import: {exc}", file=sys.stderr)
            return 1
        print("OK: GUI module imported.")

        # Report the CA store: a binary built on one distribution can find no
        # certificates on another, which surfaces only as a TLS failure when
        # the bot tries to connect. Make it visible before that happens.
        try:
            context = ssl.create_default_context()
            count = len(context.get_ca_certs())
        except Exception as exc:  # noqa: BLE001 - diagnostics must not crash
            print(f"Error: could not build an SSL context: {exc}", file=sys.stderr)
            return 1
        if count == 0:
            print(
                "Error: no CA certificates are visible; TLS connections will "
                "fail. Set SSL_CERT_FILE to a CA bundle.",
                file=sys.stderr,
            )
            return 1
        print(f"OK: CA store loaded ({count} certificates).")

        # build_provider() already runs inside a frozen bundle for whichever
        # provider LLM_PROVIDER selects, every time a non-cache command loads
        # a real Config (see the call below at line ~272) -- so
        # packaging/smoke.sh's --dry-run check, which always exports
        # LLM_PROVIDER=anthropic, already exercised the Anthropic path before
        # this existed. What was never exercised inside a frozen bundle is
        # the Gemini path (no smoke check ever selects it), and construction
        # ahead of Config validation, so a broken provider import surfaces
        # here without first requiring a full Discord/LLM/channel
        # configuration. This proves the base anthropic/google-genai packages
        # import and construct a client inside the frozen bundle; it does
        # NOT prove every rangecontrol.spec collect_all hidden-import or
        # data-file entry is load-bearing -- a submodule reachable only
        # through an actual request/response cycle would not be caught here
        # (confirmed experimentally: removing "anthropic" from collect_all
        # and rebuilding still passed this exact check, because RangeControl's
        # own `import anthropic` is already found by PyInstaller's ordinary
        # static analysis regardless of collect_all). Constructing a client
        # makes no network call -- it is a dummy key and a throwaway Config,
        # one per known provider, so this costs nothing and needs no real
        # credentials.
        if not VALID_PROVIDERS:
            print(
                "Error: no providers registered in VALID_PROVIDERS.",
                file=sys.stderr,
            )
            return 1
        for provider_name in VALID_PROVIDERS:
            dummy_config = Config(
                discord_bot_token="verify-bundle",
                llm_provider=provider_name,
                llm_api_key="verify-bundle-dummy-key",
                llm_model=DEFAULT_MODELS[provider_name],
                llm_gate_model=DEFAULT_MODELS[provider_name],
                white_cell_channel_id=0,
                allowed_channel_ids=(),
                range_dir=Path("."),
                human_in_the_loop=False,
            extra_instructions="",
            )
            try:
                build_provider(dummy_config)
            except Exception as exc:  # noqa: BLE001 - any import/construction failure counts
                print(
                    f"Error: {provider_name} provider failed to construct: {exc}",
                    file=sys.stderr,
                )
                return 1
            print(f"OK: {provider_name} provider constructed.")
        return 0

    # Load .env if present; real environment variables always win.
    load_dotenv(override=False)

    try:
        config = load_config()
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 1

    if getattr(args, "command", None) in {"cache-status", "cache-put"}:
        try:
            return _run_cache_command(args, config)
        except (OSError, ValueError) as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1

    # _dry_run_would_generate reads Range.md itself (see its docstring), on
    # this same main thread, ahead of prepare()'s own OSError handling --
    # an unreadable Range.md (a locked file, a permissions mistake) must be
    # reported the same clean way every other startup OSError already is
    # below, not raise a traceback one module over from where C1 fixed the
    # identical class of bug for the GUI.
    try:
        dry_run_refused = args.dry_run and _dry_run_would_generate(args, config)
    except OSError as exc:
        print(f"Startup failed: {exc}", file=sys.stderr)
        return 1

    if dry_run_refused:
        print(
            "No Range.md exists yet, so --dry-run has nothing to report on: "
            "building one calls the configured LLM provider for every "
            "resource, which --dry-run must never do.\n"
            "Run `rangecontrol` once without --dry-run to generate Range.md, "
            "then run --dry-run again to see the ingest report.",
            file=sys.stderr,
        )
        return 1

    try:
        provider = build_provider(config)
        result = prepare(
            config, provider, regenerate=_decide_regenerate(args, config)
        )
    except (LLMError, RuntimeError, ValueError, OSError) as exc:
        # LLMError is what both adapters raise for an outage, a bad key, or a
        # malformed response — the most likely startup failure of all. Without
        # it here, a provider problem prints a stack trace while every other
        # failure gets a clean message.
        print(f"Startup failed: {exc}", file=sys.stderr)
        return 1

    if args.dry_run:
        from rangecontrol.range_doc.report import format_report

        print(format_report(result.report))
        return 0

    if not confirm(result.report, auto_yes=args.yes):
        print("Aborted.")
        return 2

    _, _, state_dir = _paths(config)
    advisor = Advisor(
        provider=provider,
        gate_provider=build_gate_provider(config),
        range_md=result.range_md,
        corpus_text=result.corpus.to_prompt_text(),
        extra_instructions=config.extra_instructions,
    )
    try:
        run_bot(config, advisor, AuditLog(state_dir))
    except (RuntimeError, OSError) as exc:
        # Connecting is a startup step like any other. A rejected token or a
        # missing intent must read the same way a missing config value does.
        print(f"Startup failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
