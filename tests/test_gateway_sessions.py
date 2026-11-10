"""Session state for /v1/responses, on both backends.

The Redis tests use a real server. They skip when one is not reachable, and CI
runs them against a service container.
"""

import sqlite3

import httpx
import pytest
from fastapi.testclient import TestClient

from gateway.providers import anthropic as anthropic_provider
from gateway.providers.anthropic import AnthropicProvider
from gateway.providers.fake import FakeProvider
from gateway.sessions import (
    RedisSessionStore,
    Session,
    SqliteSessionStore,
    build_store,
)
from tests.gateway_support import (
    HEADERS,
    anthropic_message_body,
    build,
    mock_provider_transport,
)

REDIS_URL = "redis://127.0.0.1:6379/15"


def redis_client():
    try:
        import redis
    except ImportError:  # pragma: no cover
        return None
    try:
        client = redis.Redis.from_url(REDIS_URL, decode_responses=True,
                                      socket_connect_timeout=1)
        client.ping()
        return client
    except Exception:
        return None


requires_redis = pytest.mark.skipif(
    redis_client() is None,
    reason="no Redis reachable at 127.0.0.1:6379; CI runs these in a service container",
)


@pytest.fixture
def redis_store():
    client = redis_client()
    assert client is not None
    client.flushdb()
    try:
        yield RedisSessionStore(REDIS_URL, client=client)
    finally:
        client.flushdb()


@pytest.fixture(params=["sqlite", "redis"])
def store(request, governance_db):
    if request.param == "sqlite":
        return SqliteSessionStore(governance_db)
    client = redis_client()
    if client is None:
        pytest.skip("no Redis reachable at 127.0.0.1:6379")
    client.flushdb()
    return RedisSessionStore(REDIS_URL, client=client)


# --- the store interface, on both backends ----------------------------


def test_round_trip(store):
    session = Session(
        response_id="resp_a",
        key_id="tester",
        model="claude-3-5-sonnet",
        provider="anthropic",
        messages=[{"role": "user", "content": "hi"}],
        instructions="be terse",
    )
    store.put(session, 60)
    got = store.get("resp_a")

    assert got is not None
    assert got.response_id == "resp_a"
    assert got.provider == "anthropic"
    assert got.messages == [{"role": "user", "content": "hi"}]
    assert got.instructions == "be terse"


def test_unknown_id_is_none(store):
    assert store.get("resp_missing") is None


def test_delete_removes_it(store):
    store.put(Session("resp_b", "tester", "m", "fake"), 60)
    store.delete("resp_b")
    assert store.get("resp_b") is None


def test_put_overwrites(store):
    store.put(Session("resp_c", "tester", "m", "fake", [{"role": "user", "content": "1"}]), 60)
    store.put(Session("resp_c", "tester", "m", "fake", [{"role": "user", "content": "2"}]), 60)
    got = store.get("resp_c")
    assert got is not None
    assert got.messages == [{"role": "user", "content": "2"}]


def test_a_zero_ttl_entry_is_gone(store):
    """Expiry is enforced, whichever backend holds it."""
    if isinstance(store, SqliteSessionStore):
        store.put(Session("resp_d", "tester", "m", "fake"), 60)
        conn = sqlite3.connect(store._db)
        conn.execute(
            "UPDATE gateway_sessions SET expires_ts = '2000-01-01T00:00:00.000+00:00'"
            " WHERE response_id = 'resp_d'"
        )
        conn.commit()
        conn.close()
    else:
        store.put(Session("resp_d", "tester", "m", "fake"), 60)
        store._client.delete(store._key("resp_d"))  # Redis expiry, forced
    assert store.get("resp_d") is None


def test_build_store_rejects_an_unknown_backend():
    with pytest.raises(ValueError, match="unknown session backend"):
        build_store("memcached")


def test_sqlite_purge_drops_only_expired_rows(governance_db):
    store = SqliteSessionStore(governance_db)
    store.put(Session("keep", "t", "m", "fake"), 60)
    store.put(Session("drop", "t", "m", "fake"), 60)
    conn = sqlite3.connect(governance_db)
    conn.execute(
        "UPDATE gateway_sessions SET expires_ts = '2000-01-01T00:00:00.000+00:00'"
        " WHERE response_id = 'drop'"
    )
    conn.commit()
    conn.close()

    assert store.purge_expired() == 1
    assert store.get("keep") is not None
    assert store.get("drop") is None


@requires_redis
def test_redis_sets_a_real_ttl(redis_store):
    redis_store.put(Session("resp_ttl", "t", "m", "fake"), 30)
    ttl = redis_store._client.ttl(redis_store._key("resp_ttl"))
    assert 0 < ttl <= 30


@requires_redis
def test_redis_purge_is_a_no_op(redis_store):
    """Redis expires entries itself, so there is nothing to sweep."""
    assert redis_store.purge_expired() == 0


# --- through the endpoint ---------------------------------------------


