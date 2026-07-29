"""Ask a provider which models the configured key can actually reach.

A hardcoded suggestion list goes stale the day a new model ships, and a stale
list is worse than none: an operator picks a name that no longer exists and
finds out mid-exercise. This asks the vendor instead.

Like every other vendor call in this project, the SDK imports live here in
``rangecontrol/llm/`` and nowhere else. This is a metadata request, not a
generation — it consumes no tokens.
"""

from __future__ import annotations

from rangecontrol.config import Config
from rangecontrol.llm.base import LLMError

# Names a provider returns that are not usable for adjudication. Embedding and
# image models would otherwise appear in the dropdown as if they were choices.
_EXCLUDED_FRAGMENTS = ("embedding", "embed", "aqa", "imagen", "veo", "tts")


def _usable(name: str) -> bool:
    lowered = name.lower()
    return bool(name) and not any(bad in lowered for bad in _EXCLUDED_FRAGMENTS)


def list_models(config: Config, client=None) -> tuple[str, ...]:
    """Return the model ids ``config``'s key can use, newest-looking first.

    Raises ``LLMError`` on any transport or shape failure, matching the
    contract every other call in this package follows — the caller shows the
    message rather than guessing why the list is empty.
    """
    provider = config.llm_provider
    if provider == "anthropic":
        names = _list_anthropic(config, client)
    elif provider == "gemini":
        names = _list_gemini(config, client)
    else:
        raise LLMError(f"Cannot list models for provider '{provider}'.")

    usable = sorted({name for name in names if _usable(name)}, reverse=True)
    if not usable:
        raise LLMError(
            "The provider returned no usable models for this API key."
        )
    return tuple(usable)


def _list_anthropic(config: Config, client) -> list[str]:
    if client is None:
        from anthropic import Anthropic

        client = Anthropic(api_key=config.llm_api_key)
    try:
        page = client.models.list(limit=100)
        return [str(getattr(model, "id", "")) for model in page]
    except Exception as exc:  # noqa: BLE001 - wrap every SDK failure uniformly
        raise LLMError(
            f"Could not list Anthropic models: {type(exc).__name__}"
        ) from exc


def _list_gemini(config: Config, client) -> list[str]:
    if client is None:
        from google import genai

        client = genai.Client(api_key=config.llm_api_key)
    try:
        names = []
        for model in client.models.list():
            # Gemini reports "models/gemini-2.5-pro"; the API takes the bare id.
            raw = str(getattr(model, "name", "") or "")
            name = raw.split("/", 1)[1] if "/" in raw else raw
            actions = getattr(model, "supported_actions", None)
            # Only models that can answer a question are useful here. An absent
            # list means the SDK did not report it, so keep the model rather
            # than silently dropping a usable one.
            if actions and "generateContent" not in actions:
                continue
            names.append(name)
        return names
    except Exception as exc:  # noqa: BLE001 - wrap every SDK failure uniformly
        raise LLMError(
            f"Could not list Gemini models: {type(exc).__name__}"
        ) from exc
