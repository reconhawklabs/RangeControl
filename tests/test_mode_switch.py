import threading
import time

from rangecontrol.bot.mode import ModeSwitch


def test_defaults_to_the_value_it_was_constructed_with():
    assert ModeSwitch(True).enabled is True
    assert ModeSwitch(False).enabled is False


def test_set_changes_the_value():
    switch = ModeSwitch(False)
    switch.set(True)
    assert switch.enabled is True
    switch.set(False)
    assert switch.enabled is False


def test_a_change_on_one_thread_is_visible_on_another():
    """The GUI toggles from the Tk thread; the bot reads from its own."""
    switch = ModeSwitch(False)
    seen = []

    def reader():
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if switch.enabled:
                seen.append(True)
                return
            time.sleep(0.001)

    thread = threading.Thread(target=reader)
    thread.start()
    switch.set(True)
    thread.join(timeout=10)
    assert not thread.is_alive()
    assert seen == [True]
