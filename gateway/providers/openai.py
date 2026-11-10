"""OpenAI provider: the chat completions wire format, over httpx.

Deliberately not the openai SDK. The gateway's job is to translate wire
formats, so the request and response bodies stay explicit and testable, and
nothing here depends on an SDK's own client plumbing.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from typing import Any

import httpx

from gateway.errors import upstream_failure
from gateway.providers.base import Completion
from gateway.translate import content_to_text

DEFAULT_BASE_URL = "https://api.openai.com/v1"
TIMEOUT_SECONDS = 120


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
        body["max_tokens"] = max_tokens
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
    ) -> Iterator[str]:
        body = _body(model, messages, temperature, max_tokens, extra_system, stream=True)
        try:
            with http_client(timeout=TIMEOUT_SECONDS) as client, client.stream(
                "POST", f"{base_url()}/chat/completions", headers=_headers(), json=body
            ) as response:
                if response.status_code >= 400:
                    response.read()
                    raise upstream_failure(self.name, _message(response))
                for line in response.iter_lines():
                    delta = _delta_from_line(line)
                    if delta:
                        yield delta
        except httpx.RequestError as exc:
            raise upstream_failure(self.name, exc.__class__.__name__) from exc


def _delta_from_line(line: str) -> str:
    if not line.startswith("data:"):
        return ""
    data = line[len("data:") :].strip()
    if not data or data == "[DONE]":
        return ""
    try:
        event = json.loads(data)
    except ValueError:
        return ""
    choices = event.get("choices") or []
    if not choices:
        return ""
    return content_to_text((choices[0].get("delta") or {}).get("content"))


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
