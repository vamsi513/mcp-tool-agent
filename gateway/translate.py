"""Translation between the OpenAI shapes and the Anthropic Messages shape.

Pure functions over plain dicts, so every mapping case can be tested without
an app, a client or a provider.

The four rules the gateway implements:

* `system` messages and `/v1/responses` `instructions` are hoisted to
  Anthropic's top-level `system` field, in order, joined by blank lines.
* Consecutive turns with the same role are merged, because Anthropic requires
  user and assistant to alternate.
* `max_tokens` is required by Anthropic, so a configured default is supplied
  when the request omits it.
* `stop_reason` maps back to an OpenAI `finish_reason`: `end_turn` to `stop`,
  `max_tokens` to `length`.
"""

from __future__ import annotations

from typing import Any

# Anthropic stop_reason -> OpenAI finish_reason. Anything unlisted falls back
# to "stop" rather than inventing a value.
STOP_REASON_TO_FINISH_REASON = {
    "end_turn": "stop",
    "max_tokens": "length",
    "stop_sequence": "stop",
    "tool_use": "tool_calls",
    "pause_turn": "stop",
    "refusal": "content_filter",
}

MERGE_SEPARATOR = "\n\n"


def content_to_text(content: Any) -> str:
    """Flatten a message's content to text.

    A string passes through. A list of content parts keeps the text parts in
    order; anything else (an image, a tool result) is dropped here, and the
    caller is responsible for having rejected it already.
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                if isinstance(item.get("text"), str):
                    parts.append(item["text"])
        return "".join(parts)
    return str(content)


def split_system(messages: list[dict]) -> tuple[str, list[dict]]:
    """Pull system turns out of a message list.

    Returns the joined system text and the remaining turns, in order.
    """
    system_parts: list[str] = []
    rest: list[dict] = []
    for message in messages:
        role = message.get("role")
        if role in ("system", "developer"):
            text = content_to_text(message.get("content"))
            if text:
                system_parts.append(text)
        else:
            rest.append(message)
    return MERGE_SEPARATOR.join(system_parts), rest


def merge_consecutive_roles(messages: list[dict]) -> list[dict]:
    """Merge neighbouring turns that share a role, so roles alternate.

    Anthropic rejects two user turns in a row. Their text is joined with a
    blank line, which keeps them readable as separate statements.
    """
    merged: list[dict] = []
    for message in messages:
        role = message.get("role")
        text = content_to_text(message.get("content"))
        if merged and merged[-1]["role"] == role:
            previous = merged[-1]["content"]
            merged[-1]["content"] = (
                MERGE_SEPARATOR.join([previous, text]) if previous and text else previous or text
            )
            continue
        merged.append({"role": role, "content": text})
    return merged


def to_anthropic_request(
    messages: list[dict],
    model: str,
    max_tokens: int | None,
    default_max_tokens: int,
    temperature: float | None = None,
    extra_system: str = "",
    stream: bool = False,
) -> dict:
    """Build an Anthropic Messages request from OpenAI-shaped messages."""
    system_text, turns = split_system(messages)
    if extra_system:
        system_text = (
            MERGE_SEPARATOR.join([extra_system, system_text]) if system_text else extra_system
        )

    body: dict[str, Any] = {
        "model": model,
        "messages": merge_consecutive_roles(turns),
        # Anthropic requires this field, so a missing value becomes the
        # configured default rather than an error.
        "max_tokens": int(max_tokens) if max_tokens else int(default_max_tokens),
    }
    if system_text:
        body["system"] = system_text
    if temperature is not None:
        body["temperature"] = temperature
    if stream:
        body["stream"] = True
    return body


def finish_reason_from_stop_reason(stop_reason: Any) -> str:
    if isinstance(stop_reason, str) and stop_reason in STOP_REASON_TO_FINISH_REASON:
        return STOP_REASON_TO_FINISH_REASON[stop_reason]
    return "stop"


def text_from_anthropic_content(content: Any) -> str:
    """Join the text blocks of an Anthropic response's content array."""
    if not isinstance(content, list):
        return content_to_text(content)
    parts = [
        block["text"]
        for block in content
        if isinstance(block, dict)
        and block.get("type") == "text"
        and isinstance(block.get("text"), str)
    ]
    return "".join(parts)


def responses_input_to_messages(value: Any) -> list[dict]:
    """Normalise a /v1/responses `input` into OpenAI-shaped messages.

    Accepts a bare string, or a list of items each carrying a role and
    content. A list entry without a role is treated as a user turn.
    """
    if value is None:
        return []
    if isinstance(value, str):
        return [{"role": "user", "content": value}]
    if isinstance(value, dict):
        value = [value]
    if not isinstance(value, list):
        return [{"role": "user", "content": str(value)}]

    messages: list[dict] = []
    for item in value:
        if isinstance(item, str):
            messages.append({"role": "user", "content": item})
            continue
        if not isinstance(item, dict):
            messages.append({"role": "user", "content": str(item)})
            continue
        role = item.get("role") or "user"
        messages.append({"role": role, "content": content_to_text(item.get("content"))})
    return messages
