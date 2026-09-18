"""Environment-backed configuration with fail-fast validation."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

VALID_PROVIDERS = ("anthropic", "gemini")

DEFAULT_MODELS = {
    "anthropic": "claude-opus-5",
    "gemini": "gemini-2.5-pro",
}

# Sent to the asker when the white cell denies a held ruling. Fixed text,
# never derived from the ruling, so a denial can carry nothing about what
# the change would have touched. The white cell may reword it in the GUI.
DEFAULT_HITL_DENIED_TEXT = (
    "That request has been denied to retain range integrity."
)

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off", ""}


class ConfigError(Exception):
    """Raised when configuration is missing or malformed."""


@dataclass(frozen=True)
class Config:
    discord_bot_token: str
    llm_provider: str
    llm_api_key: str
    llm_model: str
    llm_gate_model: str
    white_cell_channel_id: int
    allowed_channel_ids: tuple[int, ...]
    range_dir: Path
    human_in_the_loop: bool
    extra_instructions: str
    hitl_denied_text: str = DEFAULT_HITL_DENIED_TEXT


def _require(env: Mapping[str, str], name: str) -> str:
    value = env.get(name, "").strip()
    if not value:
        raise ConfigError(
            f"{name} is required but not set. "
            f"Add it to your .env file or export it before starting RangeControl."
        )
    return value


def _require_int(env: Mapping[str, str], name: str) -> int:
    raw = _require(env, name)
    try:
        return int(raw)
    except ValueError:
        raise ConfigError(
            f"{name} must be a numeric Discord channel ID."
        ) from None


def _parse_id_list(env: Mapping[str, str], name: str) -> tuple[int, ...]:
    raw = env.get(name, "").strip()
    if not raw:
        return ()
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    try:
        return tuple(int(p) for p in parts)
    except ValueError:
        raise ConfigError(
            f"{name} must be a comma-separated list of numeric Discord channel IDs."
        ) from None


def _parse_bool(env: Mapping[str, str], name: str) -> bool:
    """Parse a boolean setting, rejecting anything ambiguous.

    Defaulting an unrecognised value to False would silently disable human
    review for an operator who believed they had enabled it — the one failure
    mode this setting cannot have.
    """
    raw = env.get(name, "").strip().lower()
    if raw in _TRUE:
        return True
    if raw in _FALSE:
        return False
    raise ConfigError(
        f"{name} must be true or false; got '{raw}'."
    )


def load_config(env: Mapping[str, str] | None = None) -> Config:
    """Build a Config from a mapping, defaulting to the process environment."""
    env = os.environ if env is None else env

    provider = _require(env, "LLM_PROVIDER").lower()
    if provider not in VALID_PROVIDERS:
        raise ConfigError(
            f"LLM_PROVIDER must be one of {', '.join(VALID_PROVIDERS)}; got '{provider}'."
        )

    model = env.get("LLM_MODEL", "").strip() or DEFAULT_MODELS[provider]
    gate_model = env.get("LLM_GATE_MODEL", "").strip() or model

    return Config(
        discord_bot_token=_require(env, "DISCORD_BOT_TOKEN"),
        llm_provider=provider,
        llm_api_key=_require(env, "LLM_API_KEY"),
        llm_model=model,
        llm_gate_model=gate_model,
        white_cell_channel_id=_require_int(env, "WHITE_CELL_CHANNEL_ID"),
        allowed_channel_ids=_parse_id_list(env, "ALLOWED_CHANNEL_IDS"),
        range_dir=Path(env.get("RANGE_DIR", "").strip() or "."),
        human_in_the_loop=_parse_bool(env, "HUMAN_IN_THE_LOOP"),
        # Free text from the white cell. Never stripped of content, only
        # of surrounding whitespace, so wording is theirs alone.
        extra_instructions=env.get("EXTRA_INSTRUCTIONS", "").strip(),
        # Blank means "the built-in wording": an empty denial cannot be sent.
        hitl_denied_text=(
            env.get("HITL_DENIED_TEXT", "").strip() or DEFAULT_HITL_DENIED_TEXT
        ),
    )
