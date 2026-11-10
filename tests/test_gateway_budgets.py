"""Monthly budget per key, on top of the repository's metering."""

import sqlite3
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

import metering
from gateway import budgets
from gateway.config import KeyConfig
from gateway.providers.fake import FakeProvider
from tests.gateway_support import HEADERS, KEY_ID, build, prices


def usage_rows(db):
    conn = sqlite3.connect(db)
    try:
        return conn.execute(
            "SELECT user, model, input_tokens, output_tokens FROM usage_llm"
            " ORDER BY id"
        ).fetchall()
    finally:
        conn.close()


# --- accounting -------------------------------------------------------


def test_spend_comes_from_the_existing_usage_table(governance_db):
    key = KeyConfig(id=KEY_ID, secret="s", monthly_budget_usd=10.0)
    # One unit of cost per token in the test price table.
    metering.record_llm_turn(KEY_ID, "fake-1", 3, 2, db=governance_db)

    state = budgets.check(key, prices(), governance_db)
    assert state.spent == pytest.approx(5.0)
    assert state.limit == 10.0
    assert state.remaining == pytest.approx(5.0)
    assert not state.exceeded


def test_a_key_with_no_budget_is_never_exceeded(governance_db):
    key = KeyConfig(id=KEY_ID, secret="s", monthly_budget_usd=None)
    metering.record_llm_turn(KEY_ID, "fake-1", 1_000, 1_000, db=governance_db)

    state = budgets.check(key, prices(), governance_db)
    assert state.limit is None
    assert state.remaining is None
    assert not state.exceeded


def test_another_key_spend_does_not_count(governance_db):
    metering.record_llm_turn("someone-else", "fake-1", 100, 100, db=governance_db)
    state = budgets.check(
        KeyConfig(id=KEY_ID, secret="s", monthly_budget_usd=1.0), prices(), governance_db
    )
    assert state.spent == 0.0


def test_spend_is_scoped_to_this_month(governance_db):
    metering.record_llm_turn(KEY_ID, "fake-1", 5, 5, db=governance_db)
    conn = sqlite3.connect(governance_db)
    conn.execute("UPDATE usage_llm SET day = '2001-01-15'")
    conn.commit()
    conn.close()

    state = budgets.check(
        KeyConfig(id=KEY_ID, secret="s", monthly_budget_usd=1.0), prices(), governance_db
    )
    assert state.spent == 0.0


def test_month_start_is_the_first(monkeypatch):
    moment = datetime(2026, 11, 17, 12, 0, tzinfo=UTC)
    assert budgets.month_start(moment) == "2026-11-01"


def test_the_summary_says_the_figure_is_an_estimate(governance_db):
    state = budgets.check(
        KeyConfig(id=KEY_ID, secret="s", monthly_budget_usd=1.0), prices(), governance_db
    )
    assert "estimate" in budgets.summary(state)["basis"]


# --- through the endpoints --------------------------------------------


def test_a_successful_call_writes_a_usage_row(governance_db):
    app, _ = build(governance_db, FakeProvider(reply="a b"))
    with TestClient(app) as client:
        client.post(
            "/v1/chat/completions",
            headers=HEADERS,
            json={"model": "fake-1", "messages": [{"role": "user", "content": "x y z"}]},
        )

    rows = usage_rows(governance_db)
    assert len(rows) == 1
    assert rows[0] == (KEY_ID, "fake-1", 3, 2)


def test_responses_writes_a_usage_row_too(governance_db):
    app, _ = build(governance_db, FakeProvider(reply="a b"))
    with TestClient(app) as client:
        client.post("/v1/responses", headers=HEADERS,
                    json={"model": "fake-1", "input": "x y z"})
    assert len(usage_rows(governance_db)) == 1


def test_streaming_writes_a_usage_row_from_the_reported_usage(governance_db):
    app, _ = build(governance_db, FakeProvider(reply="a b c"))
    with TestClient(app) as client:
        with client.stream(
            "POST", "/v1/chat/completions", headers=HEADERS,
            json={"model": "fake-1", "messages": [{"role": "user", "content": "x y"}],
                  "stream": True},
        ) as response:
            response.read()

    rows = usage_rows(governance_db)
    assert len(rows) == 1
    assert rows[0] == (KEY_ID, "fake-1", 2, 3)


