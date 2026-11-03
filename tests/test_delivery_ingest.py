"""Ingest behaviour against recorded GitHub responses. No network."""

import sqlite3

import pytest

import delivery_ingest
from connector_github import ConnectorError
from tests.conftest import FIXTURE_MAX_PRS, FIXTURE_MAX_RUNS, FIXTURE_REPO


def _count(db, table, repo=FIXTURE_REPO):
    conn = sqlite3.connect(db)
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table} WHERE repo = ?", (repo,)).fetchone()[0]
    finally:
        conn.close()


def test_ingest_writes_pulls_reviews_and_runs(governance_db, delivery_api):
    summary = delivery_ingest.ingest(
        FIXTURE_REPO, max_prs=FIXTURE_MAX_PRS, max_runs=FIXTURE_MAX_RUNS, db=governance_db
    )

    assert summary["repo"] == FIXTURE_REPO
    assert summary["complete"] is True
    assert summary["pull_requests"] == 12
    assert summary["reviews"] == 8
    assert summary["workflow_runs"] == 50

    assert _count(governance_db, "dm_pull_requests") == 12
    assert _count(governance_db, "dm_reviews") == 8
    assert _count(governance_db, "dm_workflow_runs") == 50


def test_ingest_is_idempotent(delivery_db):
    """A second pass over the same data must update, not duplicate."""
    before = (
        _count(delivery_db, "dm_pull_requests"),
        _count(delivery_db, "dm_reviews"),
        _count(delivery_db, "dm_workflow_runs"),
    )

    delivery_ingest.ingest(
        FIXTURE_REPO, max_prs=FIXTURE_MAX_PRS, max_runs=FIXTURE_MAX_RUNS, db=delivery_db
    )
    delivery_ingest.ingest(
        FIXTURE_REPO, max_prs=FIXTURE_MAX_PRS, max_runs=FIXTURE_MAX_RUNS, db=delivery_db
    )

    after = (
        _count(delivery_db, "dm_pull_requests"),
        _count(delivery_db, "dm_reviews"),
        _count(delivery_db, "dm_workflow_runs"),
    )
    assert before == after


def test_reingest_updates_a_changed_row(delivery_db):
    conn = sqlite3.connect(delivery_db)
    conn.execute(
        "UPDATE dm_pull_requests SET title = 'stale' WHERE repo = ? AND number = 466",
        (FIXTURE_REPO,),
    )
    conn.commit()
    conn.close()

    delivery_ingest.ingest(
        FIXTURE_REPO, max_prs=FIXTURE_MAX_PRS, max_runs=FIXTURE_MAX_RUNS, db=delivery_db
    )

    conn = sqlite3.connect(delivery_db)
    title = conn.execute(
        "SELECT title FROM dm_pull_requests WHERE repo = ? AND number = 466", (FIXTURE_REPO,)
    ).fetchone()[0]
    conn.close()
    assert title != "stale"


def test_ingested_repos_reports_counts(delivery_db):
    rows = delivery_ingest.ingested_repos(delivery_db)
    assert len(rows) == 1
    row = rows[0]
    assert row["repo"] == FIXTURE_REPO
    assert row["pull_requests"] == 12
    assert row["reviews"] == 8
    assert row["workflow_runs"] == 50
    assert row["complete"] is True


def test_bad_repo_name_is_rejected(governance_db, delivery_api):
    with pytest.raises(ConnectorError, match="owner/name"):
        delivery_ingest.ingest("not-a-repo", db=governance_db)


def test_per_page_never_exceeds_what_is_kept(governance_db, delivery_api, monkeypatch):
    """max_items bounds the page size, so a small run fetches a small page."""
    seen = {}

    real = delivery_ingest._get_pages

    def spy(path, params, max_items):
        seen["max_items"] = max_items
        return real(path, params, max_items)

    monkeypatch.setattr(delivery_ingest, "_get_pages", spy)
    delivery_ingest.ingest(FIXTURE_REPO, max_prs=12, max_runs=50, db=governance_db)
    assert seen["max_items"] == 12


def test_rate_limit_floor_stops_the_run(governance_db, monkeypatch):
    """A near-exhausted quota stops ingest and marks the repo incomplete."""
    import httpx

    def handler(request):
        return httpx.Response(
            200,
            json=[],
            headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1700000000"},
        )

    transport = httpx.MockTransport(handler)
    real_client = httpx.Client
    monkeypatch.setattr(
        delivery_ingest.httpx,
        "Client",
        lambda *a, **k: real_client(*a, **{**k, "transport": transport}),
    )

    summary = delivery_ingest.ingest(FIXTURE_REPO, db=governance_db)

    assert summary["complete"] is False
    assert "rate limit" in summary["stopped_early"] or "requests left" in summary["stopped_early"]
    rows = delivery_ingest.ingested_repos(governance_db)
    assert rows[0]["complete"] is False


def test_errors_never_carry_the_token(governance_db, monkeypatch):
    import httpx

    monkeypatch.setenv("GITHUB_TOKEN", "ghp_notarealtokenvalue")
    transport = httpx.MockTransport(
        lambda request: httpx.Response(401, json={"message": "Bad credentials"})
    )
    real_client = httpx.Client
    monkeypatch.setattr(
        delivery_ingest.httpx,
        "Client",
        lambda *a, **k: real_client(*a, **{**k, "transport": transport}),
    )

    with pytest.raises(ConnectorError) as excinfo:
        delivery_ingest.ingest(FIXTURE_REPO, db=governance_db)

    assert "ghp_notarealtokenvalue" not in str(excinfo.value)
