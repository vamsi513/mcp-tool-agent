"""Shared scoring for the delivery question eval.

Every expected number is derived here from tests/independent_metrics.py,
which reads the recorded fixture and uses plain Python. Nothing in this file
calls delivery_metrics, so a wrong query cannot agree with itself.
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from typing import Any

import yaml

from tests import independent_metrics as im

QUESTIONS_PATH = Path(__file__).resolve().parent.parent / "evals" / "delivery_questions.yaml"


def load_questions() -> tuple[str, list[dict]]:
    document = yaml.safe_load(QUESTIONS_PATH.read_text())
    return document["repo"], document["questions"]


def expectations() -> dict[str, Any]:
    """Independently computed values the questions refer to by name."""
    cycle = im.cycle_times()
    ttfr = im.times_to_first_review()
    reviews = im.review_counts()
    completed, failed = im.workflow_outcomes()
    weeks = im.merges_by_week()
    first_week = sorted(weeks)[0] if weeks else None

    return {
        "cycle_time_median": im.median(cycle.values()),
        "cycle_time_mean": im.mean(cycle.values()),
        "cycle_time_n": len(cycle),
        "cycle_time_prs": sorted(cycle),
        "first_cycle_time_pr": sorted(cycle)[0] if cycle else None,
        "ttfr_median": im.median(ttfr.values()),
        "ttfr_n": len(ttfr),
        "ttfr_prs": sorted(ttfr),
        "review_count_median": im.median(reviews.values()),
        "review_count_mean": im.mean(reviews.values()),
        "review_count_n": len(reviews),
        "failure_rate": round(len(failed) / len(completed), 4) if completed else None,
        "failure_count": len(failed),
        "completed_run_count": len(completed),
        "failed_run_ids": sorted(failed),
        "merges_per_week": round(sum(len(v) for v in weeks.values()) / len(weeks), 4)
        if weeks
        else None,
        "total_merges": sum(len(v) for v in weeks.values()),
        "merge_weeks": weeks,
        "first_week_merge_count": len(weeks[first_week]) if first_week else 0,
        "ingested_repo_names": [load_questions()[0]],
    }


def resolve_arguments(arguments: dict[str, Any], expected: dict[str, Any]) -> dict[str, Any]:
    """Replace the placeholder tokens a question uses with real values."""
    weeks = expected["merge_weeks"]
    first_week = sorted(weeks)[0] if weeks else None
    substitutions = {
        "FIRST_WEEK_START": first_week,
        "FIRST_WEEK_END": (
            (date.fromisoformat(first_week) + timedelta(days=6)).isoformat()
            if first_week
            else None
        ),
        "FIRST_CYCLE_TIME_PR": expected["first_cycle_time_pr"],
    }
    return {key: substitutions.get(value, value) if isinstance(value, str) else value
            for key, value in arguments.items()}


def call_tool(tool: str, arguments: dict[str, Any]) -> dict:
    import delivery_tools

    if tool == "list_delivery_repos":
        return delivery_tools.list_delivery_repos()
    if tool == "delivery_metric":
        return delivery_tools.delivery_metric(
            arguments["repo"],
            arguments["metric"],
            arguments.get("since", "") or "",
            arguments.get("until", "") or "",
        )
    if tool == "pull_request_detail":
        return delivery_tools.pull_request_detail(arguments["repo"], arguments["number"])
    raise AssertionError(f"question names an unknown tool: {tool}")


def check(question: dict, result: dict, expected: dict[str, Any]) -> list[str]:
    """Return a list of failure descriptions; empty means the question passed."""
    failures: list[str] = []
    want = question["expect"]

    def compare(label: str, got: Any, wanted: Any) -> None:
        if got != wanted:
            failures.append(f"{label}: expected {wanted!r}, got {got!r}")

    if "refused" in want:
        compare("refused", result.get("refused"), want["refused"])

    if "reason_contains" in want:
        reason = str(result.get("reason", ""))
        if want["reason_contains"] not in reason:
            failures.append(
                f"reason: expected it to mention {want['reason_contains']!r}, got {reason!r}"
            )
        # A refusal must not smuggle a number in.
        if result.get("value") is not None:
            failures.append(f"refusal carried a value: {result.get('value')!r}")

    for field in ("value", "mean", "sample_size", "failures", "completed_runs",
                  "total_merges"):
        if field in want:
            compare(field, result.get(field), expected[want[field]])

    if "unit" in want:
        compare("unit", result.get("unit"), want["unit"])

    if "citations" in want:
        for kind, name in want["citations"].items():
            compare(f"citations.{kind}", (result.get("citations") or {}).get(kind),
                    expected[name])

    if "weeks" in want:
        got_weeks = {w["week_starting"]: sorted(w["pull_requests"])
                     for w in result.get("weeks", [])}
        compare("weeks", got_weeks, expected[want["weeks"]])

    if "repos" in want:
        got_repos = [row["repo"] for row in result.get("repos", [])]
        compare("repos", got_repos, expected[want["repos"]])

    if "pull_request_number" in want:
        got_number = (result.get("pull_request") or {}).get("number")
        compare("pull_request.number", got_number, expected[want["pull_request_number"]])

    # Every non-refused metric answer has to cite something.
    if want.get("refused") is False and question["tool"] == "delivery_metric":
        citations = result.get("citations") or {}
        if not any(citations.get(kind) for kind in
                   ("pull_requests", "workflow_runs", "failed_runs")):
            failures.append("answer carried no citations")

    return failures


def run_all() -> list[dict]:
    """Score every question against the tools. No model involved."""
    _, questions = load_questions()
    expected = expectations()
    results = []
    for question in questions:
        arguments = resolve_arguments(question.get("arguments") or {}, expected)
        result = call_tool(question["tool"], arguments)
        failures = check(question, result, expected)
        results.append(
            {
                "id": question["id"],
                "question": question["question"],
                "tool": question["tool"],
                "passed": not failures,
                "failures": failures,
            }
        )
    return results
