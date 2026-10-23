import pytest

import rangecontrol.gui.setup_tab as setup_tab_module
from rangecontrol.config import DEFAULT_HITL_DENIED_TEXT
from rangecontrol.gui.settings import FIELDS, Field
from rangecontrol.range_doc.report import LARGE_CONTEXT_CHARS, IngestReport
from tests.support.tk import make_root

COMPLETE = {
    "LLM_PROVIDER": "gemini", "LLM_API_KEY": "k",
    "DISCORD_BOT_TOKEN": "t", "WHITE_CELL_CHANNEL_ID": "1",
}


def report(**overrides):
    fields = dict(
        total_files=2, by_kind=(("text", 2),), unreadable=(),
        range_md_generated=False, range_md_gaps=(), index_rows=19,
        inject_count=8, context_chars=1000,
    )
    return IngestReport(**{**fields, **overrides})


def make_tab(**callbacks):
    root = make_root()
    handlers = dict(on_change=lambda: None, on_generate=lambda: None,
                    on_start_stop=lambda: None, on_open_resources=lambda: None)
    widget = setup_tab_module.SetupTab(root, **{**handlers, **callbacks})
    return root, widget


@pytest.fixture
def tab():
    root, widget = make_tab()
    yield widget
    root.destroy()


def state(widget):
    return str(widget.cget("state"))


def test_values_round_trip_through_every_kind_of_field(tab):
    """What set_values() loads from .env is exactly what values() saves back."""
    loaded = {
        "LLM_PROVIDER": "gemini",
        "LLM_API_KEY": "abc",
        "HUMAN_IN_THE_LOOP": "true",
        "EXTRA_INSTRUCTIONS": "Exercise runs 0800-1700.\nTreat the DMZ as out of scope.",
        "HITL_DENIED_TEXT": "Custom no.",
    }
    tab.set_values(loaded)
    values = tab.values()
    for key, value in loaded.items():
        assert values[key] == value, key


def test_blank_values_fall_back_to_what_will_actually_be_used(tab):
    """A blank provider would leave Start Bot dead forever, a blank checkbox
    must save as an explicit "false", and a blank denial would send nothing."""
    fresh = tab.values()
    assert fresh["LLM_PROVIDER"] != ""
    assert fresh["HUMAN_IN_THE_LOOP"] == "false"

    tab.set_values({"LLM_API_KEY": "k", "HUMAN_IN_THE_LOOP": ""})
    values = tab.values()
    assert values["LLM_PROVIDER"] != ""
    assert values["HUMAN_IN_THE_LOOP"] == "false"
    assert values["EXTRA_INSTRUCTIONS"] == ""
    assert values["HITL_DENIED_TEXT"] == DEFAULT_HITL_DENIED_TEXT


def test_secrets_are_masked(tab):
    assert tab.entry_for("LLM_API_KEY").cget("show") == "•"
    assert tab.entry_for("DISCORD_BOT_TOKEN").cget("show") == "•"
    assert tab.entry_for("WHITE_CELL_CHANNEL_ID").cget("show") == ""


def test_generate_needs_resources_and_a_stopped_bot_and_refresh_stays_available(tab):
    """The advisor holds the built context while the bot runs. Refresh is the
    way out of the dull state, so it cannot share Generate's gate."""
    tab.set_resources_present(False)
    assert state(tab.generate_button) == "disabled"
    assert "resources" in tab.generate_hint().lower()
    assert state(tab.refresh_button) != "disabled"

    tab.set_bot_running(True)
    hint = tab.generate_hint().lower()
    assert "resources" in hint and "bot" in hint
    assert state(tab.refresh_button) != "disabled"

    tab.set_resources_present(True)
    assert state(tab.generate_button) == "disabled"

    tab.set_bot_running(False)
    assert state(tab.generate_button) == "normal"
    assert tab.generate_button.cget("text") == "Generate"
    tab.set_report(report(), error="")
    assert tab.generate_button.cget("text") == "Regenerate"


def test_start_needs_a_complete_config_and_any_report(tab):
    """Concerns in the report are shown, never blocking: the CLI's [y/N] gate
    still let an operator proceed past them."""
    tab.set_values(COMPLETE)
    assert state(tab.start_button) == "disabled"  # nothing ingested yet

    tab.set_report(report(index_rows=0, inject_count=0, context_chars=1), error="")
    assert state(tab.start_button) == "normal"

    tab.set_values({"LLM_PROVIDER": "gemini"})
    assert state(tab.start_button) == "disabled"

    assert tab.start_button.cget("text") == "Start Bot"
    tab.set_bot_running(True)
    assert tab.start_button.cget("text") == "Stop Bot"
    tab.set_bot_running(False)
    assert tab.start_button.cget("text") == "Start Bot"


def test_the_report_panel_flags_every_concern_and_shows_errors(tab):
    tab.set_report(report(), error="")
    assert "⚠" not in tab.status_text()  # a healthy ingest grows no warning

    concerns = [
        (report(unreadable=(("diagrams/topology.vsdx", "unsupported format"),)),
         "topology.vsdx"),
        (report(range_md_gaps=("Scope",)), "scope"),
        (report(index_rows=0), "index"),
        (report(inject_count=0), "inject"),
        (report(context_chars=LARGE_CONTEXT_CHARS + 1), "context"),
    ]
    for concerning, fragment in concerns:
        tab.set_report(concerning, error="")
        warnings = [line.lower() for line in tab.status_text().splitlines() if "⚠" in line]
        assert any(fragment in line for line in warnings), fragment

    tab.set_report(None, error="Anthropic request failed: AuthenticationError")
    assert "AuthenticationError" in tab.status_text()


