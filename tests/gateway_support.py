"""Shared helpers for the gateway tests.

Every test here uses FakeProvider, so nothing reaches a provider. Real-provider
behaviour is tested by feeding recorded wire bodies through an httpx
MockTransport installed on each provider module's own `http_client` factory,
never by rebinding the global httpx.Client.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

import metering
from gateway.app import create_app
from gateway.config import GatewayConfig, KeyConfig, Route, SessionConfig
from gateway.providers.fake import FakeProvider
from gateway.sessions import SqliteSessionStore

KEY = "test-key"
KEY_ID = "tester"
HEADERS = {"Authorization": f"Bearer {KEY}"}

# Prices chosen so a handful of tokens costs a visible amount, which keeps the
# budget tests readable. The shipped prices.example.yaml is all zeros, so a
# budget could never be exceeded with it.
TEST_PRICES = {
    "currency": "USD",
    "models": {
        "fake-1": {"input_per_million": 1_000_000.0, "output_per_million": 1_000_000.0},
        "gpt-4o-mini": {"input_per_million": 1_000_000.0, "output_per_million": 1_000_000.0},
        "claude-3-5-sonnet": {
            "input_per_million": 1_000_000.0,
            "output_per_million": 1_000_000.0,
        },
    },
}


def prices() -> metering.PriceTable:
    """One unit of cost per token, so arithmetic in a test is obvious."""
    return metering.parse_prices(TEST_PRICES)


def config(
    budget: float | None = None,
    ttl_seconds: int = 3600,
    backend: str = "sqlite",
    routes: tuple[Route, ...] | None = None,
) -> GatewayConfig:
    return GatewayConfig(
        routes=routes
        or (
            Route("fake-", "fake"),
            Route("gpt-", "openai"),
            Route("claude-", "anthropic"),
        ),
        keys={KEY: KeyConfig(id=KEY_ID, secret=KEY, monthly_budget_usd=budget)},
        sessions=SessionConfig(backend=backend, ttl_seconds=ttl_seconds),
        default_max_tokens=1024,
    )


def build(
    db,
    provider: Any = None,
    store: Any = None,
    budget: float | None = None,
    ttl_seconds: int = 3600,
    routes: tuple[Route, ...] | None = None,
    providers: dict | None = None,
):
    """An app wired to a temp database and a fake provider."""
    fake = provider if provider is not None else FakeProvider(reply="fake reply")
    app = create_app(
        config(budget=budget, ttl_seconds=ttl_seconds, routes=routes),
        providers if providers is not None else {"fake": fake},
        prices(),
        db,
        store or SqliteSessionStore(db),
    )
    return app, fake


def sse_events(response) -> list[dict]:
    """Parse an SSE body into {event, data} dicts, skipping [DONE]."""
    events: list[dict] = []
    event_name: str | None = None
    for line in response.text.splitlines():
        if line.startswith("event:"):
            event_name = line[len("event:") :].strip()
        elif line.startswith("data:"):
            payload = line[len("data:") :].strip()
            if payload == "[DONE]":
                events.append({"event": event_name, "done": True})
            else:
                events.append({"event": event_name, "data": json.loads(payload)})
            event_name = None
    return events


def mock_provider_transport(monkeypatch, module, handler) -> list[dict]:
    """Install a MockTransport on a provider module's own client factory.

    Patches `module.http_client`, never httpx.Client, which is a global that
    other libraries inspect. Going through monkeypatch means the real factory
    is restored when the test ends, so the patch cannot leak into another
    test. Returns the list of request bodies the module sent.
    """
    sent: list[dict] = []

    def recording(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content or b"{}"))
        return handler(request)

    def factory(**kwargs):
        kwargs["transport"] = httpx.MockTransport(recording)
        return httpx.Client(**kwargs)

    monkeypatch.setattr(module, "http_client", factory)
    return sent


def openai_chat_body(text: str = "hi from openai", finish: str = "stop") -> dict:
    return {
        "id": "chatcmpl-upstream",
        "object": "chat.completion",
        "model": "gpt-4o-mini",
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": text},
             "finish_reason": finish}
        ],
        "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
    }


def anthropic_message_body(
    text: str = "hi from anthropic", stop_reason: str = "end_turn"
) -> dict:
    return {
        "id": "msg_upstream",
        "type": "message",
        "role": "assistant",
        "model": "claude-3-5-sonnet",
        "content": [{"type": "text", "text": text}],
        "stop_reason": stop_reason,
        "usage": {"input_tokens": 13, "output_tokens": 5},
    }
