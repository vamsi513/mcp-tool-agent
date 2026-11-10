"""The unauthenticated routes a browser or a health check hits.

These three answer without a key. Everything under /v1 still needs one, and
the root body must not leak how the instance is configured.
"""

import pytest
from fastapi.testclient import TestClient

from gateway.app import SERVICE_NAME, SUPPORTED_PATHS
from gateway.config import Route
from gateway.providers.fake import FakeProvider
from tests.gateway_support import KEY, build


@pytest.fixture
def client(governance_db):
    app, fake = build(governance_db, FakeProvider(reply="hello there"))
    with TestClient(app) as test_client:
        yield test_client, fake


# --- /healthz ---------------------------------------------------------


def test_healthz_is_200_and_reports_ok(client):
    test_client, _ = client
    response = test_client.get("/healthz")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_healthz_needs_no_authorization_header(client):
    test_client, _ = client
    assert "authorization" not in {k.lower() for k in test_client.headers}
    assert test_client.get("/healthz").status_code == 200


def test_healthz_touches_no_provider(client):
    test_client, fake = client
    test_client.get("/healthz")
    assert fake.calls == []


def test_healthz_has_exactly_one_field(client):
    test_client, _ = client
    body = test_client.get("/healthz").json()
    assert body == {"status": "ok"}
    assert list(body) == ["status"]


def test_healthz_exposes_no_configuration(governance_db):
    """Liveness must not tell an unauthenticated caller how this is set up.

    Built with distinctive config, as for the root route, so a leak would be
    unmistakable.
    """
    app, _ = build(
        governance_db,
        budget=123.456,
        routes=(Route("secret-prefix-", "fake"),),
        providers={"fake": FakeProvider(reply="x")},
    )
    with TestClient(app) as test_client:
        response = test_client.get("/healthz")

    assert response.json() == {"status": "ok"}
    text = response.text
    for leak in (
        KEY,                 # the API key
        "secret-prefix-",    # a configured model prefix
        "123.456",           # a budget
        "sqlite",            # the session backend
        "redis",
        "fake",              # a provider name
        "anthropic",
        "openai",
        "routes",
        "providers",
        "session_backend",
    ):
        assert leak not in text, f"the healthz body leaked {leak!r}"


# --- / ----------------------------------------------------------------


def test_root_is_200_with_the_service_name_and_paths(client):
    test_client, _ = client
    response = test_client.get("/")

    assert response.status_code == 200
    body = response.json()
    assert body == {"service": SERVICE_NAME, "paths": list(SUPPORTED_PATHS)}


def test_root_lists_the_three_documented_paths(client):
    test_client, _ = client
    paths = test_client.get("/").json()["paths"]
    assert paths == ["/v1/chat/completions", "/v1/responses", "/healthz"]


def test_root_needs_no_authorization_header(client):
    test_client, _ = client
    assert test_client.get("/").status_code == 200


def test_root_has_exactly_two_fields(client):
    """"nothing else" is checked, not assumed."""
    test_client, _ = client
    assert sorted(test_client.get("/").json()) == ["paths", "service"]


def test_root_touches_no_provider(client):
    test_client, fake = client
    test_client.get("/")
    assert fake.calls == []


def test_root_exposes_no_configuration(governance_db):
    """No key, model name, provider name or backend reaches the root body.

    Built with distinctive config so a leak would be unmistakable.
    """
    app, _ = build(
        governance_db,
        budget=123.456,
        routes=(Route("secret-prefix-", "fake"),),
        providers={"fake": FakeProvider(reply="x")},
    )
    with TestClient(app) as test_client:
        text = test_client.get("/").text

    for leak in (
        KEY,                 # the API key
        "secret-prefix-",    # a configured model prefix
        "123.456",           # a budget
        "sqlite",            # the session backend
        "fake",              # a provider name
        "anthropic",
        "openai",
    ):
        assert leak not in text, f"the root body leaked {leak!r}"


def test_root_body_is_small(client):
    test_client, _ = client
    assert len(test_client.get("/").content) < 200


# --- /favicon.ico -----------------------------------------------------


def test_favicon_is_204_with_no_body(client):
    test_client, _ = client
    response = test_client.get("/favicon.ico")

    assert response.status_code == 204
    assert response.content == b""


def test_favicon_needs_no_authorization_header(client):
    test_client, _ = client
    assert test_client.get("/favicon.ico").status_code == 204


# --- none of this weakened the authenticated routes -------------------


def test_chat_completions_still_401s_without_a_key(client):
    test_client, _ = client
    response = test_client.post(
        "/v1/chat/completions",
        json={"model": "fake-1", "messages": [{"role": "user", "content": "x"}]},
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "missing_api_key"


def test_responses_still_401s_without_a_key(client):
    test_client, _ = client
    response = test_client.post("/v1/responses", json={"model": "fake-1", "input": "x"})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "missing_api_key"


def test_a_v1_route_still_works_with_a_key(client):
    test_client, _ = client
    response = test_client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {KEY}"},
        json={"model": "fake-1", "messages": [{"role": "user", "content": "x"}]},
    )
    assert response.status_code == 200


def test_an_unknown_path_is_still_a_404(client):
    """Only the three routes were added; nothing else became reachable."""
    test_client, _ = client
    assert test_client.get("/v1/models").status_code == 404
    assert test_client.get("/admin").status_code == 404
    assert test_client.post("/", json={}).status_code == 405
