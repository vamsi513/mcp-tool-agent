"""Delivery metrics computed in SQL over the ingested tables.

Five metrics, each with a deliberately simple definition stated in the result
so nobody has to guess what was measured. Every result carries the primary
keys of the rows it was computed from — pull request numbers or workflow run
ids — and those same identifiers are written to `dm_metric_computations`, so
a number can always be traced back to its inputs.

A metric with no qualifying rows returns a null value and a sample size of
zero. It never substitutes a default.
"""

from __future__ import annotations

import json
import os
from contextlib import closing
from datetime import UTC, datetime
from typing import Any

from store import connect

PR_METRICS = ("pr_cycle_time", "time_to_first_review", "review_count", "merge_frequency")
RUN_METRICS = ("workflow_failure_rate",)
METRICS = PR_METRICS + RUN_METRICS

DEFINITIONS = {
    "pr_cycle_time": (
        "Hours from pull request creation to merge, over merged pull requests "
        "whose merge falls in the window. Unmerged pull requests are excluded."
    ),
    "time_to_first_review": (
        "Hours from pull request creation to its earliest submitted review, "
        "over pull requests created in the window that received at least one "
        "review. Pull requests with no review are excluded."
    ),
    "review_count": (
        "Submitted reviews per pull request, over pull requests created in the "
        "window. Pull requests with no reviews count as zero."
    ),
    "workflow_failure_rate": (
        "Share of completed workflow runs created in the window whose "
        "conclusion is 'failure'. Runs that are cancelled, skipped or still "
        "in progress are excluded from both numerator and denominator."
    ),
    "merge_frequency": (
        "Merged pull requests per calendar week, bucketed by the Monday of the "
        "week the merge happened. Weeks with no merges do not appear."
    ),
}

