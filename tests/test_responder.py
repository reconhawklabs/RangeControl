from rangecontrol.advisor.models import Advice, GateResult, GateVerdict, Ruling
from rangecontrol.bot.responder import DISCORD_LIMIT, format_public
from rangecontrol.bot.whitecell import build_embed, embed_fields


def ruling(verdict="DENIED", confidence="high", public="No. A partner service uses it."):
    return Ruling(
        verdict=verdict,
        public_response=public,
        internal_reason="would break MSEL-01 callback to DC-VULCAN",
        impacted=("MSEL-01",),
        confidence=confidence,
    )


def advice(**overrides):
    payload = {
        "kind": "ruling",
        "public_text": "No. A partner service uses it.",
        "ruling": ruling(),
        "gate": GateResult(verdict=GateVerdict.CHANGE_REQUEST, reason="r"),
    }
    payload.update(overrides)
    return Advice(**payload)


def test_denial_is_sent_verbatim_with_no_added_framing():
    assert format_public(advice()) == "No. A partner service uses it."


def test_approval_is_sent_verbatim():
    approved = advice(
        kind="ruling",
        public_text="Approved. Watch your own management path.",
        ruling=ruling(verdict="APPROVED", public="Approved. Watch your own management path."),
    )
    assert format_public(approved) == "Approved. Watch your own management path."


def test_deflection_and_error_text_pass_through():
    assert format_public(advice(kind="deflection", public_text="D", ruling=None)) == "D"
    assert format_public(advice(kind="error", public_text="E", ruling=None)) == "E"


def test_output_never_exceeds_the_discord_limit():
    long = advice(public_text="x" * (DISCORD_LIMIT + 500), ruling=ruling(public="x" * 3000))
    assert len(format_public(long)) <= DISCORD_LIMIT


def test_public_output_never_contains_internal_reason_or_impacted():
    rendered = format_public(advice())
    assert "MSEL-01" not in rendered
    assert "DC-VULCAN" not in rendered


def test_public_output_never_labels_the_confidence():
    assert "confidence" not in format_public(advice(ruling=ruling(confidence="low"))).lower()


def test_white_cell_fields_carry_the_real_reason():
    fields = dict((name, value) for name, value, _ in embed_fields(
        advice(), user_name="blue-lead", channel_name="ops", question="block it"
    ))
    joined = " ".join(fields.values())
    assert "DC-VULCAN" in joined
    assert "MSEL-01" in joined
    assert "blue-lead" in joined
    assert "block it" in joined


def test_white_cell_includes_the_exact_public_text_that_was_sent():
    fields = embed_fields(
        advice(), user_name="u", channel_name="c", question="q"
    )
    assert any("No. A partner service uses it." in value for _, value, _ in fields)


def test_low_confidence_is_flagged_for_the_white_cell():
    fields = embed_fields(
        advice(ruling=ruling(confidence="low")),
        user_name="u", channel_name="c", question="q",
    )
    joined = " ".join(value for _, value, _ in fields)
    assert "⚠" in joined or "LOW" in joined.upper()


def test_error_advice_produces_an_embed_with_the_error_detail():
    fields = embed_fields(
        Advice(kind="error", public_text="can't process", error="LLMError('down')"),
        user_name="u", channel_name="c", question="q",
    )
    assert any("LLMError" in value for _, value, _ in fields)


def test_every_embed_field_fits_discord_limits():
    long_advice = advice(ruling=ruling(public="p" * 3000))
    for name, value, _ in embed_fields(
        long_advice, user_name="u", channel_name="c", question="q" * 3000
    ):
        assert len(name) <= 256
        assert len(value) <= 1024


def test_awaiting_review_appends_a_field_instructing_the_white_cell_to_react():
    fields = embed_fields(
        advice(), user_name="u", channel_name="c", question="q", awaiting=True
    )
    assert "Awaiting review" in [name for name, _, _ in fields]


def test_awaiting_review_prefixes_the_embed_title():
    embed = build_embed(
        advice(), user_name="u", channel_name="c", question="q", awaiting=True
    )
    assert embed.title.startswith("⏳ AWAITING REVIEW")


def test_without_awaiting_the_embed_has_no_review_prompt():
    fields = embed_fields(advice(), user_name="u", channel_name="c", question="q")
    assert "Awaiting review" not in [name for name, _, _ in fields]

    embed = build_embed(advice(), user_name="u", channel_name="c", question="q")
    assert not embed.title.startswith("⏳")


# --- explaining why something was not held ---------------------------------


def test_a_non_ruling_says_why_it_was_not_held_when_hitl_is_on():
    """The operator ticked "hold every ruling" and is watching this embed.

    Nothing here distinguished "held for you" from "answered already" except
    the absence of a prompt, which reads as the feature being broken.
    """
    from rangecontrol.advisor.models import Advice
    from rangecontrol.bot.whitecell import embed_fields

    advice = Advice(kind="clarify", public_text="Which address, and where?")
    fields = embed_fields(advice, user_name="blue", channel_name="ops-blue",
                          question="q", awaiting=False, hitl_enabled=True)
    text = " ".join(f"{n} {v}" for n, v, _ in fields)
    assert "not held" in text.lower()
    assert "clarify" in text.lower()


def test_a_held_ruling_does_not_carry_the_not_held_note():
    from rangecontrol.advisor.models import Advice, Ruling
    from rangecontrol.bot.whitecell import embed_fields

    ruling = Ruling(verdict="DENIED", public_response="No.", internal_reason="r",
                    impacted=(), confidence="high")
    fields = embed_fields(Advice(kind="ruling", public_text="No.", ruling=ruling),
                          user_name="blue", channel_name="ops-blue", question="q",
                          awaiting=True, hitl_enabled=True)
    text = " ".join(f"{n} {v}" for n, v, _ in fields)
    assert "not held" not in text.lower()


def test_nothing_is_explained_when_hitl_is_off():
    """With the mode off, "not held" would be noise on every single question."""
    from rangecontrol.advisor.models import Advice
    from rangecontrol.bot.whitecell import embed_fields

    fields = embed_fields(Advice(kind="clarify", public_text="?"),
                          user_name="blue", channel_name="ops-blue", question="q",
                          awaiting=False, hitl_enabled=False)
    text = " ".join(f"{n} {v}" for n, v, _ in fields)
    assert "not held" not in text.lower()
