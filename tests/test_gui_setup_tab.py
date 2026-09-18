import pytest

import rangecontrol.gui.setup_tab as setup_tab_module
from rangecontrol.gui.settings import Field
from rangecontrol.range_doc.report import IngestReport
from tests.support.tk import make_root

REPORT = IngestReport(
    total_files=2, by_kind=(("text", 2),),
    unreadable=(("diagrams/topology.vsdx", "unsupported format"),),
    range_md_generated=False, range_md_gaps=(), index_rows=19,
    inject_count=8, context_chars=35000,
)


@pytest.fixture
def tab():
    root = make_root()
    from rangecontrol.gui.setup_tab import SetupTab

    widget = SetupTab(root, on_change=lambda: None, on_generate=lambda: None,
                      on_start_stop=lambda: None, on_open_resources=lambda: None)
    yield widget
    root.destroy()


def test_values_round_trip(tab):
    tab.set_values({"LLM_PROVIDER": "gemini", "LLM_API_KEY": "abc"})
    assert tab.values()["LLM_PROVIDER"] == "gemini"
    assert tab.values()["LLM_API_KEY"] == "abc"


def test_secrets_are_masked(tab):
    assert tab.entry_for("LLM_API_KEY").cget("show") == "•"
    assert tab.entry_for("DISCORD_BOT_TOKEN").cget("show") == "•"
    assert tab.entry_for("WHITE_CELL_CHANNEL_ID").cget("show") == ""


def test_generate_is_disabled_without_resources(tab):
    tab.set_resources_present(False)
    assert str(tab.generate_button.cget("state")) == "disabled"
    assert "resources" in tab.generate_hint().lower()


def test_generate_enables_once_resources_exist(tab):
    tab.set_resources_present(True)
    assert str(tab.generate_button.cget("state")) == "normal"


def test_generate_is_disabled_while_the_bot_runs(tab):
    """The advisor holds the built context in memory."""
    tab.set_resources_present(True)
    tab.set_bot_running(True)
    assert str(tab.generate_button.cget("state")) == "disabled"


def test_start_is_disabled_until_configured_and_ingested(tab):
    tab.set_values({"LLM_PROVIDER": "gemini"})
    assert str(tab.start_button.cget("state")) == "disabled"


def test_start_enables_when_configured_and_a_report_exists(tab):
    tab.set_values({
        "LLM_PROVIDER": "gemini", "LLM_API_KEY": "k",
        "DISCORD_BOT_TOKEN": "t", "WHITE_CELL_CHANNEL_ID": "1",
    })
    tab.set_report(REPORT, error="")
    assert str(tab.start_button.cget("state")) == "normal"


def test_button_label_flips_to_regenerate_when_a_range_exists(tab):
    tab.set_resources_present(True)
    tab.set_report(REPORT, error="")
    assert tab.generate_button.cget("text") == "Regenerate"


def test_unreadable_files_are_shown(tab):
    tab.set_report(REPORT, error="")
    assert "topology.vsdx" in tab.status_text()


def test_ingest_error_is_shown(tab):
    tab.set_report(None, error="Anthropic request failed: AuthenticationError")
    assert "AuthenticationError" in tab.status_text()


# -- I5: all five IngestReport.has_concerns() signals must be visible --------


def test_empty_dependency_index_is_flagged(tab):
    report = IngestReport(
        total_files=1, by_kind=(("text", 1),), unreadable=(),
        range_md_generated=False, range_md_gaps=(), index_rows=0,
        inject_count=8, context_chars=1000,
    )
    tab.set_report(report, error="")
    text = tab.status_text()
    assert "⚠" in text
    assert "index" in text.lower()


def test_zero_injects_is_flagged(tab):
    report = IngestReport(
        total_files=1, by_kind=(("text", 1),), unreadable=(),
        range_md_generated=False, range_md_gaps=(), index_rows=5,
        inject_count=0, context_chars=1000,
    )
    tab.set_report(report, error="")
    text = tab.status_text()
    assert "⚠" in text
    assert "inject" in text.lower()


def test_oversized_context_is_flagged(tab):
    from rangecontrol.range_doc.report import LARGE_CONTEXT_CHARS

    report = IngestReport(
        total_files=1, by_kind=(("text", 1),), unreadable=(),
        range_md_generated=False, range_md_gaps=(), index_rows=5,
        inject_count=3, context_chars=LARGE_CONTEXT_CHARS + 1,
    )
    tab.set_report(report, error="")
    assert "⚠" in tab.status_text()


