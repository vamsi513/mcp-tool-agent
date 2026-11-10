"""Both real providers, fed recorded wire bodies through a MockTransport.

No network: each provider module's own `http_client` factory is swapped, never
the global httpx.Client.
"""

import json

import httpx
import pytest

from gateway.providers import anthropic as anthropic_provider
from gateway.providers import openai as openai_provider
from gateway.providers.base import StreamCollector
from tests.gateway_support import (
    anthropic_message_body,
    mock_provider_transport,
    openai_chat_body,
)


@pytest.fixture
def openai_calls(monkeypatch):
    """Install a handler on the OpenAI provider; returns the bodies it sent."""

    def install(handler):
        return mock_provider_transport(monkeypatch, openai_provider, handler)

    return install


@pytest.fixture
def anthropic_calls(monkeypatch):
    """Install a handler on the Anthropic provider; returns the bodies sent."""

    def install(handler):
        return mock_provider_transport(monkeypatch, anthropic_provider, handler)

    return install


# --- OpenAI -----------------------------------------------------------


def test_openai_complete_unpacks_the_response(openai_calls):
    sent = openai_calls(lambda request: httpx.Response(200, json=openai_chat_body()))
    result = openai_provider.OpenAIProvider().complete(
        model="gpt-4o-mini", messages=[{"role": "user", "content": "hi"}]
    )

    assert result.text == "hi from openai"
    assert result.finish_reason == "stop"
    assert result.input_tokens == 11
    assert result.output_tokens == 7
    assert result.total_tokens == 18
    assert sent[0]["model"] == "gpt-4o-mini"
    assert sent[0]["messages"] == [{"role": "user", "content": "hi"}]


def test_openai_instructions_become_a_leading_system_turn(openai_calls):
    sent = openai_calls(lambda request: httpx.Response(200, json=openai_chat_body()))
    openai_provider.OpenAIProvider().complete(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": "hi"}],
        extra_system="be terse",
    )
    assert sent[0]["messages"][0] == {"role": "system", "content": "be terse"}


def test_openai_error_status_becomes_a_502(openai_calls):
    openai_calls(
        lambda request: httpx.Response(500, json={"error": {"message": "upstream boom"}})
    )
    with pytest.raises(Exception) as excinfo:
        openai_provider.OpenAIProvider().complete(
            model="gpt-4o-mini", messages=[{"role": "user", "content": "hi"}]
        )
    assert excinfo.value.status_code == 502
    assert "upstream boom" in json.dumps(excinfo.value.detail)


def test_openai_stream_yields_deltas_and_returns_usage(openai_calls):
    chunks = [
        {"choices": [{"index": 0, "delta": {"content": "he"}}]},
        {"choices": [{"index": 0, "delta": {"content": "llo"}}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "length"}]},
        {"choices": [], "usage": {"prompt_tokens": 4, "completion_tokens": 2}},
    ]
    body = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
    openai_calls(lambda request: httpx.Response(200, text=body))

    collector = StreamCollector(
        openai_provider.OpenAIProvider().stream(
            model="gpt-4o-mini", messages=[{"role": "user", "content": "hi"}]
        )
    )
    assert list(collector) == ["he", "llo"]
    assert collector.text == "hello"
    assert collector.usage.input_tokens == 4
    assert collector.usage.output_tokens == 2
    assert collector.usage.finish_reason == "length"


def test_openai_stream_asks_for_usage(openai_calls):
    sent = openai_calls(lambda request: httpx.Response(200, text="data: [DONE]\n\n"))
    list(
        StreamCollector(
            openai_provider.OpenAIProvider().stream(
                model="gpt-4o-mini", messages=[{"role": "user", "content": "hi"}]
            )
        )
    )
    assert sent[0]["stream"] is True
    assert sent[0]["stream_options"] == {"include_usage": True}


# --- Anthropic --------------------------------------------------------


