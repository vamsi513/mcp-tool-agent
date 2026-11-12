"""Ollama provider: a self-hosted model behind an OpenAI-compatible API.

Ollama serves `/v1/chat/completions` in OpenAI's shape, so this is the same
translation the OpenAI provider does, with three differences that matter:

* The endpoint comes from the gateway config rather than the environment.
  There is no credential to keep out of the file, and the address is part of
  how this deployment is wired rather than a secret.
* No key is sent. Ollama does not authenticate by default; a deployment that
  puts one behind a proxy needs that proxy to handle it.
* A local server that is not running is the ordinary case, not an edge case,
  so a refused connection is reported as such rather than as a bare failure.

Text in, text out. Tools, images, embeddings and batching are not
implemented here, and nothing in this file says anything about how fast a
given model will run or on what hardware.
"""

from __future__ import annotations

import json
from collections.abc import Generator
from typing import Any

import httpx

from gateway.config import DEFAULT_OLLAMA_BASE_URL, DEFAULT_OLLAMA_TIMEOUT_SECONDS
from gateway.errors import upstream_failure
from gateway.providers.base import Completion
from gateway.translate import content_to_text


def http_client(**kwargs: Any) -> httpx.Client:
    """The HTTP client this module uses, in one place.

    Tests swap this function out. Rebinding httpx.Client instead would change
    a global that other libraries inspect, which has broken this repository
    before.
    """
    return httpx.Client(**kwargs)


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
        # Open models are not OpenAI's reasoning families, so the caller's
        # limit always goes upstream under max_tokens.
        body["max_tokens"] = max_tokens
    if stream:
        body["stream"] = True
        body["stream_options"] = {"include_usage": True}
    return body


class OllamaProvider:
    """Talks to one Ollama server, wherever the config says it is."""

    name = "ollama"

    def __init__(
        self,
        base_url: str = DEFAULT_OLLAMA_BASE_URL,
        timeout_seconds: float = DEFAULT_OLLAMA_TIMEOUT_SECONDS,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds

    @property
    def _endpoint(self) -> str:
        return f"{self.base_url}/chat/completions"

    @staticmethod
    def _headers() -> dict[str, str]:
        # No Authorization header: Ollama takes no key, and inventing one
        # here would only put a value somewhere it does not belong.
        return {"Content-Type": "application/json"}

    def _failure(self, exc: httpx.RequestError) -> Exception:
        """Turn a transport error into the gateway's own 502 shape.

        A local server that is simply not running is the common case, so it
        says so rather than naming an exception class nobody can act on.
        """
        if isinstance(exc, httpx.ConnectError):
            return upstream_failure(
                self.name,
                f"could not reach an Ollama server at {self.base_url}; "
                "is it running?",
            )
        if isinstance(exc, httpx.TimeoutException):
            return upstream_failure(
                self.name,
                f"the Ollama server at {self.base_url} did not answer within "
                f"{self.timeout_seconds:g}s",
            )
        return upstream_failure(self.name, exc.__class__.__name__)

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
            with http_client(timeout=self.timeout_seconds) as client:
                response = client.post(self._endpoint, headers=self._headers(), json=body)
        except httpx.RequestError as exc:
            raise self._failure(exc) from exc

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
            with http_client(timeout=self.timeout_seconds) as client, client.stream(
                "POST", self._endpoint, headers=self._headers(), json=body
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
                        delta = content_to_text(
                            (choices[0].get("delta") or {}).get("content")
                        )
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
            raise self._failure(exc) from exc

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
    """A short upstream message, without echoing a request back."""
    try:
        payload = response.json()
    except ValueError:
        return f"HTTP {response.status_code}"
    error = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(error, dict) and isinstance(error.get("message"), str):
        return f"HTTP {response.status_code}: {error['message'][:200]}"
    # Ollama answers some failures with a bare {"error": "..."} string.
    if isinstance(error, str):
        return f"HTTP {response.status_code}: {error[:200]}"
    return f"HTTP {response.status_code}"
