"""Read-only ingest of delivery data for one public GitHub repository.

Pulls pull requests, their reviews, and workflow runs through the REST API
and stores them in the shared SQLite database. Nothing here writes to GitHub.

Re-running is safe: every row is written with an UPSERT keyed on its GitHub
identifier, so a second pass over the same data updates in place rather than
duplicating. That also makes a partial run recoverable — if the rate limit is
hit halfway, what was fetched is kept and the next run fills in the rest.

The token is read from GITHUB_TOKEN by the connector's header helper and is
never logged, returned, or included in an error.
"""

from __future__ import annotations

import os
import sys
from contextlib import closing
from datetime import UTC, datetime
from typing import Any

import httpx

from connector_github import (
    ConnectorError,
    _headers,
    _raise_for_status,
    _validate_repo,
    api_base,
)
from store import connect

TIMEOUT_SECONDS = 30
PER_PAGE = 100

DEFAULT_MAX_PRS = 100
DEFAULT_MAX_RUNS = 200

# Stop before the quota is actually exhausted so a run never ends on a 403.
RATE_LIMIT_FLOOR = 2

_SCHEMA = """
CREATE TABLE IF NOT EXISTS dm_repos (
    repo          TEXT PRIMARY KEY,
    first_seen_at TEXT NOT NULL,
    ingested_at   TEXT NOT NULL,
    complete      INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS dm_pull_requests (
    repo       TEXT    NOT NULL,
    number     INTEGER NOT NULL,
    title      TEXT,
    state      TEXT,
    author     TEXT,
    created_at TEXT,
    merged_at  TEXT,
    closed_at  TEXT,
    PRIMARY KEY (repo, number)
);

CREATE TABLE IF NOT EXISTS dm_reviews (
    repo         TEXT    NOT NULL,
    review_id    INTEGER NOT NULL,
    pr_number    INTEGER NOT NULL,
    reviewer     TEXT,
    state        TEXT,
    submitted_at TEXT,
    PRIMARY KEY (repo, review_id)
);

CREATE TABLE IF NOT EXISTS dm_workflow_runs (
    repo         TEXT    NOT NULL,
    run_id       INTEGER NOT NULL,
    name         TEXT,
    status       TEXT,
    conclusion   TEXT,
    event        TEXT,
    head_branch  TEXT,
    created_at   TEXT,
    PRIMARY KEY (repo, run_id)
);

CREATE INDEX IF NOT EXISTS dm_pr_repo_idx     ON dm_pull_requests (repo);
CREATE INDEX IF NOT EXISTS dm_pr_merged_idx   ON dm_pull_requests (repo, merged_at);
CREATE INDEX IF NOT EXISTS dm_reviews_pr_idx  ON dm_reviews (repo, pr_number);
CREATE INDEX IF NOT EXISTS dm_runs_repo_idx   ON dm_workflow_runs (repo, created_at);
"""


def init(db: str | os.PathLike[str] | None = None) -> None:
    """Create the delivery tables if they are not there yet."""
    with closing(connect(db)) as conn, conn:
        conn.executescript(_SCHEMA)


class RateLimited(ConnectorError):
    """Raised when the remaining quota is too low to keep going."""


def _check_quota(resp: httpx.Response) -> None:
    remaining = resp.headers.get("x-ratelimit-remaining")
    if remaining is None or not remaining.isdigit():
        return
    if int(remaining) <= RATE_LIMIT_FLOOR:
        reset = resp.headers.get("x-ratelimit-reset", "")
        when = ""
        if reset.isdigit():
            stamp = datetime.fromtimestamp(int(reset), tz=UTC).isoformat(timespec="seconds")
            when = f" (resets at {stamp})"
        raise RateLimited(
            f"stopping with {remaining} API requests left{when}; "
            "set GITHUB_TOKEN to raise the limit and re-run to continue"
        )


def _get_pages(path: str, params: dict[str, Any], max_items: int) -> list[dict]:
    """Follow Link-header pagination until `max_items` or the last page."""
    items: list[dict] = []
    url = f"{api_base()}{path}"
    # Never ask for more rows than the caller will keep.
    query: dict[str, Any] | None = {**params, "per_page": min(PER_PAGE, max(1, max_items))}

    with httpx.Client(timeout=TIMEOUT_SECONDS) as client:
        while url and len(items) < max_items:
            try:
                resp = client.get(url, headers=_headers(), params=query)
            except httpx.RequestError as exc:
                raise ConnectorError(
                    f"request to GitHub failed: {exc.__class__.__name__}"
                ) from exc
            _raise_for_status(resp)

            payload = resp.json()
            batch = payload if isinstance(payload, list) else []
            items.extend(item for item in batch if isinstance(item, dict))

            _check_quota(resp)
            if "next" not in resp.links:
                break
            url = resp.links["next"]["url"]
            query = None  # the next link already carries the query string

    return items[:max_items]


