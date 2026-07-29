"""Background workers: range ingestion and the Discord bot lifecycle.

Everything here runs off the Tk main thread and reports exclusively through
the event queue. No function in this module touches a widget.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
import time
from dataclasses import dataclass

from rangecontrol.config import Config
from rangecontrol.gui.events import ERROR, INGEST, MODELS, PENDING, STATUS, publish
from rangecontrol.llm.base import LLMError
from rangecontrol.llm.factory import build_provider
from rangecontrol.main import prepare
from rangecontrol.range_doc.report import IngestReport

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class IngestOutcome:
    ok: bool
    report: IngestReport | None = None
    range_md: str = ""
    corpus_text: str = ""
    error: str = ""
    # Which request produced this outcome. The GUI can have two IngestJobs
    # in flight at once -- the startup load racing a Generate click -- and
    # both publish onto the same queue. App._handle_ingest compares this
    # against its own request counter and discards anything not from the
    # most recently issued request, so a slow, superseded job can never
    # clobber a fresher result just by finishing later. Defaults to 0 so
    # every other caller (the CLI never constructs this type at all, and
    # existing tests that build one by hand) is unaffected.
    generation: int = 0


class IngestJob:
    """One ingest or regenerate, run on a worker thread.

    Never raises out of run(): an exception on a worker thread kills the
    thread silently and leaves the window showing a spinner forever. Every
    failure becomes an IngestOutcome with ok=False and a readable message.
    """

    def __init__(
        self,
        config: Config,
        events,
        *,
        regenerate: bool,
        provider_factory=build_provider,
        generation: int = 0,
    ) -> None:
        self._config = config
        self._events = events
        self._regenerate = regenerate
        self._provider_factory = provider_factory
        self._generation = generation

    def run(self) -> None:
        # This method's never-raises guarantee also depends on publish() never
        # raising an ordinary Exception, which it already ensures internally
        # (rangecontrol/gui/events.py). What is caught here is everything
        # `prepare()` and the provider factory might throw.
        try:
            provider = self._provider_factory(self._config)
            result = prepare(self._config, provider, regenerate=self._regenerate)
        except (LLMError, RuntimeError, ValueError, OSError) as exc:
            logger.warning("ingest failed: %s", exc)
            publish(
                self._events,
                INGEST,
                IngestOutcome(ok=False, error=str(exc), generation=self._generation),
            )
            return
        except Exception as exc:  # noqa: BLE001 - a worker must not die silently
            logger.exception("unexpected ingest failure")
            publish(
                self._events,
                INGEST,
                IngestOutcome(
                    ok=False,
                    error=f"{type(exc).__name__}: {exc}",
                    generation=self._generation,
                ),
            )
            return
        except BaseException as exc:
            # SystemExit/GeneratorExit (KeyboardInterrupt does not reach a
            # non-main thread under normal SIGINT delivery) are deliberate
            # shutdown signals, not ingest failures: swallowing them the way
            # the clause above does would hide a real interpreter shutdown
            # from whatever triggered it. Tell the GUI what happened, then
            # re-raise so the signal keeps unwinding. Do NOT merge this into
            # the `except Exception` clause above — that would silently
            # swallow the signal instead of letting it propagate.
            publish(
                self._events,
                INGEST,
                IngestOutcome(
                    ok=False,
                    error=f"{type(exc).__name__}: {exc}",
                    generation=self._generation,
                ),
            )
            raise

        publish(
            self._events,
            INGEST,
            IngestOutcome(
                ok=True,
                report=result.report,
                range_md=result.range_md,
                corpus_text=result.corpus.to_prompt_text(),
                generation=self._generation,
            ),
        )

    def start(self) -> threading.Thread:
        thread = threading.Thread(target=self.run, name="rc-ingest", daemon=True)
        thread.start()
        return thread


STOPPED = "stopped"
STARTING = "starting"
RUNNING = "running"
STOPPING = "stopping"
FAILED = "failed"


@dataclass(frozen=True)
class BotState:
    state: str
    detail: str = ""


def _default_bot_factory(**kwargs):
    from rangecontrol.bot.client import RangeControlBot

    return RangeControlBot(**kwargs)


def _shutdown_loop(loop: asyncio.AbstractEventLoop) -> None:
    """Drain a loop before closing it.

    Mirrors what asyncio.run() does on the way out: cancel whatever tasks
    are still pending, gather them so their cancellations are actually
    observed, close async generators, and only then close the executor and
    the loop itself. FakeBot spawns no extra tasks, so no test would notice
    if this were skipped -- but a real discord.py client leaves heartbeat
    and keepalive tasks and aiohttp connectors running, which without this
    surface as "Task was destroyed but it is pending" warnings and sockets
    left open after a failed start. This function must never raise: it is
    called from the last `finally` standing between a dead bot thread and a
    controller that can never be restarted.
    """
    try:
        pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
        for task in pending:
            task.cancel()
        if pending:
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        loop.run_until_complete(loop.shutdown_asyncgens())
        loop.run_until_complete(loop.shutdown_default_executor())
    except Exception:  # noqa: BLE001 - draining must not prevent closing
        logger.warning("error while draining the event loop", exc_info=True)
    finally:
        try:
            loop.close()
        except Exception:  # noqa: BLE001 - this is the last chance to close it
            logger.warning("error while closing the event loop", exc_info=True)


@dataclass(frozen=True)
class ModelsOutcome:
    ok: bool
    models: tuple[str, ...] = ()
    error: str = ""


class ModelsJob:
    """Ask the provider which models this key can reach, off the Tk thread.

    A metadata call, not a generation, but still network - so it cannot run on
    the main thread. Never raises: a failure becomes an outcome the form can
    show, because a dropdown that silently does not repopulate tells the
    operator nothing about why.
    """

    def __init__(self, config, events, *, lister=None) -> None:
        self._config = config
        self._events = events
        if lister is None:
            from rangecontrol.llm.catalog import list_models

            lister = list_models
        self._lister = lister

    def run(self) -> None:
        try:
            models = self._lister(self._config)
        except Exception as exc:  # noqa: BLE001 - a worker must not die silently
            publish(self._events, MODELS,
                    ModelsOutcome(ok=False, error=f"{type(exc).__name__}: {exc}"))
            return
        publish(self._events, MODELS, ModelsOutcome(ok=True, models=tuple(models)))

    def start(self) -> threading.Thread:
        thread = threading.Thread(target=self.run, name="rc-models", daemon=True)
        thread.start()
        return thread


class BotController:
    """Owns the Discord bot's thread and its asyncio loop.

    discord.py's Client.run() installs signal handlers and therefore only
    works on the main thread, which Tk owns. The loop is built and driven
    here instead, and shutdown is scheduled back onto it thread-safely.

    `_thread`, `_loop`, `_bot`, and `_state` are all written from the bot
    thread and read from whichever thread calls `stop()` or `state`, so all
    four live behind `_lock`. `stop()` takes one snapshot of the three
    handles under the lock before acting on any of them, rather than reading
    them one at a time -- reading `_loop` and `_bot` separately would let a
    concurrent `_run()` be seen mid-construction, with `_loop` set and `_bot`
    still `None`, silently skipping shutdown of a bot that is about to exist.
    """

    def __init__(self, events, *, bot_factory=_default_bot_factory) -> None:
        self._events = events
        self._bot_factory = bot_factory
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._bot = None
        self._state = BotState(STOPPED)

    @property
    def state(self) -> BotState:
        with self._lock:
            return self._state

    def set_human_in_the_loop(self, enabled: bool) -> None:
        """Flip the live bot's ModeSwitch without a restart.

        Read under the same lock start()/_run()/stop() use for `_bot`, so
        this cannot observe a stale reference while a bot is mid-
        construction. A harmless no-op when no bot exists yet -- stopped,
        or between start() being called and _run() actually constructing
        one -- or after one has already been torn down. ModeSwitch.set()
        is itself safe to call from any thread, so nothing further is
        needed to make this callable straight off the Tk main thread.
        """
        with self._lock:
            bot = self._bot
        if bot is not None:
            bot.mode.set(enabled)

    def _set_state(self, state: str, detail: str = "") -> None:
        new_state = BotState(state, detail)
        with self._lock:
            if self._state == new_state:
                return
            self._state = new_state
        # Publish only on an actual transition: a caller polling or calling
        # stop() repeatedly on a controller that never changed state should
        # not flood the event queue with duplicate STATUS events.
        publish(self._events, STATUS, new_state)

    def start(self, *, config, advisor, audit) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                raise RuntimeError("the bot is already running")
            thread = threading.Thread(
                target=self._run,
                args=(config, advisor, audit),
                name="rc-discord",
                daemon=True,
            )
            self._thread = thread
        self._set_state(STARTING)
        thread.start()

    def _run(self, config, advisor, audit) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        with self._lock:
            self._loop = loop
        try:
            bot = self._bot_factory(
                config=config, advisor=advisor, audit=audit,
                on_pending_change=lambda count: publish(self._events, PENDING, count),
                on_error=lambda text: publish(self._events, ERROR, text),
            )
            with self._lock:
                self._bot = bot
            token = getattr(config, "discord_bot_token", "")
            loop.run_until_complete(self._run_bot(token))
        except BaseException as exc:
            # BaseException, not Exception: asyncio.CancelledError is a
            # BaseException on this Python floor, and a narrower catch here
            # let a cancelled bot's thread die with the settled state still
            # reading RUNNING -- a green light over a bot that no longer
            # exists, which is the one outcome this whole class exists to
            # prevent.
            logger.warning("discord bot stopped: %s", exc)
            self._set_state(FAILED, _describe(exc))
        else:
            self._set_state(STOPPED)
        finally:
            # Second, independent safety net: whatever exit path was taken
            # above, this thread is one statement away from being dead, and
            # no exit path may leave STARTING/RUNNING standing once that
            # happens.
            if self.state.state in (STARTING, RUNNING):
                self._set_state(
                    FAILED, "the bot thread exited without settling its status"
                )
            # Clear the handles before draining, not after: _shutdown_loop
            # below calls run_until_complete() itself, which makes
            # loop.is_running() read True again from another thread for the
            # duration of the drain. Once _run_bot has returned there is
            # nothing left for stop() to close, so a stop() landing during
            # the drain must see None here and go straight to joining --
            # not pass the is_running() check and schedule bot.close() onto
            # a loop that is mid-teardown, which recreates the exact freeze
            # stop()'s own guard exists to prevent.
            with self._lock:
                self._loop = None
                self._bot = None
            _shutdown_loop(loop)
            asyncio.set_event_loop(None)

    async def _run_bot(self, token: str) -> None:
        # RUNNING is keyed off readiness, not scheduling. discord.py's
        # start() is login() then connect(): a rejected token is rejected
        # only after the HTTP round trip completes. Reporting RUNNING as
        # soon as the coroutine was merely scheduled left the *settled*
        # state -- the one a status bar polls -- reading RUNNING for the
        # entire login round trip, which is exactly the "green light over a
        # dead bot" scenario this controller exists to avoid.
        start_task = asyncio.create_task(self._bot.start(token))
        ready_task = asyncio.create_task(self._bot.wait_until_ready())
        try:
            done, _ = await asyncio.wait(
                {start_task, ready_task}, return_when=asyncio.FIRST_COMPLETED
            )
            if start_task in done:
                await start_task  # re-raise: it died before ever being ready
                return
            # ready_task is the one that completed. Only report RUNNING if
            # it actually succeeded: a wait_until_ready() that raised or was
            # cancelled proves nothing about whether the bot is ready, and
            # publishing RUNNING on the strength of a task that failed is
            # the same "green light over a dead bot" mistake this method
            # exists to avoid.
            if not ready_task.cancelled() and ready_task.exception() is None:
                self._set_state(RUNNING)
            await start_task  # runs until close(), or raises if start() fails
        finally:
            ready_task.cancel()
            # Any outcome of ready_task -- including an exception unrelated
            # to the cancel() above -- must not escape this finally and
            # replace whatever the try block was doing (a clean return, or a
            # real failure from start_task). Its outcome was already
            # consulted above when it mattered; suppressing everything here
            # only marks it retrieved so asyncio does not separately log it
            # as "exception was never retrieved".
            with contextlib.suppress(BaseException):
                await ready_task
            # start_task can reach this point already done-with-exception
            # without that exception ever having been retrieved: if its
            # coroutine raised SystemExit/KeyboardInterrupt, asyncio's Task
            # machinery sets the exception and re-raises past this
            # coroutine's own await points instead of resuming it normally,
            # so the `except`/`return` paths above never ran for it. Left
            # alone, asyncio logs that as "Task exception was never
            # retrieved" once the Task is garbage-collected. Retrieving it
            # here is a no-op if it was already consumed above.
            with contextlib.suppress(BaseException):
                start_task.exception()
            # If nothing ever called close() on this bot -- a bot that died
            # on its own (a rejected token, a crash) never gets one from
            # stop(), since stop() only runs for a controller a caller is
            # actively shutting down -- close it here. A real discord.py
            # client holds an aiohttp session and connector that only
            # close() releases, and the GUI's own recovery path (fix the
            # token, click Start again) guarantees repeated failed attempts
            # on fresh bot instances, each leaking a session without this.
            # close() is idempotent on a real client and on FakeBot, so
            # calling it again after an explicit stop() is harmless.
            with contextlib.suppress(BaseException):
                await self._bot.close()

    def stop(self, timeout: float = 10.0) -> None:
        # One deadline covers both waits below, not `timeout` seconds each:
        # closing the bot (future.result) and joining its thread were
        # previously budgeted independently, so a bot that never closes
        # could hold WM_DELETE_WINDOW -- and the Tk thread with it -- for up
        # to 2x `timeout` instead of the one the caller asked for.
        deadline = time.monotonic() + timeout
        with self._lock:
            thread = self._thread
            loop = self._loop
            bot = self._bot

        if loop is not None and bot is not None:
            self._request_close(loop, bot, max(0.0, deadline - time.monotonic()))

        if thread is None:
            return

        thread.join(max(0.0, deadline - time.monotonic()))
        if thread.is_alive():
            # The thread did not exit within the deadline: it is neither
            # stopped nor safe to treat as such. Reporting STOPPED here
            # would let a caller believe it is safe to start a second bot
            # alongside a live one (see start()'s is_alive() guard), so
            # _thread stays set and the state says exactly what is true.
            self._set_state(STOPPING, "the bot did not exit within the timeout")
            return

        with self._lock:
            if self._thread is thread:
                self._thread = None

    def _request_close(
        self, loop: asyncio.AbstractEventLoop, bot, timeout: float
    ) -> None:
        # The guard and the scheduling must happen under the same lock
        # acquisition, not just the guard: _run's finally clears self._loop
        # under this same lock right before it starts draining the loop
        # (see _run's comment). If the guard were evaluated and then the
        # lock released before run_coroutine_threadsafe, a stop() thread
        # descheduled in that gap could still see this loop as live, get
        # rescheduled after _run has already moved on to _shutdown_loop, and
        # schedule bot.close() onto a loop that is mid-teardown -- the exact
        # freeze this guard exists to prevent, just narrowed instead of
        # closed. `self._loop is not loop` is the identity check that makes
        # this atomic with respect to _run's clear: it is what tells us
        # whether the loop we were handed a reference to is still the one
        # _run considers live, not merely whether that loop object happens
        # to still be running or open. Do not split this back into a
        # "check, then act" pair.
        with self._lock:
            if self._loop is not loop or loop.is_closed() or not loop.is_running():
                return
            coro = bot.close()
            try:
                future = asyncio.run_coroutine_threadsafe(coro, loop)
            except RuntimeError:
                # The loop stopped between the checks above and this call.
                coro.close()
                return
        # call_soon_threadsafe (inside run_coroutine_threadsafe) never
        # blocks and never touches this lock, so releasing it before the
        # actual wait below cannot deadlock against _run, which may itself
        # be waiting to acquire this same lock.
        try:
            future.result(timeout)
        except Exception:  # noqa: BLE001 - shutdown must not raise at the caller
            logger.warning("bot did not close cleanly", exc_info=True)


def _describe(exc: BaseException) -> str:
    """Turn a Discord exception into something an operator can act on.

    Matched by class name rather than `isinstance`, deliberately: importing
    discord here to check types would defeat the point of the lazy import in
    _default_bot_factory, which exists so CLI startup never pays for
    discord.py when the GUI never touches the bot.
    """
    name = type(exc).__name__
    if name == "LoginFailure":
        return (
            "Discord rejected the bot token. Use Developer Portal -> your "
            "application -> Bot -> Reset Token. The Application ID, Public "
            "Key, and Client Secret will not work."
        )
    if name == "PrivilegedIntentsRequired":
        return (
            "Discord refused the Message Content intent. Enable it under "
            "Developer Portal -> your application -> Bot -> Privileged "
            "Gateway Intents, then start again."
        )
    return f"{name}: {exc}"