UNITS = {
    "pr_cycle_time": "hours",
    "time_to_first_review": "hours",
    "review_count": "reviews per pull request",
    "workflow_failure_rate": "ratio",
    "merge_frequency": "merged pull requests per week",
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS dm_metric_computations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    computed_at TEXT    NOT NULL,
    repo        TEXT    NOT NULL,
    metric      TEXT    NOT NULL,
    since       TEXT,
    until       TEXT,
    value       REAL,
    sample_size INTEGER NOT NULL,
    source_kind TEXT    NOT NULL,
    source_ids  TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS dm_metric_repo_idx ON dm_metric_computations (repo, metric);
"""


class MetricError(Exception):
    """Raised for an unknown metric or a malformed window."""


def init(db: str | os.PathLike[str] | None = None) -> None:
    with closing(connect(db)) as conn, conn:
        conn.executescript(_SCHEMA)


def _window(since: str | None, until: str | None) -> tuple[str, str]:
    """Normalise the window to plain dates, defaulting to all of time."""
    for label, value in (("since", since), ("until", until)):
        if value:
            try:
                datetime.strptime(value[:10], "%Y-%m-%d")
            except ValueError as exc:
                raise MetricError(f"{label} must be YYYY-MM-DD, got {value!r}") from exc
    return (since or "0001-01-01")[:10], (until or "9999-12-31")[:10]


def _median(values: list[float]) -> float | None:
    """Median of an already-sorted-or-not list, or None when empty."""
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def repo_is_ingested(repo: str, db: str | os.PathLike[str] | None = None) -> bool:
    init(db)
    with closing(connect(db)) as conn:
        row = conn.execute("SELECT 1 FROM dm_repos WHERE repo = ?", (repo,)).fetchone()
    return row is not None


# Each query returns one row per unit of observation: an identifier plus the
# value being measured. Aggregation happens afterwards so the identifiers that
# produced a number stay attached to it.

_PR_CYCLE_TIME_SQL = """
SELECT number AS id,
       (julianday(merged_at) - julianday(created_at)) * 24.0 AS value
  FROM dm_pull_requests
 WHERE repo = ?
   AND merged_at IS NOT NULL
   AND created_at IS NOT NULL
   AND date(merged_at) BETWEEN ? AND ?
 ORDER BY number
"""

_TIME_TO_FIRST_REVIEW_SQL = """
SELECT p.number AS id,
       (julianday(MIN(v.submitted_at)) - julianday(p.created_at)) * 24.0 AS value
  FROM dm_pull_requests p
  JOIN dm_reviews v ON v.repo = p.repo AND v.pr_number = p.number
 WHERE p.repo = ?
   AND p.created_at IS NOT NULL
   AND v.submitted_at IS NOT NULL
   AND date(p.created_at) BETWEEN ? AND ?
 GROUP BY p.number, p.created_at
 ORDER BY p.number
"""

_REVIEW_COUNT_SQL = """
SELECT p.number AS id,
       (SELECT COUNT(*) FROM dm_reviews v
         WHERE v.repo = p.repo AND v.pr_number = p.number) * 1.0 AS value
  FROM dm_pull_requests p
 WHERE p.repo = ?
   AND p.created_at IS NOT NULL
   AND date(p.created_at) BETWEEN ? AND ?
 ORDER BY p.number
"""

_WORKFLOW_FAILURE_SQL = """
SELECT run_id AS id,
       CASE WHEN conclusion = 'failure' THEN 1.0 ELSE 0.0 END AS value
  FROM dm_workflow_runs
 WHERE repo = ?
   AND conclusion IN ('success', 'failure')
   AND created_at IS NOT NULL
   AND date(created_at) BETWEEN ? AND ?
 ORDER BY run_id
"""

_MERGE_FREQUENCY_SQL = """
SELECT number AS id,
       date(merged_at, 'weekday 1', '-7 days') AS bucket
  FROM dm_pull_requests
 WHERE repo = ?
   AND merged_at IS NOT NULL
   AND date(merged_at) BETWEEN ? AND ?
 ORDER BY bucket, number
"""

_SQL = {
    "pr_cycle_time": _PR_CYCLE_TIME_SQL,
    "time_to_first_review": _TIME_TO_FIRST_REVIEW_SQL,
    "review_count": _REVIEW_COUNT_SQL,
    "workflow_failure_rate": _WORKFLOW_FAILURE_SQL,
    "merge_frequency": _MERGE_FREQUENCY_SQL,
}


def _record_computation(
    repo: str,
    metric: str,
    since: str,
    until: str,
    value: float | None,
    sample_size: int,
    source_kind: str,
    source_ids: list[int],
    db: str | os.PathLike[str] | None,
) -> None:
    with closing(connect(db)) as conn, conn:
        conn.execute(
            "INSERT INTO dm_metric_computations"
            " (computed_at, repo, metric, since, until, value, sample_size,"
            "  source_kind, source_ids)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                datetime.now(UTC).isoformat(timespec="seconds"),
                repo,
                metric,
                since,
                until,
                value,
                sample_size,
                source_kind,
                json.dumps(source_ids),
            ),
        )


def compute(
    repo: str,
    metric: str,
    since: str | None = None,
    until: str | None = None,
    db: str | os.PathLike[str] | None = None,
) -> dict:
    """Compute one metric for one repository over an optional date window."""
    if metric not in METRICS:
        raise MetricError(f"unknown metric {metric!r}; expected one of {', '.join(METRICS)}")
    lower, upper = _window(since, until)
    init(db)

    with closing(connect(db)) as conn:
        rows = conn.execute(_SQL[metric], (repo, lower, upper)).fetchall()

    source_kind = "workflow_runs" if metric in RUN_METRICS else "pull_requests"
    result: dict[str, Any] = {
        "repo": repo,
        "metric": metric,
        "definition": DEFINITIONS[metric],
        "unit": UNITS[metric],
        "window": {"since": since or None, "until": until or None},
        "sample_size": len(rows),
        "sources": {source_kind: [int(row["id"]) for row in rows]},
    }

    if metric == "merge_frequency":
        buckets: dict[str, list[int]] = {}
        for row in rows:
            buckets.setdefault(str(row["bucket"]), []).append(int(row["id"]))
        per_week = [
            {"week_starting": week, "merges": len(numbers), "pull_requests": numbers}
            for week, numbers in sorted(buckets.items())
        ]
        value = (len(rows) / len(per_week)) if per_week else None
        result["value"] = round(value, 4) if value is not None else None
        result["weeks"] = per_week
        result["total_merges"] = len(rows)
    elif metric == "workflow_failure_rate":
        failures = [int(row["id"]) for row in rows if float(row["value"]) == 1.0]
        value = (len(failures) / len(rows)) if rows else None
        result["value"] = round(value, 4) if value is not None else None
        result["failures"] = len(failures)
        result["completed_runs"] = len(rows)
        result["sources"] = {"workflow_runs": [int(r["id"]) for r in rows],
                             "failed_runs": failures}
    else:
        values = [float(row["value"]) for row in rows]
        median = _median(values)
        result["value"] = round(median, 4) if median is not None else None
        result["mean"] = round(sum(values) / len(values), 4) if values else None
        result["min"] = round(min(values), 4) if values else None
        result["max"] = round(max(values), 4) if values else None
        result["statistic"] = "median"
        result["per_pull_request"] = [
            {"number": int(row["id"]), "value": round(float(row["value"]), 4)} for row in rows
        ]

    _record_computation(
        repo,
        metric,
        lower,
        upper,
        result["value"],
        len(rows),
        source_kind,
        [int(row["id"]) for row in rows],
        db,
    )
    return result


def recent_computations(
    repo: str | None = None, limit: int = 20, db: str | os.PathLike[str] | None = None
) -> list[dict]:
    """The recorded metric computations, newest first."""
    init(db)
    sql = "SELECT * FROM dm_metric_computations"
    args: list[Any] = []
    if repo:
        sql += " WHERE repo = ?"
        args.append(repo)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(max(1, limit))
    with closing(connect(db)) as conn:
        rows = conn.execute(sql, args).fetchall()
    return [{**dict(row), "source_ids": json.loads(row["source_ids"])} for row in rows]
