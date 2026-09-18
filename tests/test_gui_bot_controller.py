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


def test_starts_and_reports_running():
    events = queue.Queue()
    bot = FakeBot()
    controller = BotController(events, bot_factory=lambda **kw: bot)
    controller.start(config=object(), advisor=object(), audit=object())
    try:
        assert wait_for(controller, RUNNING)
        assert bot.started is True
    finally:
        controller.stop()


def test_stop_returns_to_stopped():
    events = queue.Queue()
    controller = BotController(events, bot_factory=lambda **kw: FakeBot())
    controller.start(config=object(), advisor=object(), audit=object())
    assert wait_for(controller, RUNNING)
    controller.stop()
    assert controller.state.state == STOPPED


def test_a_login_failure_surfaces_with_its_reason():
    """A green light over a dead bot is the worst available outcome."""
    events = queue.Queue()
    controller = BotController(
        events,
        bot_factory=lambda **kw: FakeBot(fail_with=RuntimeError("Improper token")),
    )
    controller.start(config=object(), advisor=object(), audit=object())
    assert wait_for(controller, FAILED)
    assert "Improper token" in controller.state.detail
    kinds = [e.payload.state for e in drain(events) if e.kind == STATUS]
    assert FAILED in kinds


def test_state_changes_are_published():
    events = queue.Queue()
    controller = BotController(events, bot_factory=lambda **kw: FakeBot())
    controller.start(config=object(), advisor=object(), audit=object())
    assert wait_for(controller, RUNNING)
    controller.stop()
    states = [e.payload.state for e in drain(events) if e.kind == STATUS]
    assert states[0] == "starting"
    assert RUNNING in states
    assert states[-1] == STOPPED


def test_starting_twice_is_rejected():
    events = queue.Queue()
    controller = BotController(events, bot_factory=lambda **kw: FakeBot())
    controller.start(config=object(), advisor=object(), audit=object())
    assert wait_for(controller, RUNNING)
    try:
        with pytest.raises(RuntimeError, match="already running"):
            controller.start(config=object(), advisor=object(), audit=object())
    finally:
        controller.stop()


def test_stop_without_start_is_harmless():
    controller = BotController(queue.Queue(), bot_factory=lambda **kw: FakeBot())
    controller.stop()
    assert controller.state.state == STOPPED


def test_double_stop_without_start_publishes_nothing():
    """stop() must only publish on an actual transition, not on every call."""
    events = queue.Queue()
    controller = BotController(events, bot_factory=lambda **kw: FakeBot())
    controller.stop()
    controller.stop()
    assert controller.state.state == STOPPED
    assert drain(events) == []


def test_running_is_published_only_after_readiness():
    events = queue.Queue()
    bot = FakeBot()
    controller = BotController(events, bot_factory=lambda **kw: bot)
    controller.start(config=object(), advisor=object(), audit=object())
    try:
        assert wait_for(controller, RUNNING)
        states = [e.payload.state for e in drain(events) if e.kind == STATUS]
        assert RUNNING in states
    finally:
        controller.stop()


def test_running_is_never_published_when_start_fails_before_ready():
    events = queue.Queue()
    controller = BotController(
        events,
        bot_factory=lambda **kw: FakeBot(fail_with=RuntimeError("Improper token")),
    )
    controller.start(config=object(), advisor=object(), audit=object())
    try:
        assert wait_for(controller, FAILED)
        states = [e.payload.state for e in drain(events) if e.kind == STATUS]
        assert RUNNING not in states
    finally:
        controller.stop()


def test_factory_failure_settles_to_failed_and_stop_does_not_overwrite_it():
    events = queue.Queue()

    def bad_factory(**kwargs):
        raise RuntimeError("boom: bad factory")

    controller = BotController(events, bot_factory=bad_factory)
    controller.start(config=object(), advisor=object(), audit=object())
    assert wait_for(controller, FAILED)
    assert "boom: bad factory" in controller.state.detail
    controller.stop()
    assert controller.state.state == FAILED


