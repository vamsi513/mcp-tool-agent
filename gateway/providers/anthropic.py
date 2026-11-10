"""Anthropic provider: the Messages wire format, over httpx.

The mapping itself lives in gateway.translate as pure functions; this module
only does the HTTP and unpacks the reply.
"""

from __future__ import annotations

import json
import os
from collections.abc import Generator
from typing import Any

import httpx

from gateway.config import DEFAULT_MAX_TOKENS
from gateway.errors import upstream_failure
from gateway.providers.base import Completion
from gateway.translate import (
    finish_reason_from_stop_reason,
    text_from_anthropic_content,
    to_anthropic_request,
)

DEFAULT_BASE_URL = "https://api.anthropic.com/v1"
API_VERSION = "2023-06-01"
TIMEOUT_SECONDS = 120


def http_client(**kwargs: Any) -> httpx.Client:
    """The HTTP client this module uses, in one place. Tests swap it out."""
    return httpx.Client(**kwargs)


def base_url() -> str:
    return (os.getenv("ANTHROPIC_BASE_URL", "").strip() or DEFAULT_BASE_URL).rstrip("/")


def _headers() -> dict[str, str]:
    key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    headers = {"Content-Type": "application/json", "anthropic-version": API_VERSION}
    if key:
        headers["x-api-key"] = key
    return headers


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, default_max_tokens: int = DEFAULT_MAX_TOKENS) -> None:
        self.default_max_tokens = default_max_tokens

    def complete(
        self,
        *,
        model: str,
        messages: list[dict],
        temperature: float | None = None,
        max_tokens: int | None = None,
        extra_system: str = "",
    ) -> Completion:
        body = to_anthropic_request(
            messages,
            model=model,
            max_tokens=max_tokens,
            default_max_tokens=self.default_max_tokens,
            temperature=temperature,
            extra_system=extra_system,
        )
        try:
            with http_client(timeout=TIMEOUT_SECONDS) as client:
                response = client.post(
                    f"{base_url()}/messages", headers=_headers(), json=body
                )
        except httpx.RequestError as exc:
            raise upstream_failure(self.name, exc.__class__.__name__) from exc

        if response.status_code >= 400:
            raise upstream_failure(self.name, _message(response))

        payload = response.json()
        usage = payload.get("usage") or {}
        return Completion(
            text=text_from_anthropic_content(payload.get("content")),
            finish_reason=finish_reason_from_stop_reason(payload.get("stop_reason")),
            input_tokens=int(usage.get("input_tokens") or 0),
            output_tokens=int(usage.get("output_tokens") or 0),
            model=payload.get("model") or model,
        )

    def stream(
        self,
        *,
        model: str,
        messages: list[dict],
        temperature: float | None = None,
        max_tokens: int | None = None,
        extra_system: str = "",
    ) -> Generator[str, None, Completion]:
        body = to_anthropic_request(
            messages,
            model=model,
            max_tokens=max_tokens,
            default_max_tokens=self.default_max_tokens,
            temperature=temperature,
            extra_system=extra_system,
            stream=True,
        )
        text = ""
        stop_reason: Any = None
        input_tokens = 0
        output_tokens = 0
        try:
            with http_client(timeout=TIMEOUT_SECONDS) as client, client.stream(
                "POST", f"{base_url()}/messages", headers=_headers(), json=body
            ) as response:
                if response.status_code >= 400:
                    response.read()
                    raise upstream_failure(self.name, _message(response))
                for line in response.iter_lines():
                    event = _event_from_line(line)
                    if event is None:
                        continue
                    kind = event.get("type")
                    if kind == "content_block_delta":
                        delta = _text_delta(event)
                        if delta:
                            text += delta
                            yield delta
                    elif kind == "message_start":
                        usage = (event.get("message") or {}).get("usage") or {}
                        input_tokens = int(usage.get("input_tokens") or 0)
                        output_tokens = int(usage.get("output_tokens") or 0)
                    elif kind == "message_delta":
                        stop_reason = (event.get("delta") or {}).get("stop_reason", stop_reason)
                        usage = event.get("usage") or {}
                        if usage.get("output_tokens") is not None:
                            output_tokens = int(usage["output_tokens"])
        except httpx.RequestError as exc:
            raise upstream_failure(self.name, exc.__class__.__name__) from exc

        return Completion(
            text=text,
            finish_reason=finish_reason_from_stop_reason(stop_reason),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            model=model,
        )


def _event_from_line(line: str) -> dict | None:
    """The JSON of one SSE data line, or None for anything else."""
    if not line.startswith("data:"):
        return None
    data = line[len("data:") :].strip()
    if not data:
        return None
    try:
        event = json.loads(data)
    except ValueError:
        return None
    return event if isinstance(event, dict) else None


def _text_delta(event: dict) -> str:
    """Text out of a content_block_delta. Thinking blocks are skipped."""
    delta = event.get("delta") or {}
    if delta.get("type") not in (None, "text_delta"):
        return ""
    text = delta.get("text")
    return text if isinstance(text, str) else ""


def _message(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return f"HTTP {response.status_code}"
    error = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(error, dict) and isinstance(error.get("message"), str):
        return f"HTTP {response.status_code}: {error['message'][:200]}"
    return f"HTTP {response.status_code}"