def test_a_clean_report_shows_no_warning_glyphs(tab):
    """Control: a healthy ingest must not grow a spurious ⚠ line."""
    report = IngestReport(
        total_files=2, by_kind=(("text", 2),), unreadable=(),
        range_md_generated=False, range_md_gaps=(), index_rows=19,
        inject_count=8, context_chars=1000,
    )
    tab.set_report(report, error="")
    assert "⚠" not in tab.status_text()


def test_start_bot_is_not_gated_on_concern_flags(tab):
    """The CLI's [y/N] gate still allowed proceeding after an explicit "y";
    the GUI panel must make every concern visible, not block on any of
    them -- Start Bot is gated on is_configured() and a report existing,
    nothing about the report's content."""
    report = IngestReport(
        total_files=1, by_kind=(("text", 1),), unreadable=(),
        range_md_generated=False, range_md_gaps=(), index_rows=0,
        inject_count=0, context_chars=1,
    )
    tab.set_values({
        "LLM_PROVIDER": "gemini", "LLM_API_KEY": "k",
        "DISCORD_BOT_TOKEN": "t", "WHITE_CELL_CHANNEL_ID": "1",
    })
    tab.set_report(report, error="")
    assert str(tab.start_button.cget("state")) == "normal"


def test_switching_provider_swaps_the_model_suggestions(tab):
    tab.set_values({"LLM_PROVIDER": "anthropic"})
    assert any("claude" in m for m in tab.model_choices())
    tab.set_values({"LLM_PROVIDER": "gemini"})
    assert any("gemini" in m for m in tab.model_choices())


def test_blank_provider_in_set_values_still_leaves_a_non_empty_provider(tab):
    """LLM_PROVIDER is a required field with no fallback in load_config, so a

    fresh window whose provider combobox is blank would leave Start Bot
    permanently disabled even once everything else is filled in. set_values
    must default the provider rather than accepting the blank.
    """
    tab.set_values({
        "LLM_API_KEY": "k", "DISCORD_BOT_TOKEN": "t",
        "WHITE_CELL_CHANNEL_ID": "1",
    })
    assert tab.values()["LLM_PROVIDER"] != ""


def test_fresh_tab_has_a_non_empty_provider_before_any_set_values_call(tab):
    assert tab.values()["LLM_PROVIDER"] != ""


def test_start_button_label_flips_when_the_bot_is_running(tab):
    assert tab.start_button.cget("text") == "Start Bot"
    tab.set_bot_running(True)
    assert tab.start_button.cget("text") == "Stop Bot"
    tab.set_bot_running(False)
    assert tab.start_button.cget("text") == "Start Bot"


def test_hint_combines_both_blockers_when_resources_missing_and_bot_running(tab):
    tab.set_resources_present(False)
    tab.set_bot_running(True)
    hint = tab.generate_hint().lower()
    assert "resources" in hint
    assert "bot" in hint


def test_human_in_the_loop_is_a_checkbox(tab):
    widget = tab.entry_for("HUMAN_IN_THE_LOOP")
    assert widget.winfo_class() == "TCheckbutton"


def test_the_checkbox_round_trips_as_a_boolean_string(tab):
    tab.set_values({"HUMAN_IN_THE_LOOP": "true"})
    assert tab.values()["HUMAN_IN_THE_LOOP"] == "true"
    tab.set_values({"HUMAN_IN_THE_LOOP": ""})
    assert tab.values()["HUMAN_IN_THE_LOOP"] == "false"


def test_the_checkbox_stays_editable_while_the_bot_runs(tab):
    """Toggling mid-exercise is the point; it must not require a restart."""
    tab.set_bot_running(True)
    assert str(tab.entry_for("HUMAN_IN_THE_LOOP").cget("state")) != "disabled"


def test_a_fresh_tab_has_the_checkbox_unchecked(tab):
    """The default in config.py's _parse_bool is False; the widget must not
    default to the blank string, which would round-trip as "" rather than the
    explicit "false" a fresh .env needs on disk."""
    assert tab.values()["HUMAN_IN_THE_LOOP"] == "false"