def test_anthropic_complete_unpacks_and_maps(anthropic_calls):
    sent = anthropic_calls(
        lambda request: httpx.Response(200, json=anthropic_message_body())
    )
    result = anthropic_provider.AnthropicProvider(default_max_tokens=256).complete(
        model="claude-3-5-sonnet",
        messages=[{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}],
    )

    assert result.text == "hi from anthropic"
    assert result.finish_reason == "stop"
    assert result.input_tokens == 13
    assert result.output_tokens == 5
    # The mapping rules, visible on the wire.
    assert sent[0]["system"] == "sys"
    assert sent[0]["max_tokens"] == 256
    assert all(m["role"] != "system" for m in sent[0]["messages"])


def test_anthropic_max_tokens_stop_reason_becomes_length(anthropic_calls):
    anthropic_calls(
        lambda request: httpx.Response(
            200, json=anthropic_message_body(stop_reason="max_tokens")
        )
    )
    result = anthropic_provider.AnthropicProvider().complete(
        model="claude-3-5-sonnet", messages=[{"role": "user", "content": "hi"}]
    )
    assert result.finish_reason == "length"


def test_anthropic_merges_consecutive_turns_on_the_wire(anthropic_calls):
    sent = anthropic_calls(
        lambda request: httpx.Response(200, json=anthropic_message_body())
    )
    anthropic_provider.AnthropicProvider().complete(
        model="claude-3-5-sonnet",
        messages=[
            {"role": "user", "content": "a"},
            {"role": "user", "content": "b"},
            {"role": "assistant", "content": "ok"},
        ],
    )
    assert sent[0]["messages"] == [
        {"role": "user", "content": "a\n\nb"},
        {"role": "assistant", "content": "ok"},
    ]


def test_anthropic_error_status_becomes_a_502(anthropic_calls):
    anthropic_calls(
        lambda request: httpx.Response(429, json={"error": {"message": "slow down"}})
    )
    with pytest.raises(Exception) as excinfo:
        anthropic_provider.AnthropicProvider().complete(
            model="claude-3-5-sonnet", messages=[{"role": "user", "content": "hi"}]
        )
    assert excinfo.value.status_code == 502
    assert "slow down" in json.dumps(excinfo.value.detail)


def test_anthropic_stream_yields_text_and_returns_usage(anthropic_calls):
    events = [
        {"type": "message_start",
         "message": {"usage": {"input_tokens": 9, "output_tokens": 0}}},
        {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "he"}},
        {"type": "content_block_delta", "delta": {"type": "thinking_delta",
                                                  "thinking": "skip me"}},
        {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "llo"}},
        {"type": "message_delta", "delta": {"stop_reason": "max_tokens"},
         "usage": {"output_tokens": 6}},
    ]
    body = "".join(f"data: {json.dumps(e)}\n\n" for e in events)
    anthropic_calls(lambda request: httpx.Response(200, text=body))

    collector = StreamCollector(
        anthropic_provider.AnthropicProvider().stream(
            model="claude-3-5-sonnet", messages=[{"role": "user", "content": "hi"}]
        )
    )
    assert list(collector) == ["he", "llo"]
    assert collector.usage.input_tokens == 9
    assert collector.usage.output_tokens == 6
    assert collector.usage.finish_reason == "length"


def test_anthropic_stream_sets_the_stream_flag(anthropic_calls):
    sent = anthropic_calls(lambda request: httpx.Response(200, text=""))
    list(
        StreamCollector(
            anthropic_provider.AnthropicProvider().stream(
                model="claude-3-5-sonnet", messages=[{"role": "user", "content": "hi"}]
            )
        )
    )
    assert sent[0]["stream"] is True


def test_no_provider_module_rebinds_the_global_httpx_client(monkeypatch):
    """The failure that broke the delivery eval must not come back."""
    before = httpx.Client
    mock_provider_transport(
        monkeypatch, openai_provider, lambda request: httpx.Response(200, json={})
    )
    mock_provider_transport(
        monkeypatch, anthropic_provider, lambda request: httpx.Response(200, json={})
    )
    assert httpx.Client is before
    assert isinstance(httpx.Client, type)


def test_the_patch_does_not_leak_between_tests():
    """After the previous test, both providers must have their real factory."""
    assert openai_provider.http_client.__module__ == openai_provider.__name__
    assert anthropic_provider.http_client.__module__ == anthropic_provider.__name__