def test_a_baseexception_after_running_settles_to_failed_not_running():
    """CancelledError is a BaseException, not an Exception, on this Python
    floor. A narrower except clause let the thread die while the settled
    state still read RUNNING -- the worst available outcome, and
    indistinguishable from a healthy bot to anything polling state.
    """
    events = queue.Queue()
    controller = BotController(events, bot_factory=lambda **kw: DiesAfterReadyBot())
    controller.start(config=object(), advisor=object(), audit=object())
    try:
        assert wait_for(controller, FAILED)
        # Prove the thread is actually dead, not merely that state briefly
        # touched FAILED before bouncing back.
        thread = controller._thread
        assert thread is not None
        thread.join(timeout=2.0)
        assert not thread.is_alive()
        assert controller.state.state == FAILED
        assert controller.state.state != RUNNING
    finally:
        controller.stop()


def test_stop_timeout_leaves_thread_marked_and_refuses_restart():
    events = queue.Queue()
    bot = UnresponsiveBot()
    controller = BotController(events, bot_factory=lambda **kw: bot)
    controller.start(config=object(), advisor=object(), audit=object())
    try:
        assert wait_for(controller, RUNNING)
        controller.stop(timeout=0.1)
        assert controller.state.state == STOPPING
        with pytest.raises(RuntimeError, match="already running"):
            controller.start(config=object(), advisor=object(), audit=object())
    finally:
        # Force the real shutdown so the test leaves no thread or loop
        # running: schedule the close directly on the bot's own loop rather
        # than through BotController.stop(), since UnresponsiveBot.close()
        # deliberately does not unblock start() on its own.
        loop = controller._loop
        if loop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(bot._closed.set)
        controller.stop(timeout=5.0)
        assert wait_for(controller, STOPPED)


def test_handles_are_cleared_before_the_drain_not_after():
    """Regression for the window _shutdown_loop's own run_until_complete
    reopens: _run must null self._loop/self._bot before it starts draining
    the loop, not after -- otherwise a stop() landing during a slow drain
    can see a "live" loop and schedule bot.close() onto it while it is
    mid-teardown, recreating the exact freeze BotController.stop()'s
    is_running()/is_closed() guard was added to eliminate.
    """
    events = queue.Queue()
    bot = LingeringTaskBot(linger=0.3)
    controller = BotController(events, bot_factory=lambda **kw: bot)
    controller.start(config=object(), advisor=object(), audit=object())
    try:
        assert wait_for(controller, RUNNING)

        stop_thread = threading.Thread(
            target=controller.stop, kwargs={"timeout": 5.0}
        )
        stop_thread.start()

        # Wait for the drain to actually begin (the lingering task observing
        # its own cancellation) rather than guessing a sleep duration. At
        # this instant the drain is in progress and will not return for
        # another ~0.3s (LingeringTaskBot's linger).
        assert bot.cancelled.wait(timeout=2.0)

        # The handles must already be gone at this exact moment, not only
        # after the (slow) drain finishes.
        assert controller._loop is None
        assert controller._bot is None

        # A stop() landing right now must see nothing to close and go
        # straight to joining, returning long before the drain's own delay
        # elapses -- not burn the full timeout scheduling bot.close() onto a
        # loop that is mid-teardown.
        start = time.monotonic()
        controller.stop(timeout=5.0)
        elapsed = time.monotonic() - start
        assert elapsed < 2.0, elapsed

        stop_thread.join(10.0)
        assert not stop_thread.is_alive()
    finally:
        controller.stop(timeout=5.0)
    assert wait_for(controller, STOPPED)


def test_readiness_failure_does_not_publish_running_or_corrupt_a_clean_stop():
    events = queue.Queue()
    controller = BotController(events, bot_factory=lambda **kw: BadReadinessBot())
    controller.start(config=object(), advisor=object(), audit=object())
    try:
        # Give the background thread a moment to run past the readiness
        # check (which fails near-instantly); state must never become
        # RUNNING off the back of it.
        time.sleep(0.05)
        assert controller.state.state != RUNNING
        controller.stop()
        assert controller.state.state == STOPPED
        states = [e.payload.state for e in drain(events) if e.kind == STATUS]
        assert RUNNING not in states
        assert states[-1] == STOPPED
    finally:
        controller.stop()


