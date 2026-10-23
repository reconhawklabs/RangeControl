import asyncio
import queue
import threading
import time

import pytest

from rangecontrol.bot.mode import ModeSwitch
from rangecontrol.gui.events import STATUS
from rangecontrol.gui.runtime import FAILED, RUNNING, STOPPED, STOPPING, BotController


class FakeBot:
    """Stands in for RangeControlBot: start() blocks until close() is called.

    Also stands in for discord.Client's wait_until_ready(): _ready is set
    once start() has done whatever it needs to do to become "ready", the
    same way discord.py's is set once on_ready fires. A bot constructed with
    fail_with raises before ever becoming ready, modeling a token rejected
    during login -- the case RUNNING must never be published for.
    """

    def __init__(self, *, fail_with: Exception | None = None) -> None:
        self._fail_with = fail_with
        self._closed = asyncio.Event()
        self._ready = asyncio.Event()
        self.started = False
        self.closed = False
        self.mode = ModeSwitch(False)

    async def start(self, token):
        self.started = True
        if self._fail_with is not None:
            raise self._fail_with
        self._ready.set()
        await self._closed.wait()

    async def close(self):
        self.closed = True
        self._closed.set()

    async def wait_until_ready(self):
        await self._ready.wait()


class DiesAfterReadyBot(FakeBot):
    """Becomes ready normally, then dies with a BaseException that is not an
    Exception -- the shape asyncio.CancelledError has on this Python floor.

    Exercises the finally-block safety net: RUNNING must not be the settled
    state once this bot's thread has exited, even though it truly did
    become ready before dying.
    """

    async def start(self, token):
        self.started = True
        self._ready.set()
        await asyncio.sleep(0)  # yield once so wait_until_ready() can win
        raise asyncio.CancelledError("simulated cancellation")


class UnresponsiveBot(FakeBot):
    """A bot whose close() never unblocks start().

    Exercises BotController.stop()'s join-timeout path: close() returns
    (it is not itself slow), but it has no effect, so the bot thread never
    exits within the deadline.
    """

    async def close(self):
        return


class LingeringTaskBot(FakeBot):
    """Spawns a background task that resists cancellation for a while,
    mirroring a real client's connector teardown.

    Exercises the window between _run_bot returning and BotController._run's
    handles being cleared: _shutdown_loop's own run_until_complete() calls
    make loop.is_running() read True again for their duration, so a stop()
    landing in that window must not be able to schedule bot.close() onto a
    loop that is mid-teardown. `cancelled` is a threading.Event, safe to
    read/wait-on cross-thread, set at the exact moment the lingering task
    observes its own cancellation -- i.e. the moment the drain actually
    starts -- so the test does not have to guess a sleep duration.
    """

    def __init__(self, *, linger: float = 0.3) -> None:
        super().__init__()
        self._linger = linger
        self.cancelled = threading.Event()

    async def start(self, token):
        self.started = True
        self._ready.set()

        async def _stubborn():
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                self.cancelled.set()
                # Resist for a while instead of unwinding immediately, the
                # way a real aiohttp connector teardown can.
                await asyncio.sleep(self._linger)

        asyncio.create_task(_stubborn())
        await self._closed.wait()


class BadReadinessBot(FakeBot):
    """wait_until_ready() raises instead of resolving normally.

    Real discord.Client.wait_until_ready() just awaits an Event and does not
    do this, but the controller must not let a misbehaving readiness check
    (a) publish RUNNING on the strength of a task that failed, or (b) leak
    out of _run_bot's finally and turn an otherwise-clean stop() into a
    spurious FAILED.
    """

    async def wait_until_ready(self):
        raise RuntimeError("readiness blew up")


def drain(events):
    out = []
    while True:
        try:
            out.append(events.get_nowait())
        except queue.Empty:
            return out


def wait_for(controller, state, timeout=5.0):
    deadline = threading.Event()
    for _ in range(int(timeout / 0.02)):
        if controller.state.state == state:
            return True
        deadline.wait(0.02)
    return False


START = {"config": object(), "advisor": object(), "audit": object()}


def states_in(events):
    return [e.payload.state for e in drain(events) if e.kind == STATUS]


def test_start_runs_until_stop_and_publishes_every_transition():
    events = queue.Queue()
    bot = FakeBot()
    controller = BotController(events, bot_factory=lambda **kw: bot)

    # Stopping a controller that never started publishes nothing.
    controller.stop()
    controller.stop()
    assert controller.state.state == STOPPED
    assert drain(events) == []

    controller.start(**START)
    try:
        assert wait_for(controller, RUNNING)
        assert bot.started is True
        with pytest.raises(RuntimeError, match="already running"):
            controller.start(**START)
    finally:
        controller.stop()

    assert controller.state.state == STOPPED
    states = states_in(events)
    assert states[0] == "starting"
    assert RUNNING in states
    assert states[-1] == STOPPED