def test_three_turn_chain_on_anthropic(governance_db, monkeypatch):
    """previous_response_id works against Anthropic, which has no sessions.

    Each turn goes to the real Anthropic provider with a mocked transport, so
    the merged, system-hoisted body is what the chain is checked against.
    """
    replies = ["first reply", "second reply", "third reply"]
    state = {"n": 0}

    def handler(request):
        body = anthropic_message_body(text=replies[state["n"]])
        state["n"] += 1
        return httpx.Response(200, json=body)

    sent = mock_provider_transport(monkeypatch, anthropic_provider, handler)
    app, _ = build(
        governance_db,
        providers={"anthropic": AnthropicProvider(default_max_tokens=64)},
    )

    with TestClient(app) as client:
        first = client.post(
            "/v1/responses",
            headers=HEADERS,
            json={"model": "claude-3-5-sonnet", "input": "turn one",
                  "instructions": "be terse"},
        ).json()
        second = client.post(
            "/v1/responses",
            headers=HEADERS,
            json={"model": "claude-3-5-sonnet", "input": "turn two",
                  "previous_response_id": first["id"]},
        ).json()
        third = client.post(
            "/v1/responses",
            headers=HEADERS,
            json={"model": "claude-3-5-sonnet", "input": "turn three",
                  "previous_response_id": second["id"]},
        ).json()

    assert first["output_text"] == "first reply"
    assert second["output_text"] == "second reply"
    assert third["output_text"] == "third reply"
    assert len({first["id"], second["id"], third["id"]}) == 3

    # The third request carried the whole conversation, with roles alternating.
    turns = sent[2]["messages"]
    assert [t["content"] for t in turns] == [
        "turn one", "first reply", "turn two", "second reply", "turn three",
    ]
    assert [t["role"] for t in turns] == [
        "user", "assistant", "user", "assistant", "user",
    ]
    # The instructions set on turn one still apply on turn three.
    assert sent[2]["system"] == "be terse"
    assert sent[2]["max_tokens"] == 64


def test_unknown_previous_response_id_is_a_404(governance_db):
    app, _ = build(governance_db)
    with TestClient(app) as client:
        response = client.post(
            "/v1/responses",
            headers=HEADERS,
            json={"model": "fake-1", "input": "x", "previous_response_id": "resp_nope"},
        )
    assert response.status_code == 404
    error = response.json()["error"]
    assert error["code"] == "response_not_found"
    assert error["param"] == "previous_response_id"
    assert "resp_nope" in error["message"]


def test_expired_previous_response_id_is_a_404(governance_db):
    app, _ = build(governance_db, FakeProvider(reply="ok"), ttl_seconds=3600)
    with TestClient(app) as client:
        first = client.post(
            "/v1/responses", headers=HEADERS, json={"model": "fake-1", "input": "x"}
        ).json()

        conn = sqlite3.connect(governance_db)
        conn.execute(
            "UPDATE gateway_sessions SET expires_ts = '2000-01-01T00:00:00.000+00:00'"
        )
        conn.commit()
        conn.close()

        response = client.post(
            "/v1/responses",
            headers=HEADERS,
            json={"model": "fake-1", "input": "y", "previous_response_id": first["id"]},
        )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "response_not_found"


def test_streaming_also_stores_the_session(governance_db):
    app, _ = build(governance_db, FakeProvider(reply="streamed reply"))
    with TestClient(app) as client:
        with client.stream(
            "POST", "/v1/responses", headers=HEADERS,
            json={"model": "fake-1", "input": "x", "stream": True},
        ) as response:
            response.read()
        events = [line for line in response.text.splitlines() if line.startswith("data:")]
        import json as _json

        final = _json.loads(events[-1][len("data:"):])["response"]

        follow_up = client.post(
            "/v1/responses",
            headers=HEADERS,
            json={"model": "fake-1", "input": "y", "previous_response_id": final["id"]},
        )
    assert follow_up.status_code == 200


@requires_redis
def test_chain_works_with_the_redis_backend(governance_db, redis_store):
    app, fake = build(governance_db, FakeProvider(scripted=["one", "two"]),
                      store=redis_store)
    with TestClient(app) as client:
        first = client.post(
            "/v1/responses", headers=HEADERS, json={"model": "fake-1", "input": "a"}
        ).json()
        second = client.post(
            "/v1/responses",
            headers=HEADERS,
            json={"model": "fake-1", "input": "b", "previous_response_id": first["id"]},
        ).json()

    assert first["output_text"] == "one"
    assert second["output_text"] == "two"
    assert [m["content"] for m in fake.calls[-1]["messages"]] == ["a", "one", "b"]


@requires_redis
def test_unknown_id_is_a_404_on_redis_too(governance_db, redis_store):
    app, _ = build(governance_db, store=redis_store)
    with TestClient(app) as client:
        response = client.post(
            "/v1/responses",
            headers=HEADERS,
            json={"model": "fake-1", "input": "x", "previous_response_id": "resp_gone"},
        )
    assert response.status_code == 404