def test_a_failed_start_still_closes_the_bot():
    """Nothing calls bot.close() when the bot dies on its own: stop() only
    ever runs for a controller a caller is actively shutting down, and a
    bot that fails during start() (a rejected token, say) is never handed
    to stop() at all. A real discord.py client holds an aiohttp session and
    connector that only close() releases, and the GUI's own recovery path --
    fix the token, click Start again -- guarantees every failed attempt
    leaks one without this.
    """
    events = queue.Queue()
    bot = FakeBot(fail_with=RuntimeError("Improper token"))
    controller = BotController(events, bot_factory=lambda **kw: bot)
    controller.start(config=object(), advisor=object(), audit=object())
    try:
        assert wait_for(controller, FAILED)
        assert bot.closed is True
    finally:
        controller.stop()


# -- set_human_in_the_loop: the live toggle -----------------------------------


def test_set_human_in_the_loop_updates_the_live_bots_mode():
    events = queue.Queue()
    bot = FakeBot()
    controller = BotController(events, bot_factory=lambda **kw: bot)
    controller.start(config=object(), advisor=object(), audit=object())
    try:
        assert wait_for(controller, RUNNING)
        controller.set_human_in_the_loop(True)
        assert bot.mode.enabled is True
        controller.set_human_in_the_loop(False)
        assert bot.mode.enabled is False
    finally:
        controller.stop()


def test_set_human_in_the_loop_is_a_harmless_no_op_before_any_bot_exists():
    controller = BotController(queue.Queue(), bot_factory=lambda **kw: FakeBot())
    controller.set_human_in_the_loop(True)  # must not raise
    assert controller.state.state == STOPPED


# -- I1/I2: the bot factory must receive live pending/error callbacks -------


def test_bot_factory_receives_pending_and_error_callbacks():
    """RangeControlBot needs a way to publish onto the GUI's event queue
    without importing gui.events itself -- these two callbacks are it.
    Calling them directly here (off the bot thread) proves the wiring
    without needing a real RangeControlBot or PendingRegistry."""
    from rangecontrol.gui.events import ERROR, PENDING

    events = queue.Queue()
    captured = {}

    def factory(**kwargs):
        captured.update(kwargs)
        return FakeBot()

    controller = BotController(events, bot_factory=factory)
    controller.start(config=object(), advisor=object(), audit=object())
    try:
        assert wait_for(controller, RUNNING)
    finally:
        controller.stop()

    assert callable(captured.get("on_pending_change"))
    assert callable(captured.get("on_error"))

    captured["on_pending_change"](2)
    captured["on_error"]("white cell unreachable")

    kinds = [(e.kind, e.payload) for e in drain(events)]
    assert (PENDING, 2) in kinds
    assert (ERROR, "white cell unreachable") in kinds


def test_set_human_in_the_loop_is_a_harmless_no_op_after_stop():
    events = queue.Queue()
    controller = BotController(events, bot_factory=lambda **kw: FakeBot())
    controller.start(config=object(), advisor=object(), audit=object())
    assert wait_for(controller, RUNNING)
    controller.stop()
    controller.set_human_in_the_loop(True)  # must not raise: no bot to reach


def test_set_denied_text_updates_the_live_bot():
    events = queue.Queue()
    bot = FakeBot()
    controller = BotController(events, bot_factory=lambda **kwargs: bot)
    controller.start(config=object(), advisor=object(), audit=object())
    try:
        wait_for(controller, RUNNING)
        controller.set_denied_text("Nope.")
        assert bot.denied_text == "Nope."
    finally:
        controller.stop()


def test_set_denied_text_is_a_harmless_no_op_before_any_bot_exists():
    controller = BotController(queue.Queue())
    controller.set_denied_text("Nope.")  # must not raise
