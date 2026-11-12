"""The Ollama provider against a mock transport. No server is contacted.

Every test installs a handler on the provider module's own `http_client`
factory, the same way the OpenAI and Anthropic provider tests do, so nothing
here opens a socket and no Ollama needs to be running.
"""

from __future__ import annotations

import json

import httpx
import pytest
from fastapi.testclient import TestClient

import metering
from gateway.app import create_app
from gateway.config import (
    DEFAULT_OLLAMA_BASE_URL,
    GatewayConfig,
    KeyConfig,
    OllamaConfig,
    Route,
    parse_config,
)
from gateway.errors import GatewayError
from gateway.providers import ollama as ollama_module
from gateway.providers.ollama import OllamaProvider
from gateway.sessions import SqliteSessionStore
from tests.gateway_support import (
    HEADERS,
    KEY,
    KEY_ID,
    mock_provider_transport,
    sse_events,
)

MODEL = "llama3.2"


def chat_body(text: str = "hello from a local model", finish: str = "stop") -> dict:
    return {
        "id": "chatcmpl-ollama",
        "object": "chat.completion",
        "model": MODEL,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": finish,
            }
        ],
        "usage": {"prompt_tokens": 9, "completion_tokens": 4, "total_tokens": 13},
    }


def sse(chunks: list[dict]) -> str:
    return "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks) + "data: [DONE]\n\n"


def stream_body(words: list[str]) -> str:
    chunks: list[dict] = []
    for word in words:
        chunks.append(
            {
                "id": "chatcmpl-ollama",
                "object": "chat.completion.chunk",
                "model": MODEL,
                "choices": [{"index": 0, "delta": {"content": word}}],
            }
        )
    chunks.append(
        {
            "id": "chatcmpl-ollama",
            "object": "chat.completion.chunk",
            "model": MODEL,
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 7, "completion_tokens": 3},
        }
    )
    return sse(chunks)


# ------------------------------------------------------------ normal replies


def test_a_normal_completion_is_translated(monkeypatch):
    sent = mock_provider_transport(
        monkeypatch,
        ollama_module,
        lambda request: httpx.Response(200, json=chat_body()),
    )
    result = OllamaProvider().complete(
        model=MODEL, messages=[{"role": "user", "content": "hi"}]
    )

    assert result.text == "hello from a local model"
    assert result.finish_reason == "stop"
    assert result.input_tokens == 9
    assert result.output_tokens == 4
    assert result.model == MODEL
    assert sent[0]["model"] == MODEL
    assert sent[0]["messages"] == [{"role": "user", "content": "hi"}]
    assert "stream" not in sent[0]


def test_no_credential_is_sent(monkeypatch):
    seen: list[httpx.Headers] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers)
        return httpx.Response(200, json=chat_body())

    mock_provider_transport(monkeypatch, ollama_module, handler)
    OllamaProvider().complete(model=MODEL, messages=[{"role": "user", "content": "hi"}])

    assert "authorization" not in seen[0]
    assert seen[0]["content-type"] == "application/json"


def test_instructions_become_a_leading_system_turn(monkeypatch):
    sent = mock_provider_transport(
        monkeypatch,
        ollama_module,
        lambda request: httpx.Response(200, json=chat_body()),
    )
    OllamaProvider().complete(
        model=MODEL,
        messages=[{"role": "user", "content": "hi"}],
        extra_system="be brief",
    )
    assert sent[0]["messages"][0] == {"role": "system", "content": "be brief"}


def test_a_token_limit_goes_upstream_as_max_tokens(monkeypatch):
    sent = mock_provider_transport(
        monkeypatch,
        ollama_module,
        lambda request: httpx.Response(200, json=chat_body()),
    )
    OllamaProvider().complete(
        model=MODEL,
        messages=[{"role": "user", "content": "hi"}],
        max_tokens=64,
        temperature=0.2,
    )
    assert sent[0]["max_tokens"] == 64
    assert sent[0]["temperature"] == 0.2
    assert "max_completion_tokens" not in sent[0]


def test_a_reply_with_no_usage_reports_zeros_rather_than_guessing(monkeypatch):
    body = chat_body()
    body.pop("usage")
    mock_provider_transport(
        monkeypatch, ollama_module, lambda request: httpx.Response(200, json=body)
    )
    result = OllamaProvider().complete(
        model=MODEL, messages=[{"role": "user", "content": "hi"}]
    )
    assert (result.input_tokens, result.output_tokens) == (0, 0)


# ------------------------------------------------------------------ streaming