def _get_one(path: str, params: dict[str, Any] | None = None) -> Any:
    with httpx.Client(timeout=TIMEOUT_SECONDS) as client:
        try:
            resp = client.get(f"{api_base()}{path}", headers=_headers(), params=params)
        except httpx.RequestError as exc:
            raise ConnectorError(f"request to GitHub failed: {exc.__class__.__name__}") from exc
    _raise_for_status(resp)
    _check_quota(resp)
    return resp.json()


def _login(value: Any) -> str | None:
    if isinstance(value, dict):
        login = value.get("login")
        return login if isinstance(login, str) else None
    return None


def _upsert_pull_requests(conn: Any, repo: str, rows: list[dict]) -> int:
    conn.executemany(
        "INSERT INTO dm_pull_requests"
        " (repo, number, title, state, author, created_at, merged_at, closed_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
        " ON CONFLICT(repo, number) DO UPDATE SET"
        "   title=excluded.title, state=excluded.state, author=excluded.author,"
        "   created_at=excluded.created_at, merged_at=excluded.merged_at,"
        "   closed_at=excluded.closed_at",
        [
            (
                repo,
                pr.get("number"),
                pr.get("title"),
                pr.get("state"),
                _login(pr.get("user")),
                pr.get("created_at"),
                pr.get("merged_at"),
                pr.get("closed_at"),
            )
            for pr in rows
            if isinstance(pr.get("number"), int)
        ],
    )
    return len([pr for pr in rows if isinstance(pr.get("number"), int)])


def _upsert_reviews(conn: Any, repo: str, pr_number: int, rows: list[dict]) -> int:
    payload = [
        (
            repo,
            review.get("id"),
            pr_number,
            _login(review.get("user")),
            review.get("state"),
            review.get("submitted_at"),
        )
        for review in rows
        if isinstance(review.get("id"), int)
    ]
    conn.executemany(
        "INSERT INTO dm_reviews"
        " (repo, review_id, pr_number, reviewer, state, submitted_at)"
        " VALUES (?, ?, ?, ?, ?, ?)"
        " ON CONFLICT(repo, review_id) DO UPDATE SET"
        "   pr_number=excluded.pr_number, reviewer=excluded.reviewer,"
        "   state=excluded.state, submitted_at=excluded.submitted_at",
        payload,
    )
    return len(payload)


def _upsert_runs(conn: Any, repo: str, rows: list[dict]) -> int:
    payload = [
        (
            repo,
            run.get("id"),
            run.get("name"),
            run.get("status"),
            run.get("conclusion"),
            run.get("event"),
            run.get("head_branch"),
            run.get("created_at"),
        )
        for run in rows
        if isinstance(run.get("id"), int)
    ]
    conn.executemany(
        "INSERT INTO dm_workflow_runs"
        " (repo, run_id, name, status, conclusion, event, head_branch, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
        " ON CONFLICT(repo, run_id) DO UPDATE SET"
        "   name=excluded.name, status=excluded.status, conclusion=excluded.conclusion,"
        "   event=excluded.event, head_branch=excluded.head_branch,"
        "   created_at=excluded.created_at",
        payload,
    )
    return len(payload)


def _mark_repo(conn: Any, repo: str, complete: bool) -> None:
    now = datetime.now(UTC).isoformat(timespec="seconds")
    conn.execute(
        "INSERT INTO dm_repos (repo, first_seen_at, ingested_at, complete)"
        " VALUES (?, ?, ?, ?)"
        " ON CONFLICT(repo) DO UPDATE SET ingested_at=excluded.ingested_at,"
        "   complete=excluded.complete",
        (repo, now, now, 1 if complete else 0),
    )