def test_a_failed_start_reports_why_never_reads_running_and_releases_the_bot():
    """A green light over a dead bot is the worst available outcome. The
    failed bot must still be closed: a real client holds an aiohttp session,
    and fix-the-token-and-retry would otherwise leak one per attempt."""
    events = queue.Queue()
    bot = FakeBot(fail_with=RuntimeError("Improper token"))
    controller = BotController(events, bot_factory=lambda **kw: bot)
    controller.start(**START)
    try:
        assert wait_for(controller, FAILED)
        assert "Improper token" in controller.state.detail
        assert bot.closed is True
        states = states_in(events)
        assert FAILED in states
        assert RUNNING not in states
    finally:
        controller.stop()

    # A factory that cannot even build the bot settles the same way, and a
    # later stop() must not paper over the failure with STOPPED.
    def bad_factory(**kwargs):
        raise RuntimeError("boom: bad factory")

    controller = BotController(queue.Queue(), bot_factory=bad_factory)
    controller.start(**START)
    assert wait_for(controller, FAILED)
    assert "boom: bad factory" in controller.state.detail
    controller.stop()
    assert controller.state.state == FAILED


def test_a_bot_that_breaks_after_starting_never_leaves_running_standing():
    """CancelledError is a BaseException, not an Exception: a narrower except
    clause let the thread die while the settled state still read RUNNING."""
    controller = BotController(queue.Queue(), bot_factory=lambda **kw: DiesAfterReadyBot())
    controller.start(**START)
    try:
        assert wait_for(controller, FAILED)
        thread = controller._thread
        assert thread is not None
        thread.join(timeout=2.0)
        assert not thread.is_alive()
        assert controller.state.state == FAILED
    finally:
        controller.stop()

    # A readiness check that raises must neither publish RUNNING nor turn a
    # clean stop() into a spurious FAILED.
    events = queue.Queue()
    controller = BotController(events, bot_factory=lambda **kw: BadReadinessBot())
    controller.start(**START)
    try:
        time.sleep(0.05)  # the readiness check fails near-instantly
        assert controller.state.state != RUNNING
        controller.stop()
        assert controller.state.state == STOPPED
        states = states_in(events)
        assert RUNNING not in states
        assert states[-1] == STOPPED
    finally:
        controller.stop()


def test_stop_timeout_leaves_thread_marked_and_refuses_restart():
    events = queue.Queue()
    bot = UnresponsiveBot()
    controller = BotController(events, bot_factory=lambda **kw: bot)
    controller.start(**START)
    try:
        assert wait_for(controller, RUNNING)
        controller.stop(timeout=0.1)
        assert controller.state.state == STOPPING
        with pytest.raises(RuntimeError, match="already running"):
            controller.start(**START)
    finally:
        # UnresponsiveBot.close() deliberately does not unblock start(), so
        # schedule the close on the bot's own loop to leave no thread behind.
        loop = controller._loop
        if loop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(bot._closed.set)
        controller.stop(timeout=5.0)
        assert wait_for(controller, STOPPED)


def test_handles_are_cleared_before_the_drain_not_after():
    """Regression: a stop() landing during _shutdown_loop's slow drain must
    see no live loop, not schedule bot.close() onto one mid-teardown and
    recreate the freeze the is_running()/is_closed() guard eliminated."""
    events = queue.Queue()
    bot = LingeringTaskBot(linger=0.3)
    controller = BotController(events, bot_factory=lambda **kw: bot)
    controller.start(**START)
    try:
        assert wait_for(controller, RUNNING)

        stop_thread = threading.Thread(
            target=controller.stop, kwargs={"timeout": 5.0}
        )
        stop_thread.start()

        # Wait for the drain to actually begin rather than guessing a sleep.
        assert bot.cancelled.wait(timeout=2.0)
        assert controller._loop is None
        assert controller._bot is None

        start = time.monotonic()
        controller.stop(timeout=5.0)
        elapsed = time.monotonic() - start
        assert elapsed < 2.0, elapsed

        stop_thread.join(10.0)
        assert not stop_thread.is_alive()
    finally:
        controller.stop(timeout=5.0)
    assert wait_for(controller, STOPPED)


def test_a_running_bot_takes_live_settings_and_can_publish_back_to_the_gui():
    """HITL and the denial wording change mid-exercise without a restart;
    the bot reaches the GUI queue only through the two callbacks it is given."""
    from rangecontrol.gui.events import ERROR, PENDING

    events = queue.Queue()
    bot = FakeBot()
    captured = {}

    def factory(**kwargs):
        captured.update(kwargs)
        return bot

    controller = BotController(events, bot_factory=factory)
    # No bot yet: both live setters are harmless no-ops.
    controller.set_human_in_the_loop(True)
    controller.set_denied_text("Nope.")
    assert controller.state.state == STOPPED

    controller.start(**START)
    try:
        assert wait_for(controller, RUNNING)
        controller.set_human_in_the_loop(True)
        assert bot.mode.enabled is True
        controller.set_human_in_the_loop(False)
        assert bot.mode.enabled is False
        controller.set_denied_text("Nope.")
        assert bot.denied_text == "Nope."
    finally:
        controller.stop()

    controller.set_human_in_the_loop(True)  # after stop: no bot to reach
    assert bot.mode.enabled is False

    captured["on_pending_change"](2)
    captured["on_error"]("white cell unreachable")
    kinds = [(e.kind, e.payload) for e in drain(events)]
    assert (PENDING, 2) in kinds
    assert (ERROR, "white cell unreachable") in kinds