def test_column_split_handles_seven_fields_without_dropping_or_overlapping(monkeypatch):
    """The split exists precisely for the case where FIELDS grows past six.

    LLM_PROVIDER is included among the seven: the module hard-depends on that
    key existing (_sync_model_choices subscripts it directly, and the default
    provider is seeded into a var of that name), so a FIELDS without it is a
    programming error the module should keep crashing loudly on, not a state
    this test should pretend is reachable.
    """
    fields = (Field("LLM_PROVIDER", "AI provider"),) + tuple(
        Field(f"FIELD_{i}", f"Field {i}") for i in range(6)
    )
    monkeypatch.setattr(setup_tab_module, "FIELDS", fields)

    root = make_root()
    widget = setup_tab_module.SetupTab(
        root, on_change=lambda: None, on_generate=lambda: None,
        on_start_stop=lambda: None, on_open_resources=lambda: None,
    )
    try:
        assert set(widget._entries.keys()) == {f.key for f in fields}

        placements = []
        for field in fields:
            cell = widget._entries[field.key].master
            column_frame = cell.master  # each column is its own grid
            column = int(column_frame.grid_info()["column"])
            placements.append((column, int(cell.grid_info()["row"])))

        assert len(placements) == 7  # nothing dropped
        assert len(set(placements)) == 7  # nothing overlapping
        assert {column for column, _ in placements} == {0, 1}  # exactly two columns
    finally:
        root.destroy()


# --- refreshing the resources folder ---------------------------------------
#
# Dropping files into resources/ while the window is open used to leave
# Generate dull until a restart: the folder was only ever checked once, at
# startup. The hint tells the user to put material there, so the action it
# asks for has to be completable without closing the app.


def test_a_refresh_button_sits_with_the_resources_hint(tab):
    assert tab.refresh_button.winfo_class() == "TButton"
    assert "refresh" in tab.refresh_button.cget("text").lower()


def test_refresh_invokes_its_callback():
    root = make_root()
    from rangecontrol.gui.setup_tab import SetupTab

    called = []
    widget = SetupTab(root, on_change=lambda: None, on_generate=lambda: None,
                      on_start_stop=lambda: None, on_open_resources=lambda: None,
                      on_refresh=lambda: called.append(1))
    try:
        widget.refresh_button.invoke()
        assert called == [1]
    finally:
        root.destroy()


def test_refresh_stays_available_when_generate_is_dull(tab):
    """It is the way out of the dull state, so it cannot share the same gate."""
    tab.set_resources_present(False)
    assert str(tab.generate_button.cget("state")) == "disabled"
    assert str(tab.refresh_button.cget("state")) != "disabled"


def test_refresh_stays_available_while_the_bot_runs(tab):
    tab.set_bot_running(True)
    assert str(tab.refresh_button.cget("state")) != "disabled"


# --- fetched models must survive later edits -------------------------------


def test_fetched_models_survive_selecting_one(tab):
    """Reported: pick a fetched model, reopen the dropdown, only defaults show.

    Every variable write runs _changed(), which re-applied the hardcoded
    suggestions and threw the fetched list away.
    """
    tab.set_values({"LLM_PROVIDER": "gemini"})
    tab.set_model_choices(("gemini-3.5-flash", "gemini-3.5-pro"))
    tab.set_values({"LLM_PROVIDER": "gemini", "LLM_MODEL": "gemini-3.5-flash"})
    assert tab.model_choices() == ("gemini-3.5-flash", "gemini-3.5-pro")


def test_fetched_models_survive_editing_an_unrelated_field(tab):
    tab.set_values({"LLM_PROVIDER": "gemini"})
    tab.set_model_choices(("gemini-3.5-flash",))
    tab.set_values({"LLM_PROVIDER": "gemini", "WHITE_CELL_CHANNEL_ID": "42"})
    assert tab.model_choices() == ("gemini-3.5-flash",)


def test_switching_provider_drops_the_other_providers_fetched_list(tab):
    """An Anthropic key cannot reach Gemini models; showing them would mislead."""
    tab.set_values({"LLM_PROVIDER": "gemini"})
    tab.set_model_choices(("gemini-3.5-flash",))
    tab.set_values({"LLM_PROVIDER": "anthropic"})
    assert "gemini-3.5-flash" not in tab.model_choices()
    assert any("claude" in m for m in tab.model_choices())


def test_switching_back_restores_that_providers_fetched_list(tab):
    tab.set_values({"LLM_PROVIDER": "gemini"})
    tab.set_model_choices(("gemini-3.5-flash",))
    tab.set_values({"LLM_PROVIDER": "anthropic"})
    tab.set_values({"LLM_PROVIDER": "gemini"})
    assert tab.model_choices() == ("gemini-3.5-flash",)