def ingest(
    repo: str,
    max_prs: int = DEFAULT_MAX_PRS,
    max_runs: int = DEFAULT_MAX_RUNS,
    db: str | os.PathLike[str] | None = None,
) -> dict:
    """Fetch and store pull requests, reviews and workflow runs for `repo`.

    Returns a summary of what was written. `complete` is False when the run
    stopped early on the rate limit; re-running continues from the API and
    overwrites the same rows, so nothing is duplicated.
    """
    repo = _validate_repo(repo)
    init(db)

    partial_reason = ""
    pulls: list[dict] = []
    runs: list[dict] = []
    reviews_by_pr: dict[int, list[dict]] = {}

    try:
        pulls = _get_pages(
            f"/repos/{repo}/pulls",
            {"state": "all", "sort": "updated", "direction": "desc"},
            max_prs,
        )
        for pull in pulls:
            number = pull.get("number")
            if not isinstance(number, int):
                continue
            payload = _get_one(f"/repos/{repo}/pulls/{number}/reviews", {"per_page": PER_PAGE})
            reviews_by_pr[number] = [r for r in payload if isinstance(r, dict)] if isinstance(
                payload, list
            ) else []

        runs_payload = _get_one(
            f"/repos/{repo}/actions/runs", {"per_page": min(max_runs, PER_PAGE)}
        )
        if isinstance(runs_payload, dict):
            raw = runs_payload.get("workflow_runs")
            runs = [r for r in raw if isinstance(r, dict)][:max_runs] if isinstance(
                raw, list
            ) else []
    except RateLimited as exc:
        partial_reason = str(exc)

    with closing(connect(db)) as conn, conn:
        pr_written = _upsert_pull_requests(conn, repo, pulls)
        review_written = 0
        for number, rows in reviews_by_pr.items():
            review_written += _upsert_reviews(conn, repo, number, rows)
        run_written = _upsert_runs(conn, repo, runs)
        _mark_repo(conn, repo, complete=not partial_reason)

    summary = {
        "repo": repo,
        "pull_requests": pr_written,
        "reviews": review_written,
        "workflow_runs": run_written,
        "complete": not partial_reason,
    }
    if partial_reason:
        summary["stopped_early"] = partial_reason
    return summary


def ingested_repos(db: str | os.PathLike[str] | None = None) -> list[dict]:
    """Repositories that have been ingested, with row counts."""
    init(db)
    with closing(connect(db)) as conn:
        rows = conn.execute(
            "SELECT r.repo, r.ingested_at, r.complete,"
            " (SELECT COUNT(*) FROM dm_pull_requests p WHERE p.repo = r.repo) AS pull_requests,"
            " (SELECT COUNT(*) FROM dm_reviews v WHERE v.repo = r.repo) AS reviews,"
            " (SELECT COUNT(*) FROM dm_workflow_runs w WHERE w.repo = r.repo) AS workflow_runs"
            " FROM dm_repos r ORDER BY r.repo"
        ).fetchall()
    return [{**dict(row), "complete": bool(row["complete"])} for row in rows]


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Ingest delivery data for one repository")
    parser.add_argument("repo", nargs="?", default=os.getenv("DELIVERY_REPO", ""),
                        help="owner/name of a public repository (or set DELIVERY_REPO)")
    parser.add_argument("--max-prs", type=int, default=DEFAULT_MAX_PRS)
    parser.add_argument("--max-runs", type=int, default=DEFAULT_MAX_RUNS)
    parser.add_argument("--db", default=None, help="database path (or GOVERNANCE_DB)")
    parser.add_argument("--list", action="store_true", help="show what has been ingested")
    args = parser.parse_args(argv)

    if args.list:
        for row in ingested_repos(args.db):
            print(
                f"{row['repo']}  prs={row['pull_requests']}  reviews={row['reviews']}"
                f"  runs={row['workflow_runs']}  complete={row['complete']}"
                f"  ingested_at={row['ingested_at']}"
            )
        return 0

    if not args.repo:
        print("no repository given; pass owner/name or set DELIVERY_REPO", file=sys.stderr)
        return 2

    try:
        summary = ingest(args.repo, args.max_prs, args.max_runs, args.db)
    except ConnectorError as exc:
        print(f"ingest failed: {exc}", file=sys.stderr)
        return 1

    print(
        f"{summary['repo']}: {summary['pull_requests']} pull requests, "
        f"{summary['reviews']} reviews, {summary['workflow_runs']} workflow runs"
    )
    if not summary["complete"]:
        print(f"incomplete: {summary['stopped_early']}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
