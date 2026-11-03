"""Every SQL metric is checked against a value computed independently."""

import sqlite3

import pytest

import delivery_metrics as dm
from tests import independent_metrics as im
from tests.conftest import FIXTURE_REPO


def test_pr_cycle_time_matches_independent(delivery_db):
    expected = im.cycle_times()
    result = dm.compute(FIXTURE_REPO, "pr_cycle_time", db=delivery_db)

    assert result["value"] == im.median(expected.values())
    assert result["mean"] == im.mean(expected.values())
    assert result["sample_size"] == len(expected)
    assert result["sources"]["pull_requests"] == sorted(expected)
    assert result["unit"] == "hours"
    assert result["statistic"] == "median"


def test_time_to_first_review_matches_independent(delivery_db):
    expected = im.times_to_first_review()
    result = dm.compute(FIXTURE_REPO, "time_to_first_review", db=delivery_db)

    assert result["value"] == im.median(expected.values())
    assert result["sample_size"] == len(expected)
    assert result["sources"]["pull_requests"] == sorted(expected)


def test_review_count_matches_independent(delivery_db):
    expected = im.review_counts()
    result = dm.compute(FIXTURE_REPO, "review_count", db=delivery_db)

    assert result["value"] == im.median(expected.values())
    assert result["mean"] == im.mean(expected.values())
    assert result["sample_size"] == len(expected)


def test_workflow_failure_rate_matches_independent(delivery_db):
    completed, failed = im.workflow_outcomes()
    result = dm.compute(FIXTURE_REPO, "workflow_failure_rate", db=delivery_db)

    assert result["completed_runs"] == len(completed)
    assert result["failures"] == len(failed)
    assert result["value"] == round(len(failed) / len(completed), 4)
    assert result["sources"]["failed_runs"] == sorted(failed)


def test_merge_frequency_matches_independent(delivery_db):
    expected = im.merges_by_week()
    result = dm.compute(FIXTURE_REPO, "merge_frequency", db=delivery_db)

    got = {w["week_starting"]: sorted(w["pull_requests"]) for w in result["weeks"]}
    assert got == expected
    assert result["total_merges"] == sum(len(v) for v in expected.values())
    assert result["value"] == round(result["total_merges"] / len(expected), 4)


def test_merge_week_bucket_handles_a_monday_merge(governance_db):
    """A pull request merged on a Monday belongs to that Monday's week."""
    dm.init(governance_db)
    conn = sqlite3.connect(governance_db)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS dm_pull_requests (
            repo TEXT, number INTEGER, title TEXT, state TEXT, author TEXT,
            created_at TEXT, merged_at TEXT, closed_at TEXT,
            PRIMARY KEY (repo, number));
        CREATE TABLE IF NOT EXISTS dm_repos (
            repo TEXT PRIMARY KEY, first_seen_at TEXT, ingested_at TEXT, complete INTEGER);
        """
    )
    conn.execute("INSERT INTO dm_repos VALUES ('x/y','t','t',1)")
    # 2026-10-19 is a Monday; 2026-10-20 is the Tuesday after it.
    conn.executemany(
        "INSERT INTO dm_pull_requests (repo, number, created_at, merged_at)"
        " VALUES ('x/y', ?, ?, ?)",
        [
            (1, "2026-10-19T08:00:00Z", "2026-10-19T09:00:00Z"),
            (2, "2026-10-20T08:00:00Z", "2026-10-20T09:00:00Z"),
        ],
    )
    conn.commit()
    conn.close()

    result = dm.compute("x/y", "merge_frequency", db=governance_db)
    weeks = {w["week_starting"]: sorted(w["pull_requests"]) for w in result["weeks"]}
    assert weeks == {"2026-10-19": [1, 2]}


def test_window_filters_rows(delivery_db):
    all_weeks = im.merges_by_week()
    first_week = sorted(all_weeks)[0]

    result = dm.compute(
        FIXTURE_REPO, "merge_frequency", since=first_week, until=first_week_end(first_week),
        db=delivery_db,
    )
    got = {w["week_starting"]: sorted(w["pull_requests"]) for w in result["weeks"]}
    assert got == {first_week: all_weeks[first_week]}


def first_week_end(monday: str) -> str:
    from datetime import date, timedelta

    return (date.fromisoformat(monday) + timedelta(days=6)).isoformat()


def test_empty_window_returns_null_not_zero(delivery_db):
    result = dm.compute(
        FIXTURE_REPO, "pr_cycle_time", since="1990-01-01", until="1990-12-31", db=delivery_db
    )
    assert result["value"] is None
    assert result["sample_size"] == 0
    assert result["sources"]["pull_requests"] == []


def test_unknown_repo_has_no_rows(delivery_db):
    result = dm.compute("nobody/nothing", "pr_cycle_time", db=delivery_db)
    assert result["sample_size"] == 0
    assert result["value"] is None


def test_unknown_metric_raises(delivery_db):
    with pytest.raises(dm.MetricError, match="unknown metric"):
        dm.compute(FIXTURE_REPO, "velocity", db=delivery_db)


def test_bad_window_raises(delivery_db):
    with pytest.raises(dm.MetricError, match="YYYY-MM-DD"):
        dm.compute(FIXTURE_REPO, "pr_cycle_time", since="last tuesday", db=delivery_db)


def test_every_metric_records_its_source_rows(delivery_db):
    for metric in dm.METRICS:
        dm.compute(FIXTURE_REPO, metric, db=delivery_db)

    recorded = dm.recent_computations(FIXTURE_REPO, limit=len(dm.METRICS), db=delivery_db)
    by_metric = {row["metric"]: row for row in recorded}

    assert set(by_metric) == set(dm.METRICS)
    for metric, row in by_metric.items():
        live = dm.compute(FIXTURE_REPO, metric, db=delivery_db)
        assert row["sample_size"] == live["sample_size"]
        assert len(row["source_ids"]) == live["sample_size"], metric


def test_definitions_exist_for_every_metric():
    assert set(dm.DEFINITIONS) == set(dm.METRICS)
    assert set(dm.UNITS) == set(dm.METRICS)
    for text in dm.DEFINITIONS.values():
        assert len(text) > 40