# --- the token limit field depends on the model family ----------------


@pytest.mark.parametrize("model", ["gpt-5", "gpt-5.4-mini", "o1", "o1-mini", "o3",
                                   "o3-mini", "o4-mini"])
def test_reasoning_models_get_max_completion_tokens(openai_calls, model):
    """Recorded request body: the limit travels under the reasoning name."""
    sent = openai_calls(lambda request: httpx.Response(200, json=openai_chat_body()))
    openai_provider.OpenAIProvider().complete(
        model=model, messages=[{"role": "user", "content": "hi"}], max_tokens=64
    )

    assert sent[0]["max_completion_tokens"] == 64
    assert "max_tokens" not in sent[0]


@pytest.mark.parametrize("model", ["gpt-4o", "gpt-4o-mini", "gpt-4.1", "gpt-3.5-turbo",
                                   "chatgpt-4o-latest"])
def test_other_models_keep_max_tokens(openai_calls, model):
    sent = openai_calls(lambda request: httpx.Response(200, json=openai_chat_body()))
    openai_provider.OpenAIProvider().complete(
        model=model, messages=[{"role": "user", "content": "hi"}], max_tokens=64
    )

    assert sent[0]["max_tokens"] == 64
    assert "max_completion_tokens" not in sent[0]


def test_neither_field_is_sent_when_the_caller_omits_a_limit(openai_calls):
    sent = openai_calls(lambda request: httpx.Response(200, json=openai_chat_body()))
    openai_provider.OpenAIProvider().complete(
        model="gpt-5", messages=[{"role": "user", "content": "hi"}]
    )

    assert "max_tokens" not in sent[0]
    assert "max_completion_tokens" not in sent[0]


def test_the_mapping_applies_to_a_streamed_request_too(openai_calls):
    sent = openai_calls(lambda request: httpx.Response(200, text="data: [DONE]\n\n"))
    list(
        StreamCollector(
            openai_provider.OpenAIProvider().stream(
                model="o3-mini",
                messages=[{"role": "user", "content": "hi"}],
                max_tokens=32,
            )
        )
    )

    assert sent[0]["max_completion_tokens"] == 32
    assert "max_tokens" not in sent[0]


def test_temperature_is_passed_through_unchanged_for_reasoning_models(openai_calls):
    """Temperature handling is deliberately untouched.

    Some reasoning models accept only the default and will refuse another
    value upstream; the gateway does not second-guess the caller.
    """
    sent = openai_calls(lambda request: httpx.Response(200, json=openai_chat_body()))
    openai_provider.OpenAIProvider().complete(
        model="gpt-5", messages=[{"role": "user", "content": "hi"}], temperature=0.7
    )

    assert sent[0]["temperature"] == 0.7


@pytest.mark.parametrize(
    "model, expected",
    [("gpt-5", "max_completion_tokens"), ("o4-mini", "max_completion_tokens"),
     ("gpt-4o", "max_tokens")],
)
def test_token_limit_field_is_exposed_for_callers(model, expected):
    assert openai_provider.token_limit_field(model) == expected


def test_a_responses_max_output_tokens_reaches_the_reasoning_field(
    governance_db, monkeypatch
):
    """/v1/responses sends max_output_tokens; the upstream chat call renames it."""
    from fastapi.testclient import TestClient

    from gateway.config import Route
    from tests.gateway_support import HEADERS, build

    sent = mock_provider_transport(
        monkeypatch,
        openai_provider,
        lambda request: httpx.Response(200, json=openai_chat_body()),
    )
    app, _ = build(
        governance_db,
        routes=(Route("gpt-5", "openai"),),
        providers={"openai": openai_provider.OpenAIProvider()},
    )
    with TestClient(app) as client:
        response = client.post(
            "/v1/responses",
            headers=HEADERS,
            json={"model": "gpt-5", "input": "hi", "max_output_tokens": 128},
        )

    assert response.status_code == 200
    assert sent[0]["max_completion_tokens"] == 128
    assert "max_tokens" not in sent[0]
