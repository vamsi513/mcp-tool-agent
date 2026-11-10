"""Both endpoints against the fake provider: shapes, routing, errors."""

import pytest
from fastapi.testclient import TestClient

from gateway.config import Route
from gateway.providers.fake import FakeProvider
from tests.gateway_support import HEADERS, build, sse_events


@pytest.fixture
def client(governance_db):
    app, fake = build(governance_db, FakeProvider(reply="hello there"))
    with TestClient(app) as test_client:
        yield test_client, fake


# --- /v1/chat/completions ---------------------------------------------


def test_chat_completion_shape(client):
    test_client, _ = client
    response = test_client.post(
        "/v1/chat/completions",
        headers=HEADERS,
        json={"model": "fake-1", "messages": [{"role": "user", "content": "a b c"}]},
    )
    assert response.status_code == 200
    body = response.json()

    assert body["object"] == "chat.completion"
    assert body["id"].startswith("chatcmpl-")
    assert body["model"] == "fake-1"
    assert body["choices"][0]["message"] == {"role": "assistant", "content": "hello there"}
    assert body["choices"][0]["finish_reason"] == "stop"
    # usage is the OpenAI naming, and total is the sum.
    assert body["usage"] == {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}


def test_chat_completion_passes_temperature_and_max_tokens(client):
    test_client, _ = client
    response = test_client.post(
        "/v1/chat/completions",
        headers=HEADERS,
        json={
            "model": "fake-1",
            "messages": [{"role": "user", "content": "x"}],
            "temperature": 0.25,
            "max_tokens": 64,
        },
    )
    assert response.status_code == 200


def test_chat_completion_flattens_content_parts(client):
    test_client, fake = client
    test_client.post(
        "/v1/chat/completions",
        headers=HEADERS,
        json={
            "model": "fake-1",
            "messages": [
                {"role": "user", "content": [{"type": "text", "text": "a"},
                                             {"type": "text", "text": "b"}]}
            ],
        },
    )
    assert fake.calls[-1]["messages"] == [{"role": "user", "content": "ab"}]


def test_empty_messages_is_a_400(client):
    test_client, _ = client
    response = test_client.post(
        "/v1/chat/completions", headers=HEADERS, json={"model": "fake-1", "messages": []}
    )
    assert response.status_code == 400
    assert response.json()["error"]["param"] == "messages"


# --- /v1/responses ----------------------------------------------------


def test_responses_shape(client):
    test_client, _ = client
    response = test_client.post(
        "/v1/responses", headers=HEADERS, json={"model": "fake-1", "input": "a b c"}
    )
    assert response.status_code == 200
    body = response.json()

    assert body["id"].startswith("resp_")
    assert body["object"] == "response"
    assert body["status"] == "completed"
    assert body["model"] == "fake-1"

    message = body["output"][0]
    assert message["type"] == "message"
    assert message["role"] == "assistant"
    assert message["content"][0]["type"] == "output_text"
    assert message["content"][0]["text"] == "hello there"
    assert body["output_text"] == "hello there"

    # usage is the Responses naming, not the chat one.
    assert body["usage"] == {"input_tokens": 3, "output_tokens": 2, "total_tokens": 5}


def test_responses_accepts_a_list_input(client):
    test_client, fake = client
    response = test_client.post(
        "/v1/responses",
        headers=HEADERS,
        json={
            "model": "fake-1",
            "input": [
                {"role": "user", "content": "one"},
                {"role": "assistant", "content": "two"},
                {"role": "user", "content": "three"},
            ],
        },
    )
    assert response.status_code == 200
    assert [m["content"] for m in fake.calls[-1]["messages"]] == ["one", "two", "three"]


def test_responses_instructions_reach_the_provider(client):
    test_client, fake = client
    test_client.post(
        "/v1/responses",
        headers=HEADERS,
        json={"model": "fake-1", "input": "hi", "instructions": "be terse"},
    )
    assert fake.calls[-1]["extra_system"] == "be terse"


def test_responses_max_output_tokens_is_accepted(client):
    test_client, _ = client
    response = test_client.post(
        "/v1/responses",
        headers=HEADERS,
        json={"model": "fake-1", "input": "hi", "max_output_tokens": 32},
    )
    assert response.status_code == 200


def test_empty_input_is_a_400(client):
    test_client, _ = client
    response = test_client.post("/v1/responses", headers=HEADERS, json={"model": "fake-1"})
    assert response.status_code == 400
    assert response.json()["error"]["param"] == "input"


# --- routing ----------------------------------------------------------


def test_unknown_model_is_a_400_naming_the_prefixes(client):
    test_client, _ = client
    response = test_client.post(
        "/v1/chat/completions",
        headers=HEADERS,
        json={"model": "llama-3", "messages": [{"role": "user", "content": "x"}]},
    )
    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == "model_not_found"
    assert "fake-" in error["message"]


