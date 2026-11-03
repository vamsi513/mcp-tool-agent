"""Agent tools over the ingested delivery metrics. Read-only.

Three tools. Every successful answer carries the pull request numbers or
workflow run ids it was computed from, so a claim can be checked against the
source rows. Two things are refused rather than guessed:

  * a repository that has not been ingested — the tool says so and names what
    has been, instead of answering about data it does not have;
  * a window with no qualifying rows — the tool reports sample_size 0 and a
    null value rather than returning zero as though it were measured.

Refusals come back as ordinary results with `refused: true`, so the model can
read the reason and tell the user, which is more useful than an exception.
"""

from __future__ import annotations

from contextlib import closing
from typing import Any

from delivery_ingest import ingested_repos
from delivery_metrics import (
    DEFINITIONS,
    METRICS,
    MetricError,
    compute,
    repo_is_ingested,
)
from store import connect

MAX_CITED = 50

# Keys that are detail the model does not need in order to answer. Dropping
# per_pull_request in particular stops the model recomputing a statistic from
# the row values instead of quoting the one the metric already computed.
DROPPED_FROM_PAYLOAD = ("per_pull_request",)


def _refusal(reason: str, **extra: Any) -> dict:
    return {"refused": True, "reason": reason, **extra}


def _summarise(result: dict) -> str:
    """One sentence stating the figure, its unit and the sample it came from.

    Pre-rendered so an answer can quote it rather than deriving a number of
    its own from the underlying rows.
    """
    metric = result["metric"]
    unit = result["unit"]
    size = result["sample_size"]

    if metric == "workflow_failure_rate":
        return (
            f"{result['failures']} of {result['completed_runs']} completed "
            f"workflow runs failed, a failure rate of {result['value']}"
        )
    if metric == "merge_frequency":
        weeks = len(result.get("weeks") or [])
        return (
            f"{result['total_merges']} pull requests were merged across "
            f"{weeks} week(s), {result['value']} {unit}"
        )

    statistic = result.get("statistic", "value")
    rows = "pull request" if size == 1 else "pull requests"
    return (
        f"the {statistic} {metric} is {result['value']} {unit}, "
        f"over a sample of {size} {rows}"
    )


def _cite(sources: dict[str, list[int]]) -> dict[str, Any]:
    """Trim citation lists to something a prompt can carry, honestly labelled."""
    citations: dict[str, Any] = {}
    for kind, ids in sources.items():
        citations[kind] = ids[:MAX_CITED]
        if len(ids) > MAX_CITED:
            citations[f"{kind}_truncated"] = {
                "shown": MAX_CITED,
                "total": len(ids),
                "note": "cited identifiers truncated; the metric used all of them",
            }
    return citations


def list_delivery_repos() -> dict:
    """List repositories whose delivery data has been ingested.

    Call this first when you are unsure whether a repository can be reported
    on. The counts are the rows actually stored, not an estimate.
    """
    repos = ingested_repos()
    return {
        "count": len(repos),
        "repos": repos,
        "available_metrics": list(METRICS),
        "note": (
            "Only these repositories can be reported on. Anything else has not "
            "been ingested and must not be answered from memory."
        ),
    }


def delivery_metric(
    repo: str, metric: str, since: str = "", until: str = ""
) -> dict:
    """Compute one delivery metric for an ingested repository.

    `metric` is one of pr_cycle_time, time_to_first_review, review_count,
    workflow_failure_rate, merge_frequency. `since` and `until` are optional
    YYYY-MM-DD bounds on the event each metric is keyed to.

    The result carries a `summary` sentence stating the figure, its unit and
    the sample size: report that figure as given rather than deriving one of
    your own. It also states the metric's definition and the pull request
    numbers or workflow run ids it came from; always quote those identifiers
    when reporting the number. If the result has `refused: true`, say so and
    do not supply a figure of your own.
    """
    if metric not in METRICS:
        return _refusal(
            f"unknown metric {metric!r}",
            available_metrics=list(METRICS),
        )

    if not repo_is_ingested(repo):
        return _refusal(
            f"{repo} has not been ingested, so there is no data to report",
            ingested_repos=[row["repo"] for row in ingested_repos()],
            remedy="run the ingest for this repository first",
        )

    try:
        result = compute(repo, metric, since or None, until or None)
    except MetricError as exc:
        return _refusal(str(exc))

    if result["sample_size"] == 0:
        return _refusal(
            "no rows match this metric and window, so there is nothing to report",
            repo=repo,
            metric=metric,
            definition=DEFINITIONS[metric],
            window=result["window"],
            sample_size=0,
            value=None,
        )

    result["summary"] = _summarise(result)
    result["citations"] = _cite(result["sources"])
    result["refused"] = False
    result.pop("sources", None)
    for key in DROPPED_FROM_PAYLOAD:
        result.pop(key, None)
    return result


def pull_request_detail(repo: str, number: int) -> dict:
    """Look up one stored pull request and its reviews.

    Use this to check a pull request number that a metric cited.
    """
    if not repo_is_ingested(repo):
        return _refusal(f"{repo} has not been ingested")

    with closing(connect()) as conn:
        pull = conn.execute(
            "SELECT number, title, state, author, created_at, merged_at, closed_at"
            " FROM dm_pull_requests WHERE repo = ? AND number = ?",
            (repo, number),
        ).fetchone()
        if pull is None:
            return _refusal(
                f"pull request {number} is not in the ingested data for {repo}"
            )
        reviews = conn.execute(
            "SELECT review_id, reviewer, state, submitted_at FROM dm_reviews"
            " WHERE repo = ? AND pr_number = ? ORDER BY submitted_at",
            (repo, number),
        ).fetchall()

    return {
        "refused": False,
        "repo": repo,
        "pull_request": dict(pull),
        "review_count": len(reviews),
        "reviews": [dict(row) for row in reviews],
    }


TOOLS = (list_delivery_repos, delivery_metric, pull_request_detail)
