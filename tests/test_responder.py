from rangecontrol.advisor.models import Advice, GateResult, GateVerdict, Ruling
from rangecontrol.bot.responder import DISCORD_LIMIT, format_public
from rangecontrol.bot.whitecell import EMBED_LIMIT, build_embed, embed_fields

PUBLIC = "No. That is waiting on the change board."


def ruling(verdict="DENIED", confidence="high", public=PUBLIC):
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
        "public_text": PUBLIC,
        "ruling": ruling(),
        "gate": GateResult(verdict=GateVerdict.CHANGE_REQUEST, reason="r"),
    }
    payload.update(overrides)
    return Advice(**payload)


def fields_of(item, **kwargs):
    kwargs = {"user_name": "u", "channel_name": "c", "question": "q", **kwargs}
    return embed_fields(item, **kwargs)


def test_public_reply_is_the_text_verbatim_with_nothing_internal():
    """No framing, label, or confidence badge: anything added here could
    correlate denials, and nothing internal may ride along."""
    approved = "Approved. Watch your own management path."
    cases = [
        advice(ruling=ruling(confidence="low")),
        advice(public_text=approved, ruling=ruling(verdict="APPROVED", public=approved)),
        advice(kind="deflection", public_text="D", ruling=None),
        advice(kind="error", public_text="E", ruling=None),
    ]
    for case in cases:
        rendered = format_public(case)
        assert rendered == case.public_text
        for internal in ("MSEL-01", "DC-VULCAN", "confidence"):
            assert internal.lower() not in rendered.lower()


def test_public_reply_never_exceeds_the_discord_limit():
    long = advice(public_text="x" * (DISCORD_LIMIT + 500), ruling=ruling(public="x" * 3000))
    assert len(format_public(long)) <= DISCORD_LIMIT


def test_white_cell_embed_carries_what_the_public_reply_withholds():
    fields = {
        name: value
        for name, value, _ in fields_of(
            advice(ruling=ruling(confidence="low")),
            user_name="blue-lead", channel_name="ops", question="block it",
        )
    }
    joined = " ".join(fields.values())
    for expected in ("DC-VULCAN", "MSEL-01", "blue-lead", "block it"):
        assert expected in joined
    assert fields["Sent to blue team"] == PUBLIC
    assert "LOW" in fields["Confidence"].upper()

    error = Advice(kind="error", public_text="can't process", error="LLMError('down')")
    assert any("LLMError" in value for _, value, _ in fields_of(error))


def test_the_embed_states_whether_a_reviewer_must_act():
    """With HITL on, an embed with no prompt reads as the mode being broken
    unless it says why nothing is held; with HITL off that note is noise."""
    held = build_embed(advice(), user_name="u", channel_name="c", question="q",
                       awaiting=True, hitl_enabled=True)
    held_names = [field.name for field in held.fields]
    assert held.title.startswith("⏳ AWAITING REVIEW")
    assert "Awaiting review" in held_names
    assert "Not held" not in held_names

    clarify = Advice(kind="clarify", public_text="Which address, and where?")
    not_held = fields_of(clarify, hitl_enabled=True)
    text = " ".join(f"{name} {value}" for name, value, _ in not_held).lower()
    assert "not held" in text
    assert "clarify" in text
    assert "Awaiting review" not in [name for name, _, _ in not_held]

    off = build_embed(clarify, user_name="u", channel_name="c", question="q")
    assert not off.title.startswith("⏳")
    assert {"Awaiting review", "Not held"}.isdisjoint(f.name for f in off.fields)


def test_white_cell_sees_the_withheld_draft_and_why():
    """The reviewer has to be able to tell the prompt is drifting, so the
    replacement is never silent."""
    draft = "Approved. Nothing in our inventory matches that address."
    screened = advice(
        public_text="Approved — go ahead.",
        ruling=ruling(verdict="APPROVED", public=draft),
        screen_reason="claims about what exists: inventory",
    )
    fields = {name: value for name, value, _ in fields_of(screened)}
    assert fields["Sent to blue team"] == "Approved — go ahead."
    assert draft in " ".join(fields.values())
    assert "inventory" in fields["Screened"]

    assert "Screened" not in [name for name, _, _ in fields_of(advice())]


def test_a_worst_case_screened_ruling_embed_fits_discord():
    """Discord rejects an embed over 6000 characters in total. The white cell
    post would fail, and with human in the loop on the asker is then answered
    with no review at all, so every field and the total must fit."""
    long = "x" * 5000
    worst = Advice(
        kind="ruling",
        public_text=long,
        ruling=Ruling(
            verdict="APPROVED",
            public_response=long,
            internal_reason=long,
            impacted=(long,),
            confidence="low",
        ),
        gate=GateResult(verdict=GateVerdict.CHANGE_REQUEST, reason="r"),
        screen_reason=long,
    )
    embed = build_embed(
        worst, user_name=long, channel_name=long, question=long,
        awaiting=True, hitl_enabled=True,
    )
    for field in embed.fields:
        assert len(field.name) <= 256
        assert len(field.value) <= 1024
    total = len(embed.title) + sum(len(f.name) + len(f.value) for f in embed.fields)
    assert total <= EMBED_LIMIT