def test_model_suggestions_follow_the_provider_and_keep_fetched_lists(tab):
    """Reported: pick a fetched model, reopen the dropdown, only defaults show."""
    tab.set_values({"LLM_PROVIDER": "anthropic"})
    assert any("claude" in m for m in tab.model_choices())
    tab.set_values({"LLM_PROVIDER": "gemini"})
    assert any("gemini" in m for m in tab.model_choices())

    fetched = ("gemini-3.5-flash", "gemini-3.5-pro")
    tab.set_model_choices(fetched)
    tab.set_values({"LLM_PROVIDER": "gemini", "LLM_MODEL": "gemini-3.5-flash"})
    assert tab.model_choices() == fetched
    tab.set_values({"LLM_PROVIDER": "gemini", "WHITE_CELL_CHANNEL_ID": "42"})
    assert tab.model_choices() == fetched

    # An Anthropic key cannot reach Gemini models; showing them would mislead.
    tab.set_values({"LLM_PROVIDER": "anthropic"})
    assert "gemini-3.5-flash" not in tab.model_choices()
    assert any("claude" in m for m in tab.model_choices())
    tab.set_values({"LLM_PROVIDER": "gemini"})
    assert tab.model_choices() == fetched


def test_human_in_the_loop_stays_live_and_unlocks_the_denied_reply(tab):
    """Toggling mid-exercise is the point. The denial is only ever sent while
    rulings are held, so its box is editable only then, and re-gated live."""
    tab.set_bot_running(True)
    assert state(tab.entry_for("HUMAN_IN_THE_LOOP")) != "disabled"

    reply = tab.entry_for("HITL_DENIED_TEXT")
    tab.set_values({"HUMAN_IN_THE_LOOP": "false"})
    assert state(reply) == "disabled"
    assert tab.values()["HITL_DENIED_TEXT"] == DEFAULT_HITL_DENIED_TEXT  # load lands anyway

    tab._vars["HUMAN_IN_THE_LOOP"].set("true")
    assert state(reply) == "normal"
    tab._vars["HUMAN_IN_THE_LOOP"].set("false")
    assert state(reply) == "disabled"


def test_refresh_invokes_its_callback():
    """Files dropped into resources/ used to leave Generate dull until a restart."""
    called = []
    root, widget = make_tab(on_refresh=lambda: called.append(1))
    try:
        widget.refresh_button.invoke()
        assert called == [1]
    finally:
        root.destroy()


def test_typing_extra_instructions_fires_the_change_callback():
    """Otherwise the guidance would never reach .env."""
    fired = []
    root, widget = make_tab(on_change=lambda: fired.append(1))
    try:
        fired.clear()
        widget.entry_for("EXTRA_INSTRUCTIONS").insert("1.0", "hold DMZ changes")
        widget.update()
        assert fired, "editing the guidance box did not trigger a save"
    finally:
        root.destroy()


def test_every_field_gets_its_own_cell_when_fields_grow(monkeypatch):
    """The two-column split exists for FIELDS growing past six: nothing may be
    dropped or drawn on top of another field. LLM_PROVIDER stays in because
    the module hard-depends on it."""
    fields = (Field("LLM_PROVIDER", "AI provider"),) + tuple(
        Field(f"FIELD_{i}", f"Field {i}") for i in range(6)
    )
    monkeypatch.setattr(setup_tab_module, "FIELDS", fields)

    root, widget = make_tab()
    try:
        assert set(widget._entries.keys()) == {f.key for f in fields}
        placements = set()
        for field in fields:
            cell = widget._entries[field.key].master
            column = int(cell.master.grid_info()["column"])
            placements.add((column, int(cell.grid_info()["row"])))
        assert len(placements) == 7  # nothing dropped or overlapping
        assert {column for column, _ in placements} == {0, 1}
    finally:
        root.destroy()


def test_every_hint_wraps_to_its_column_and_stays_readable(tab):
    """Reported: the Discord token hint was clipped by the column divider.
    The test root is never mapped, so drive the <Configure> binding directly."""

    def hint_label_for(key):
        text = next(f.hint for f in FIELDS if f.key == key)
        cell = tab.entry_for(key).master
        for child in cell.winfo_children():
            if child.winfo_class() == "TLabel" and child.cget("text") == text:
                return child
        raise AssertionError(f"no hint label found for {key}")

    def resize(cell, width):
        cell.event_generate("<Configure>", width=width, height=80)
        cell.update_idletasks()

    for field in FIELDS:
        if not field.hint:
            continue
        hint = hint_label_for(field.key)
        resize(hint.master, 240)
        assert int(hint.cget("wraplength")) <= 240, field.key

    narrow = hint_label_for("DISCORD_BOT_TOKEN")
    resize(narrow.master, 40)
    assert int(narrow.cget("wraplength")) >= 160
