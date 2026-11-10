"""OpenAI provider: the chat completions wire format, over httpx.

Deliberately not the openai SDK. The gateway's job is to translate wire
formats, so the request and response bodies stay explicit and testable, and
nothing here depends on an SDK's own client plumbing.
"""

from __future__ import annotations

import json
import os
from collections.abc import Generator
from typing import Any

import httpx

from gateway.errors import upstream_failure
from gateway.providers.base import Completion
from gateway.translate import content_to_text

DEFAULT_BASE_URL = "https://api.openai.com/v1"
TIMEOUT_SECONDS = 120

# Model families that take max_completion_tokens on the chat completions
# endpoint rather than max_tokens. Matched on the start of the model name.
REASONING_MODEL_PREFIXES = ("gpt-5", "o1", "o3", "o4")


def is_reasoning_model(model: str) -> bool:
    """True when the upstream expects max_completion_tokens for this model."""
    return model.startswith(REASONING_MODEL_PREFIXES)


def token_limit_field(model: str) -> str:
    """The upstream field name for a caller's token limit."""
    return "max_completion_tokens" if is_reasoning_model(model) else "max_tokens"


def http_client(**kwargs: Any) -> httpx.Client:
    """The HTTP client this module uses, in one place.

    Tests swap this function out. Rebinding httpx.Client instead would change
    a global that other libraries inspect, which has broken this repository
    before.
    """
    return httpx.Client(**kwargs)


def base_url() -> str:
    return (os.getenv("OPENAI_BASE_URL", "").strip() or DEFAULT_BASE_URL).rstrip("/")


def _headers() -> dict[str, str]:
    key = os.getenv("OPENAI_API_KEY", "").strip()
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    return headers


def _body(
    model: str,
    messages: list[dict],
    temperature: float | None,
    max_tokens: int | None,
    extra_system: str,
    stream: bool,
) -> dict:
    turns = list(messages)
    if extra_system:
        # /v1/responses instructions become a leading system turn.
        turns = [{"role": "system", "content": extra_system}, *turns]

    body: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": m.get("role", "user"), "content": content_to_text(m.get("content"))}
            for m in turns
        ],
    }
    if temperature is not None:
        body["temperature"] = temperature
    if max_tokens is not None:
        # A caller's max_tokens, or a /v1/responses max_output_tokens, goes
        # upstream under whichever name the model family expects. Temperature
        # is passed through unchanged; some reasoning models accept only the
        # default and will refuse another value.
        body[token_limit_field(model)] = max_tokens
    if stream:
        body["stream"] = True
        body["stream_options"] = {"include_usage": True}
    return body


class OpenAIProvider:
    name = "openai"

    def complete(
        self,
        *,
        model: str,
        messages: list[dict],
        temperature: float | None = None,
        max_tokens: int | None = None,
        extra_system: str = "",
    ) -> Completion:
        body = _body(model, messages, temperature, max_tokens, extra_system, stream=False)
        try:
            with http_client(timeout=TIMEOUT_SECONDS) as client:
                response = client.post(
                    f"{base_url()}/chat/completions", headers=_headers(), json=body
                )
        except httpx.RequestError as exc:
            raise upstream_failure(self.name, exc.__class__.__name__) from exc

        if response.status_code >= 400:
            raise upstream_failure(self.name, _message(response))

        payload = response.json()
        choice = (payload.get("choices") or [{}])[0]
        usage = payload.get("usage") or {}
        return Completion(
            text=content_to_text((choice.get("message") or {}).get("content")),
            finish_reason=choice.get("finish_reason") or "stop",
            input_tokens=int(usage.get("prompt_tokens") or 0),
            output_tokens=int(usage.get("completion_tokens") or 0),
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
        body = _body(model, messages, temperature, max_tokens, extra_system, stream=True)
        text = ""
        finish_reason = "stop"
        prompt_tokens = 0
        completion_tokens = 0
        try:
            with http_client(timeout=TIMEOUT_SECONDS) as client, client.stream(
                "POST", f"{base_url()}/chat/completions", headers=_headers(), json=body
            ) as response:
                if response.status_code >= 400:
                    response.read()
                    raise upstream_failure(self.name, _message(response))
                for line in response.iter_lines():
                    event = _event_from_line(line)
                    if event is None:
                        continue
                    choices = event.get("choices") or []
                    if choices:
                        delta = content_to_text((choices[0].get("delta") or {}).get("content"))
                        if choices[0].get("finish_reason"):
                            finish_reason = choices[0]["finish_reason"]
                        if delta:
                            text += delta
                            yield delta
                    usage = event.get("usage")
                    if isinstance(usage, dict):
                        prompt_tokens = int(usage.get("prompt_tokens") or 0)
                        completion_tokens = int(usage.get("completion_tokens") or 0)
        except httpx.RequestError as exc:
            raise upstream_failure(self.name, exc.__class__.__name__) from exc

        return Completion(
            text=text,
            finish_reason=finish_reason,
            input_tokens=prompt_tokens,
            output_tokens=completion_tokens,
            model=model,
        )


def _event_from_line(line: str) -> dict | None:
    """The JSON of one SSE data line, or None for keep-alives and [DONE]."""
    if not line.startswith("data:"):
        return None
    data = line[len("data:") :].strip()
    if not data or data == "[DONE]":
        return None
    try:
        event = json.loads(data)
    except ValueError:
        return None
    return event if isinstance(event, dict) else None


def _message(response: httpx.Response) -> str:
    """A short upstream message, without echoing a request or a credential."""
    try:
        payload = response.json()
    except ValueError:
        return f"HTTP {response.status_code}"
    error = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(error, dict) and isinstance(error.get("message"), str):
        return f"HTTP {response.status_code}: {error['message'][:200]}"
    return f"HTTP {response.status_code}"