def test_the_resources_hint_wraps_instead_of_running_off_the_window(tab):
    tab.set_resources_present(False)
    assert int(tab.hint_label.cget("wraplength")) > 0


def test_the_resources_hint_has_no_em_dash(tab):
    tab.set_resources_present(False)
    assert "—" not in tab.generate_hint()


def test_allowed_channels_says_it_wants_ids(tab):
    from rangecontrol.gui.settings import FIELDS

    hint = next(f.hint for f in FIELDS if f.key == "ALLOWED_CHANNEL_IDS")
    assert "ID" in hint


def test_the_checkbox_description_sits_above_the_box_not_below(tab):
    """It explains what ticking does, so it belongs above the thing you tick."""
    box = tab.entry_for("HUMAN_IN_THE_LOOP")
    cell = box.master
    box_row = int(box.grid_info()["row"])
    hint_rows = [
        int(child.grid_info()["row"])
        for child in cell.winfo_children()
        if child.winfo_class() == "TLabel" and "hold every ruling" in child.cget("text")
    ]
    assert hint_rows, "the checkbox has no description label"
    assert hint_rows[0] < box_row


# --- extra instructions -----------------------------------------------------


def test_extra_instructions_is_a_multiline_box(tab):
    """Guidance is a paragraph, not a word; a one-line Entry would fight it."""
    widget = tab.entry_for("EXTRA_INSTRUCTIONS")
    assert widget.winfo_class() == "Text"


def test_extra_instructions_round_trip_including_newlines(tab):
    text = "Exercise runs 0800-1700.\nTreat the DMZ as out of scope."
    tab.set_values({"EXTRA_INSTRUCTIONS": text})
    assert tab.values()["EXTRA_INSTRUCTIONS"] == text


def test_extra_instructions_default_to_empty(tab):
    tab.set_values({"LLM_PROVIDER": "gemini"})
    assert tab.values()["EXTRA_INSTRUCTIONS"] == ""


def test_editing_the_box_fires_the_change_callback():
    """Otherwise the guidance would never reach .env."""
    root = make_root()
    from rangecontrol.gui.setup_tab import SetupTab

    fired = []
    widget = SetupTab(root, on_change=lambda: fired.append(1),
                      on_generate=lambda: None, on_start_stop=lambda: None,
                      on_open_resources=lambda: None)
    try:
        fired.clear()
        widget.entry_for("EXTRA_INSTRUCTIONS").insert("1.0", "hold DMZ changes")
        widget.update()
        assert fired, "editing the guidance box did not trigger a save"
    finally:
        root.destroy()


def test_extra_instructions_is_not_required(tab):
    from rangecontrol.gui.settings import missing_required

    assert "Extra instructions to the AI" not in missing_required({})


# --- hints must wrap inside their own column -------------------------------


def _resize_cell(cell, width):
    """Fire the <Configure> the geometry manager would fire on a real layout.

    The test root is withdrawn, so nothing is ever mapped and every widget
    reports width 1 - measuring real geometry here would prove nothing.
    Driving the binding directly tests the thing that matters: what the hint
    does with the width it is given.
    """
    import tkinter as tk

    event = tk.Event()
    event.width = width
    for handler in cell.bind("<Configure>", None).split("\n"):
        pass
    cell.event_generate("<Configure>", width=width, height=80)
    cell.update_idletasks()


def test_hints_wrap_to_their_column_not_a_fixed_width(tab):
    """Reported: the Discord token hint was clipped by the column divider.

    A fixed wraplength overflows whenever the column is narrower than it, and
    the neighbouring column then draws over the overflow.
    """
    hint = _hint_label_for(tab, "DISCORD_BOT_TOKEN")
    _resize_cell(hint.master, 260)
    assert int(hint.cget("wraplength")) <= 260


def test_every_hint_follows_its_cell_width(tab):
    from rangecontrol.gui.settings import FIELDS

    for field in FIELDS:
        if not field.hint:
            continue
        hint = _hint_label_for(tab, field.key)
        _resize_cell(hint.master, 240)
        assert int(hint.cget("wraplength")) <= 240, field.key


def test_a_very_narrow_column_still_leaves_a_readable_width(tab):
    """Clamped, so squeezing the window cannot collapse the text to nothing."""
    hint = _hint_label_for(tab, "DISCORD_BOT_TOKEN")
    _resize_cell(hint.master, 40)
    assert int(hint.cget("wraplength")) >= 160