def test_over_budget_is_a_429_with_the_documented_code(governance_db):
    # One token costs one unit, so this budget is spent after the first call.
    app, _ = build(governance_db, FakeProvider(reply="a b"), budget=4.0)
    with TestClient(app) as client:
        first = client.post(
            "/v1/chat/completions",
            headers=HEADERS,
            json={"model": "fake-1", "messages": [{"role": "user", "content": "x y z"}]},
        )
        second = client.post(
            "/v1/chat/completions",
            headers=HEADERS,
            json={"model": "fake-1", "messages": [{"role": "user", "content": "x y z"}]},
        )

    assert first.status_code == 200
    assert second.status_code == 429
    error = second.json()["error"]
    assert error["code"] == "budget_exceeded"
    assert error["type"] == "insufficient_quota"
    assert "estimate" in error["message"]


def test_the_refusal_does_not_call_the_provider(governance_db):
    # Spend has to exist already: a cost is not knowable before the call, so
    # a key whose spend is zero always gets at least one request.
    metering.record_llm_turn(KEY_ID, "fake-1", 10, 10, db=governance_db)
    app, fake = build(governance_db, FakeProvider(reply="a b"), budget=1.0)
    with TestClient(app) as client:
        response = client.post(
            "/v1/chat/completions",
            headers=HEADERS,
            json={"model": "fake-1", "messages": [{"role": "user", "content": "x"}]},
        )
    assert response.status_code == 429
    assert fake.calls == []


def test_the_budget_applies_to_responses_as_well(governance_db):
    metering.record_llm_turn(KEY_ID, "fake-1", 10, 10, db=governance_db)
    app, _ = build(governance_db, FakeProvider(reply="a b"), budget=1.0)
    with TestClient(app) as client:
        response = client.post(
            "/v1/responses", headers=HEADERS, json={"model": "fake-1", "input": "x"}
        )
    assert response.status_code == 429
    assert response.json()["error"]["code"] == "budget_exceeded"


def test_the_budget_applies_to_streaming_as_well(governance_db):
    metering.record_llm_turn(KEY_ID, "fake-1", 10, 10, db=governance_db)
    app, _ = build(governance_db, FakeProvider(reply="a b"), budget=1.0)
    with TestClient(app) as client:
        response = client.post(
            "/v1/chat/completions",
            headers=HEADERS,
            json={"model": "fake-1", "messages": [{"role": "user", "content": "x"}],
                  "stream": True},
        )
    assert response.status_code == 429


def test_no_secret_reaches_storage_or_the_refusal(governance_db):
    metering.record_llm_turn(KEY_ID, "fake-1", 10, 10, db=governance_db)
    app, _ = build(governance_db, FakeProvider(reply="a b"), budget=1.0)
    with TestClient(app) as client:
        response = client.post(
            "/v1/chat/completions",
            headers=HEADERS,
            json={"model": "fake-1", "messages": [{"role": "user", "content": "x"}]},
        )
    assert "test-key" not in response.text
    assert b"test-key" not in governance_db.read_bytes()


def test_the_key_id_is_the_metering_user(governance_db):
    """Gateway usage lands in the same table govctl already reports."""
    app, _ = build(governance_db, FakeProvider(reply="a b"))
    with TestClient(app) as client:
        client.post(
            "/v1/chat/completions",
            headers=HEADERS,
            json={"model": "fake-1", "messages": [{"role": "user", "content": "x"}]},
        )

    report = metering.report(group="user", prices=prices(), db=governance_db)
    assert [row["user"] for row in report["rows"]] == [KEY_ID]
    assert "estimate" in report["cost_basis"]


def test_a_shipped_zero_price_table_can_never_exceed(governance_db):
    """The committed table is all zeros, so a budget built on it never trips."""
    zero_prices = metering.load_prices("prices.example.yaml")
    metering.record_llm_turn(KEY_ID, "gpt-4o-mini", 10_000_000, 10_000_000,
                             db=governance_db)
    state = budgets.check(
        KeyConfig(id=KEY_ID, secret="s", monthly_budget_usd=0.01),
        zero_prices,
        governance_db,
    )
    assert state.spent == 0.0
    assert not state.exceeded


def test_a_fresh_key_gets_one_call_before_any_refusal(governance_db):
    """Cost is not knowable before the call, so spend must exist to refuse.

    A key can therefore overshoot its budget by at most one request. That is
    stated in the README rather than hidden.
    """
    app, _ = build(governance_db, FakeProvider(reply="a b"), budget=0.000001)
    with TestClient(app) as client:
        first = client.post(
            "/v1/chat/completions",
            headers=HEADERS,
            json={"model": "fake-1", "messages": [{"role": "user", "content": "x"}]},
        )
        second = client.post(
            "/v1/chat/completions",
            headers=HEADERS,
            json={"model": "fake-1", "messages": [{"role": "user", "content": "x"}]},
        )

    assert first.status_code == 200
    assert second.status_code == 429