def test_unknown_model_is_a_400_on_responses_too(client):
    test_client, _ = client
    response = test_client.post(
        "/v1/responses", headers=HEADERS, json={"model": "llama-3", "input": "x"}
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "model_not_found"


def test_the_first_matching_prefix_wins(governance_db):
    specific = FakeProvider(reply="specific")
    general = FakeProvider(reply="general")
    app, _ = build(
        governance_db,
        routes=(Route("fake-special", "fake_a"), Route("fake-", "fake_b")),
        providers={"fake_a": specific, "fake_b": general},
    )
    with TestClient(app) as test_client:
        first = test_client.post(
            "/v1/chat/completions",
            headers=HEADERS,
            json={"model": "fake-special-1", "messages": [{"role": "user", "content": "x"}]},
        ).json()
        second = test_client.post(
            "/v1/chat/completions",
            headers=HEADERS,
            json={"model": "fake-other", "messages": [{"role": "user", "content": "x"}]},
        ).json()

    assert first["choices"][0]["message"]["content"] == "specific"
    assert second["choices"][0]["message"]["content"] == "general"


# --- auth -------------------------------------------------------------


def test_a_missing_key_is_a_401(client):
    test_client, _ = client
    response = test_client.post(
        "/v1/chat/completions",
        json={"model": "fake-1", "messages": [{"role": "user", "content": "x"}]},
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "missing_api_key"


def test_an_unknown_key_is_a_401(client):
    test_client, _ = client
    response = test_client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer nope"},
        json={"model": "fake-1", "messages": [{"role": "user", "content": "x"}]},
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_api_key"


def test_an_error_body_never_echoes_the_key(client):
    test_client, _ = client
    response = test_client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer super-secret-value"},
        json={"model": "fake-1", "messages": [{"role": "user", "content": "x"}]},
    )
    assert "super-secret-value" not in response.text


# --- unsupported features are refused, not silently dropped -----------


@pytest.mark.parametrize(
    "field, value",
    [("tools", [{"type": "function"}]), ("tool_choice", "auto"), ("n", 2),
     ("response_format", {"type": "json_object"}), ("logprobs", True)],
)
def test_unsupported_chat_fields_are_refused(client, field, value):
    test_client, _ = client
    response = test_client.post(
        "/v1/chat/completions",
        headers=HEADERS,
        json={"model": "fake-1", "messages": [{"role": "user", "content": "x"}], field: value},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "unsupported_feature"
    assert field in response.json()["error"]["message"]


@pytest.mark.parametrize("field, value", [("tools", [{"type": "function"}]),
                                          ("reasoning", {"effort": "low"})])
def test_unsupported_responses_fields_are_refused(client, field, value):
    test_client, _ = client
    response = test_client.post(
        "/v1/responses", headers=HEADERS, json={"model": "fake-1", "input": "x", field: value}
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "unsupported_feature"


# --- health -----------------------------------------------------------


def test_healthz_reports_the_wiring(client):
    test_client, _ = client
    body = test_client.get("/healthz").json()
    assert body["status"] == "ok"
    assert body["session_backend"] == "sqlite"
    assert {"prefix": "fake-", "provider": "fake"} in body["routes"]


# --- streaming --------------------------------------------------------


def test_chat_streaming_uses_the_chat_chunk_format(client):
    test_client, _ = client
    with test_client.stream(
        "POST",
        "/v1/chat/completions",
        headers=HEADERS,
        json={"model": "fake-1", "messages": [{"role": "user", "content": "x"}],
              "stream": True},
    ) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        response.read()

    events = sse_events(response)
    assert events[-1]["done"] is True
    chunks = [e["data"] for e in events if "data" in e]
    assert all(c["object"] == "chat.completion.chunk" for c in chunks)
    # Named events belong to the responses endpoint, not this one.
    assert all(e["event"] is None for e in events)

    text = "".join(
        c["choices"][0]["delta"].get("content", "") for c in chunks
    )
    assert text == "hello there"
    assert chunks[-1]["choices"][0]["finish_reason"] == "stop"


def test_responses_streaming_uses_named_events(client):
    test_client, _ = client
    with test_client.stream(
        "POST",
        "/v1/responses",
        headers=HEADERS,
        json={"model": "fake-1", "input": "x", "stream": True},
    ) as response:
        assert response.status_code == 200
        response.read()

    events = sse_events(response)
    names = [e["event"] for e in events]
    assert names[0] == "response.created"
    assert names[-1] == "response.completed"
    assert "response.output_text.delta" in names

    deltas = [e["data"]["delta"] for e in events
              if e["event"] == "response.output_text.delta"]
    assert "".join(deltas) == "hello there"

    final = events[-1]["data"]["response"]
    assert final["object"] == "response"
    assert final["output_text"] == "hello there"
    assert final["usage"]["total_tokens"] == final["usage"]["input_tokens"] + final[
        "usage"
    ]["output_tokens"]


def test_the_two_streaming_formats_differ(client):
    """Each endpoint streams in its own format, not a shared one."""
    test_client, _ = client
    with test_client.stream(
        "POST", "/v1/chat/completions", headers=HEADERS,
        json={"model": "fake-1", "messages": [{"role": "user", "content": "x"}],
              "stream": True},
    ) as chat:
        chat.read()
    with test_client.stream(
        "POST", "/v1/responses", headers=HEADERS,
        json={"model": "fake-1", "input": "x", "stream": True},
    ) as responses:
        responses.read()

    assert "chat.completion.chunk" in chat.text
    assert "event: response.completed" in responses.text
    assert "chat.completion.chunk" not in responses.text
    assert "event: response" not in chat.text