def test_the_two_columns_are_declared_uniform(tab):
    """uniform= keeps them equal, so neither can starve the other."""
    # cell -> column frame -> the two-column grid
    grid = tab.entry_for("DISCORD_BOT_TOKEN").master.master.master
    left = grid.columnconfigure(0)
    right = grid.columnconfigure(1)
    assert left["uniform"] == right["uniform"] != ""
    assert int(left["weight"]) == int(right["weight"]) == 1


def _hint_label_for(tab, key):
    """The grey hint label living in the same cell as ``key``'s widget."""
    from rangecontrol.gui.settings import FIELDS

    text = next(f.hint for f in FIELDS if f.key == key)
    cell = tab.entry_for(key).master
    for child in cell.winfo_children():
        if child.winfo_class() == "TLabel" and child.cget("text") == text:
            return child
    raise AssertionError(f"no hint label found for {key}")


def test_refresh_sits_after_open_resources_and_is_named_for_what_it_does(tab):
    """"Refresh" alone reads as refreshing the whole app, not the folder."""
    assert tab.refresh_button.cget("text") == "Refresh Resources"
    open_col = None
    for child in tab.refresh_button.master.winfo_children():
        if child.winfo_class() == "TButton" and child.cget("text") == "Open resources folder":
            open_col = int(child.grid_info()["column"])
    assert open_col is not None, "Open resources folder button not found"
    assert int(tab.refresh_button.grid_info()["column"]) > open_col


def test_denied_reply_shows_the_default_when_blank(tab):
    """A blank field would send a blank denial; the operator should see the
    text that will actually go out and edit from there."""
    from rangecontrol.config import DEFAULT_HITL_DENIED_TEXT

    tab.set_values({})
    assert tab.values()["HITL_DENIED_TEXT"] == DEFAULT_HITL_DENIED_TEXT
    tab.set_values({"HITL_DENIED_TEXT": "Custom no."})
    assert tab.values()["HITL_DENIED_TEXT"] == "Custom no."


def test_credentials_sit_left_and_discord_settings_right(tab):
    """The white cell ID belongs with the other Discord settings, directly
    above Allowed channels, and the left column holds only the four
    provider and token fields so nothing pads them apart."""
    def placement(key):
        cell = tab.entry_for(key).master
        return int(cell.master.grid_info()["column"]), int(cell.grid_info()["row"])

    assert [placement(k)[0] for k in ("LLM_PROVIDER", "LLM_MODEL", "LLM_API_KEY", "DISCORD_BOT_TOKEN")] == [0, 0, 0, 0]
    assert placement("WHITE_CELL_CHANNEL_ID") == (1, 0)
    assert placement("ALLOWED_CHANNEL_IDS") == (1, 1)
    assert placement("HITL_DENIED_TEXT")[0] == 1


def test_left_column_rows_are_not_stretched_by_the_right_column(tab):
    """A tall text box on the right must not open a gap under a one-line
    entry on the left: the columns lay out independently."""
    tab.update_idletasks()
    key_cell = tab.entry_for("LLM_API_KEY").master
    token_cell = tab.entry_for("DISCORD_BOT_TOKEN").master
    gap = token_cell.winfo_y() - (key_cell.winfo_y() + key_cell.winfo_reqheight())
    assert gap <= 2


def test_denied_reply_has_no_hint_text(tab):
    cell = tab.entry_for("HITL_DENIED_TEXT").master
    labels = [c.cget("text") for c in cell.winfo_children() if c.winfo_class() == "TLabel"]
    assert labels == ["Reply when the white cell denies"]


def test_denied_reply_is_editable_only_with_human_in_the_loop(tab):
    """The message is never sent unless rulings are held, so the box is
    disabled until the checkbox is ticked, and re-enabled live."""
    widget = tab.entry_for("HITL_DENIED_TEXT")
    tab.set_values({"HUMAN_IN_THE_LOOP": "false"})
    assert str(widget.cget("state")) == "disabled"
    # A load still lands in the disabled box, and values() still reads it.
    from rangecontrol.config import DEFAULT_HITL_DENIED_TEXT

    assert tab.values()["HITL_DENIED_TEXT"] == DEFAULT_HITL_DENIED_TEXT

    tab._vars["HUMAN_IN_THE_LOOP"].set("true")
    assert str(widget.cget("state")) == "normal"
    tab._vars["HUMAN_IN_THE_LOOP"].set("false")
    assert str(widget.cget("state")) == "disabled"
