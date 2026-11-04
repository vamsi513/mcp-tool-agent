"""Expected metric values computed from the raw fixture, without SQL.

This deliberately shares nothing with delivery_metrics: it reads the recorded
JSON directly and uses datetime and statistics. If a query in delivery_metrics
is wrong, these numbers will disagree with it.
"""

from __future__ import annotations

import json
import statistics
from datetime import datetime, timedelta
from pathlib import Path

FIXTURE = Path(__file__).parent / "fixtures" / "github_delivery.json"
STAMP = "%Y-%m-%dT%H:%M:%SZ"


def _load() -> dict:
    return json.loads(FIXTURE.read_text())["responses"]


def _hours(start: str, end: str) -> float:
    return (
        datetime.strptime(end, STAMP) - datetime.strptime(start, STAMP)
    ).total_seconds() / 3600.0


def raw(max_prs: int = 12, max_runs: int = 50) -> dict:
    """The fixture's pull requests, reviews and runs as the ingest would keep them."""
    responses = _load()

    pulls = next(
        value["json"] for key, value in responses.items() if "/pulls?" in key
    )[:max_prs]

    reviews: dict[int, list[dict]] = {}
    for key, value in responses.items():
        if "/reviews" not in key:
            continue
        number = int(key.split("/pulls/")[1].split("/reviews")[0])
        reviews[number] = value["json"] or []

    runs = next(
        value["json"]["workflow_runs"]
        for key, value in responses.items()
        if "/actions/runs" in key
    )[:max_runs]

    kept = {pull["number"] for pull in pulls}
    return {
        "pulls": pulls,
        "reviews": {n: r for n, r in reviews.items() if n in kept},
        "runs": runs,
    }


def _in_window(stamp: str, since: str | None, until: str | None) -> bool:
    day = stamp[:10]
    if since and day < since:
        return False
    if until and day > until:
        return False
    return True


def cycle_times(since: str | None = None, until: str | None = None) -> dict[int, float]:
    data = raw()
    return {
        p["number"]: _hours(p["created_at"], p["merged_at"])
        for p in data["pulls"]
        if p.get("merged_at") and _in_window(p["merged_at"], since, until)
    }


def times_to_first_review(
    since: str | None = None, until: str | None = None
) -> dict[int, float]:
    data = raw()
    out: dict[int, float] = {}
    for pull in data["pulls"]:
        if not _in_window(pull["created_at"], since, until):
            continue
        submitted = [
            r["submitted_at"] for r in data["reviews"].get(pull["number"], [])
            if r.get("submitted_at")
        ]
        if submitted:
            out[pull["number"]] = _hours(pull["created_at"], min(submitted))
    return out


def review_counts(since: str | None = None, until: str | None = None) -> dict[int, int]:
    data = raw()
    return {
        p["number"]: len(data["reviews"].get(p["number"], []))
        for p in data["pulls"]
        if _in_window(p["created_at"], since, until)
    }


def workflow_outcomes(
    since: str | None = None, until: str | None = None
) -> tuple[list[int], list[int]]:
    """(completed run ids, failed run ids) — cancelled and skipped excluded."""
    data = raw()
    completed, failed = [], []
    for run in data["runs"]:
        if run.get("conclusion") not in ("success", "failure"):
            continue
        if not _in_window(run["created_at"], since, until):
            continue
        completed.append(run["id"])
        if run["conclusion"] == "failure":
            failed.append(run["id"])
    return completed, failed


def merges_by_week(since: str | None = None, until: str | None = None) -> dict[str, list[int]]:
    """Merged pull request numbers keyed by the Monday of the merge week."""
    data = raw()
    weeks: dict[str, list[int]] = {}
    for pull in data["pulls"]:
        merged = pull.get("merged_at")
        if not merged or not _in_window(merged, since, until):
            continue
        day = datetime.strptime(merged, STAMP)
        monday = (day - timedelta(days=day.weekday())).date().isoformat()
        weeks.setdefault(monday, []).append(pull["number"])
    return {week: sorted(numbers) for week, numbers in sorted(weeks.items())}


def median(values) -> float | None:
    values = list(values)
    return round(statistics.median(values), 4) if values else None


def mean(values) -> float | None:
    values = list(values)
    return round(statistics.fmean(values), 4) if values else None