def test_a_stream_yields_deltas_then_returns_usage(monkeypatch):
    sent = mock_provider_transport(
        monkeypatch,
        ollama_module,
        lambda request: httpx.Response(200, text=stream_body(["one", " two", " three"])),
    )
    generator = OllamaProvider().stream(
        model=MODEL, messages=[{"role": "user", "content": "hi"}]
    )
    deltas = []
    try:
        while True:
            deltas.append(next(generator))
    except StopIteration as stop:
        usage = stop.value

    assert deltas == ["one", " two", " three"]
    assert usage.text == "one two three"
    assert usage.finish_reason == "stop"
    assert (usage.input_tokens, usage.output_tokens) == (7, 3)
    assert sent[0]["stream"] is True
    assert sent[0]["stream_options"] == {"include_usage": True}


def test_keepalives_and_junk_lines_are_ignored(monkeypatch):
    text = (
        ": keep alive\n\n"
        "data: not json\n\n"
        "data: []\n\n" + stream_body(["only"])
    )
    mock_provider_transport(
        monkeypatch, ollama_module, lambda request: httpx.Response(200, text=text)
    )
    generator = OllamaProvider().stream(
        model=MODEL, messages=[{"role": "user", "content": "hi"}]
    )
    assert list(generator) == ["only"]


# --------------------------------------------------------------------- errors


def test_an_upstream_error_becomes_a_gateway_failure(monkeypatch):
    mock_provider_transport(
        monkeypatch,
        ollama_module,
        lambda request: httpx.Response(404, json={"error": {"message": "model not found"}}),
    )
    with pytest.raises(GatewayError) as caught:
        OllamaProvider().complete(model=MODEL, messages=[{"role": "user", "content": "hi"}])

    assert caught.value.status_code == 502
    assert caught.value.detail["error"]["code"] == "upstream_error"
    assert "model not found" in caught.value.detail["error"]["message"]


def test_ollamas_bare_string_error_is_reported(monkeypatch):
    """Ollama answers some failures with {"error": "..."} rather than a dict."""
    mock_provider_transport(
        monkeypatch,
        ollama_module,
        lambda request: httpx.Response(500, json={"error": "llama runner process has terminated"}),
    )
    with pytest.raises(GatewayError) as caught:
        OllamaProvider().complete(model=MODEL, messages=[{"role": "user", "content": "hi"}])
    assert "llama runner" in caught.value.detail["error"]["message"]


def test_a_refused_connection_says_the_server_is_not_reachable(monkeypatch):
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    mock_provider_transport(monkeypatch, ollama_module, refuse)
    with pytest.raises(GatewayError) as caught:
        OllamaProvider().complete(model=MODEL, messages=[{"role": "user", "content": "hi"}])

    message = caught.value.detail["error"]["message"]
    assert caught.value.status_code == 502
    assert "could not reach an Ollama server" in message
    assert DEFAULT_OLLAMA_BASE_URL in message


def test_a_timeout_names_the_deadline(monkeypatch):
    def time_out(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("too slow", request=request)

    mock_provider_transport(monkeypatch, ollama_module, time_out)
    with pytest.raises(GatewayError) as caught:
        OllamaProvider(timeout_seconds=30).complete(
            model=MODEL, messages=[{"role": "user", "content": "hi"}]
        )
    assert "did not answer within 30s" in caught.value.detail["error"]["message"]


def test_a_refused_connection_during_a_stream_is_reported(monkeypatch):
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    mock_provider_transport(monkeypatch, ollama_module, refuse)
    generator = OllamaProvider().stream(
        model=MODEL, messages=[{"role": "user", "content": "hi"}]
    )
    with pytest.raises(GatewayError) as caught:
        list(generator)
    assert "could not reach an Ollama server" in caught.value.detail["error"]["message"]


def test_an_error_status_on_a_stream_is_reported(monkeypatch):
    mock_provider_transport(
        monkeypatch,
        ollama_module,
        lambda request: httpx.Response(503, json={"error": {"message": "busy"}}),
    )
    generator = OllamaProvider().stream(
        model=MODEL, messages=[{"role": "user", "content": "hi"}]
    )
    with pytest.raises(GatewayError) as caught:
        list(generator)
    assert caught.value.status_code == 502


# -------------------------------------------------------------------- config


def test_the_endpoint_comes_from_the_config(monkeypatch):
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json=chat_body())

    mock_provider_transport(monkeypatch, ollama_module, handler)
    OllamaProvider(base_url="http://10.0.0.5:11434/v1/").complete(
        model=MODEL, messages=[{"role": "user", "content": "hi"}]
    )
    assert seen == ["http://10.0.0.5:11434/v1/chat/completions"]


