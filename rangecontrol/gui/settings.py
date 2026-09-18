"""Form field definitions and validation.

Kept separate from the widgets so every rule here is testable without a
display, and so the window cannot drift from what load_config actually
accepts.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from rangecontrol.config import DEFAULT_MODELS, VALID_PROVIDERS


@dataclass(frozen=True)
class Field:
    key: str
    label: str
    multiline: bool = False
    secret: bool = False
    required: bool = False
    hint: str = ""
    # Which of the two form columns holds the field. None on every field
    # means "split the list evenly"; the real form places them explicitly
    # so the credentials sit together on the left and the Discord and
    # ruling settings on the right.
    column: int | None = None
    # Visible rows for a multiline field.
    lines: int = 4


FIELDS: tuple[Field, ...] = (
    Field("LLM_PROVIDER", "AI provider", required=True),
    Field("LLM_MODEL", "Model", hint="leave blank for the provider default"),
    Field("LLM_API_KEY", "API key", secret=True, required=True,
          hint="from console.anthropic.com or aistudio.google.com"),
    Field("DISCORD_BOT_TOKEN", "Discord bot token", secret=True, required=True,
          hint="Developer Portal -> your app -> Bot -> Reset Token"),
    Field("WHITE_CELL_CHANNEL_ID", "White cell channel ID", required=True,
          hint="right-click the channel -> Copy Channel ID", column=1),
    Field("ALLOWED_CHANNEL_IDS", "Allowed channels",
          hint="comma-separated channel IDs, not names; blank means every channel",
          column=1),
    Field("EXTRA_INSTRUCTIONS", "Extra instructions to the AI", multiline=True,
          hint="optional; exercise-specific guidance for rulings, e.g. windows, "
               "scope, or standing decisions. It cannot relax the "
               "non-disclosure rules.", column=1),
    Field("HUMAN_IN_THE_LOOP", "Human in the loop",
          hint="hold every ruling for white cell approval before the blue team sees it",
          column=1),
    # No hint by request: the label says what it is, and the box is only
    # editable while human in the loop is ticked, which says when it applies.
    Field("HITL_DENIED_TEXT", "Reply when the white cell denies", multiline=True,
          column=1, lines=3),
)

# Suggestions only. The combobox accepts any string: new models ship faster
# than a hardcoded list can track, and a locked list would date the app.
KNOWN_MODELS: dict[str, tuple[str, ...]] = {
    "anthropic": ("claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5-20251001"),
    "gemini": ("gemini-2.5-pro", "gemini-2.5-flash"),
}


def models_for(provider: str) -> tuple[str, ...]:
    return KNOWN_MODELS.get(provider.strip().lower(), ())


def missing_required(values: dict[str, str]) -> tuple[str, ...]:
    """Labels of required fields that are absent or blank, in form order."""
    return tuple(
        field.label
        for field in FIELDS
        if field.required and not values.get(field.key, "").strip()
    )


def is_configured(values: dict[str, str]) -> bool:
    return not missing_required(values)


def to_config_mapping(values: dict[str, str], range_dir: Path) -> dict[str, str]:
    """Build the mapping load_config consumes.

    RANGE_DIR is overwritten rather than passed through: in GUI mode the app
    home is the range, and honouring a stale .env entry would point the window
    at a directory the user cannot see from it.

    Unknown keys are passed through on purpose: settings not in the form
    (e.g., LLM_GATE_MODEL) may still be set in .env by hand and must reach
    load_config. Whitelisting would silently drop them.
    """
    mapping = {k: v for k, v in values.items() if v is not None}
    mapping["RANGE_DIR"] = str(range_dir)
    provider = mapping.get("LLM_PROVIDER", "").strip().lower()
    if provider in VALID_PROVIDERS and not mapping.get("LLM_MODEL", "").strip():
        mapping["LLM_MODEL"] = DEFAULT_MODELS[provider]
    return mapping