def test_config_defaults_when_the_section_is_absent():
    config = parse_config({"routes": [], "keys": []})
    assert config.ollama.base_url == DEFAULT_OLLAMA_BASE_URL
    assert config.ollama.timeout_seconds == 120.0


def test_config_reads_the_providers_section():
    config = parse_config(
        {
            "routes": [{"prefix": "llama", "provider": "ollama"}],
            "keys": [],
            "providers": {
                "ollama": {"base_url": "http://box:11434/v1", "timeout_seconds": 30}
            },
        }
    )
    assert config.ollama.base_url == "http://box:11434/v1"
    assert config.ollama.timeout_seconds == 30.0
    assert config.provider_for("llama3.2") == "ollama"


@pytest.mark.parametrize(
    "section",
    [
        {"base_url": ""},
        {"timeout_seconds": 0},
        {"timeout_seconds": -1},
        {"timeout_seconds": "soon"},
    ],
)
def test_bad_provider_settings_are_rejected(section):
    from gateway.config import ConfigError

    with pytest.raises(ConfigError):
        parse_config({"routes": [], "keys": [], "providers": {"ollama": section}})


def test_the_example_config_routes_to_ollama():
    from gateway.config import EXAMPLE_CONFIG_PATH, load_config

    config = load_config(EXAMPLE_CONFIG_PATH)
    assert config.provider_for("llama3.2") == "ollama"
    assert config.provider_for("llama3.1:8b") == "ollama"
    assert config.provider_for("qwen2.5-coder") == "ollama"
    assert config.ollama.base_url == DEFAULT_OLLAMA_BASE_URL
    # The other providers still route as they did.
    assert config.provider_for("gpt-4o-mini") == "openai"
    assert config.provider_for("claude-3-5-sonnet") == "anthropic"
    assert config.provider_for("fake-1") == "fake"
    # The example prefixes carry Ollama's own version digit, so a name in a
    # different shape is still unrouted and gets a 400 rather than being
    # sent to a model that does not exist.
    assert config.provider_for("llama-3") is None


# ----------------------------------------------------------- through the app


def ollama_app(db, monkeypatch, handler):
    """The gateway wired so that a model name routes to a mocked Ollama."""
    mock_provider_transport(monkeypatch, ollama_module, handler)
    config = GatewayConfig(
        routes=(Route("llama", "ollama"),),
        keys={KEY: KeyConfig(id=KEY_ID, secret=KEY, monthly_budget_usd=None)},
        ollama=OllamaConfig(base_url=DEFAULT_OLLAMA_BASE_URL, timeout_seconds=5.0),
    )
    prices = metering.parse_prices(
        {
            "currency": "USD",
            "models": {MODEL: {"input_per_million": 0.0, "output_per_million": 0.0}},
        }
    )
    return create_app(
        config,
        {"ollama": OllamaProvider(timeout_seconds=5.0)},
        prices,
        db,
        SqliteSessionStore(db),
    )


def test_a_model_name_routes_to_ollama_end_to_end(tmp_path, monkeypatch):
    app = ollama_app(
        tmp_path / "o.db",
        monkeypatch,
        lambda request: httpx.Response(200, json=chat_body()),
    )
    client = TestClient(app)
    response = client.post(
        "/v1/chat/completions",
        headers=HEADERS,
        json={"model": MODEL, "messages": [{"role": "user", "content": "hi"}]},
    )
    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"] == (
        "hello from a local model"
    )


def test_a_stopped_server_surfaces_as_a_502(tmp_path, monkeypatch):
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    app = ollama_app(tmp_path / "o.db", monkeypatch, refuse)
    client = TestClient(app)
    response = client.post(
        "/v1/chat/completions",
        headers=HEADERS,
        json={"model": MODEL, "messages": [{"role": "user", "content": "hi"}]},
    )
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "upstream_error"
    assert "is it running?" in response.json()["error"]["message"]


def test_streaming_through_the_responses_endpoint(tmp_path, monkeypatch):
    app = ollama_app(
        tmp_path / "o.db",
        monkeypatch,
        lambda request: httpx.Response(200, text=stream_body(["a", " b"])),
    )
    client = TestClient(app)
    response = client.post(
        "/v1/responses",
        headers=HEADERS,
        json={"model": MODEL, "input": "hi", "stream": True},
    )
    assert response.status_code == 200
    events = sse_events(response)
    assert [e["event"] for e in events][-1] == "response.completed"
    assert events[-1]["data"]["response"]["output_text"] == "a b"
