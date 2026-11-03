"""Record and replay GitHub REST responses for the delivery ingest.

Recording hits the real API once and writes every response to a JSON file.
Tests replay that file through an httpx MockTransport, so the suite needs no
network. Both sides share `fixture_key`, so a recording and a replay cannot
drift apart.

Record with:

    python delivery_fixtures.py encode/httpx --max-prs 12 --max-runs 50
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx

FIXTURE_DIR = Path(__file__).parent / "tests" / "fixtures"


def fixture_key(request: httpx.Request) -> str:
    """Stable identity for a request: method, path and sorted query."""
    params = sorted(request.url.params.multi_items())
    query = "&".join(f"{k}={v}" for k, v in params)
    return f"{request.method} {request.url.path}" + (f"?{query}" if query else "")


def fixture_path(name: str) -> Path:
    return FIXTURE_DIR / f"{name}.json"


def load_fixtures(name: str) -> dict[str, Any]:
    path = fixture_path(name)
    if not path.is_file():
        raise FileNotFoundError(
            f"fixture {path} is missing; record it with "
            f"`python delivery_fixtures.py <owner/name>`"
        )
    data = json.loads(path.read_text())
    responses = data.get("responses")
    if not isinstance(responses, dict):
        raise ValueError(f"{path} has no responses map")
    return responses


def mock_transport(name: str) -> httpx.MockTransport:
    """A transport that replays a recorded file and fails loudly on a miss."""
    responses = load_fixtures(name)

    def handler(request: httpx.Request) -> httpx.Response:
        key = fixture_key(request)
        recorded = responses.get(key)
        if recorded is None:
            raise AssertionError(
                f"no recorded response for {key}; re-record the fixture or "
                f"narrow the test. Known keys: {sorted(responses)[:5]}"
            )
        return httpx.Response(
            recorded.get("status", 200),
            json=recorded.get("json"),
            headers=recorded.get("headers") or {},
        )

    return httpx.MockTransport(handler)


# Only the fields the ingest reads are kept, so a recording stays small enough
# to live in the repository. Everything kept is verbatim from the API.
PULL_FIELDS = ("number", "title", "state", "created_at", "merged_at", "closed_at")
REVIEW_FIELDS = ("id", "state", "submitted_at")
RUN_FIELDS = ("id", "name", "status", "conclusion", "event", "head_branch", "created_at")


def _keep(row: Any, fields: tuple[str, ...]) -> dict[str, Any]:
    kept: dict[str, Any] = {f: row.get(f) for f in fields}
    user = row.get("user")
    if isinstance(user, dict):
        kept["user"] = {"login": user.get("login")}
    return kept


def prune(key: str, body: Any) -> Any:
    """Drop fields the ingest never looks at."""
    if body is None:
        return None
    if "/pulls/" in key and key.endswith("/reviews") or "/reviews?" in key:
        return [_keep(r, REVIEW_FIELDS) for r in body if isinstance(r, dict)]
    if "/actions/runs" in key and isinstance(body, dict):
        runs = body.get("workflow_runs") or []
        return {
            "total_count": body.get("total_count"),
            "workflow_runs": [_keep(r, RUN_FIELDS) for r in runs if isinstance(r, dict)],
        }
    if "/pulls" in key and isinstance(body, list):
        return [_keep(r, PULL_FIELDS) for r in body if isinstance(r, dict)]
    return body



def fixture_metadata(name: str) -> dict[str, Any]:
    """The repository and page sizes a fixture was recorded with."""
    path = fixture_path(name)
    if not path.is_file():
        raise FileNotFoundError(
            f"fixture {path} is missing; record it with "
            f"`python delivery_fixtures.py <owner/name>`"
        )
    document = json.loads(path.read_text())
    parameters = document.get("request_parameters") or {}
    return {
        "repo": document["repo"],
        "max_prs": int(parameters.get("max_prs", 12)),
        "max_runs": int(parameters.get("max_runs", 50)),
    }


@contextmanager
def replaying(name: str) -> Iterator[httpx.MockTransport]:
    """Point delivery_ingest at a recorded file for the duration of the block."""
    import delivery_ingest

    transport = mock_transport(name)
    real_client = httpx.Client

    def factory(*args: Any, **kwargs: Any) -> httpx.Client:
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    delivery_ingest.httpx.Client = factory  # type: ignore[misc,assignment]
    try:
        yield transport
    finally:
        delivery_ingest.httpx.Client = real_client  # type: ignore[misc]


@contextmanager
def ingested_database(
    name: str = "github_delivery", db: str | os.PathLike[str] | None = None
) -> Iterator[Path]:
    """Yield a database holding the recorded repository, already ingested.

    With no `db`, a database is created in a temporary directory and removed
    on exit, so nothing outside that directory is touched. This is the single
    path used by both the test fixtures and the evaluation runner, so the two
    cannot drift apart.
    """
    import delivery_ingest

    metadata = fixture_metadata(name)
    holder: tempfile.TemporaryDirectory[str] | None = None
    if db is None:
        holder = tempfile.TemporaryDirectory(prefix="delivery-eval-")
        target = Path(holder.name) / "delivery.db"
    else:
        target = Path(db)

    try:
        with replaying(name):
            delivery_ingest.ingest(
                metadata["repo"],
                max_prs=metadata["max_prs"],
                max_runs=metadata["max_runs"],
                db=target,
            )
        yield target
    finally:
        if holder is not None:
            holder.cleanup()


class _RecordingTransport(httpx.HTTPTransport):
    """Passes requests through to GitHub and keeps a copy of each response."""

    def __init__(self, sink: dict[str, Any], prune_bodies: bool = True) -> None:
        super().__init__()
        self._sink = sink
        self._prune = prune_bodies

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        response = super().handle_request(request)
        response.read()
        try:
            body = response.json()
        except ValueError:
            body = None
        # Keep only the headers the ingest actually reads. Nothing here can
        # carry the token: it only ever appears on the request side.
        kept = {
            header: response.headers[header]
            for header in ("x-ratelimit-remaining", "x-ratelimit-reset", "link")
            if header in response.headers
        }
        key = fixture_key(request)
        self._sink[key] = {
            "status": response.status_code,
            "json": prune(key, body) if self._prune else body,
            "headers": kept,
        }
        return response


def record(
    repo: str, max_prs: int, max_runs: int, name: str, prune_bodies: bool = True
) -> dict[str, Any]:
    """Run one real ingest against GitHub and save every response."""
    import delivery_ingest

    sink: dict[str, Any] = {}
    real_client = httpx.Client

    def factory(*args: Any, **kwargs: Any) -> httpx.Client:
        kwargs["transport"] = _RecordingTransport(sink, prune_bodies)
        return real_client(*args, **kwargs)

    delivery_ingest.httpx.Client = factory  # type: ignore[misc,assignment]
    try:
        scratch = FIXTURE_DIR / "_record_scratch.db"
        scratch.unlink(missing_ok=True)
        summary = delivery_ingest.ingest(repo, max_prs=max_prs, max_runs=max_runs, db=scratch)
    finally:
        delivery_ingest.httpx.Client = real_client  # type: ignore[misc]
        (FIXTURE_DIR / "_record_scratch.db").unlink(missing_ok=True)
        for suffix in ("-wal", "-shm"):
            (FIXTURE_DIR / f"_record_scratch.db{suffix}").unlink(missing_ok=True)

    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    document = {
        "repo": repo,
        "recorded_from": "https://api.github.com",
        "pruned_to_fields_read_by_ingest": prune_bodies,
        # Replaying has to request the same page sizes, or the request keys
        # will not match what was recorded.
        "request_parameters": {"max_prs": max_prs, "max_runs": max_runs},
        "request_count": len(sink),
        "summary": summary,
        "responses": sink,
    }
    fixture_path(name).write_text(json.dumps(document, indent=1, sort_keys=True) + "\n")
    return document


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("repo", help="owner/name of a public repository")
    parser.add_argument("--max-prs", type=int, default=12)
    parser.add_argument("--max-runs", type=int, default=50)
    parser.add_argument("--name", default="github_delivery",
                        help="fixture file name without the .json suffix")
    parser.add_argument("--no-prune", action="store_true",
                        help="keep every field instead of only those the ingest reads")
    args = parser.parse_args(argv)

    if os.getenv("GITHUB_TOKEN"):
        print("note: GITHUB_TOKEN is set; it is used but never recorded", file=sys.stderr)

    document = record(
        args.repo, args.max_prs, args.max_runs, args.name, not args.no_prune
    )
    print(
        f"recorded {document['request_count']} responses for {args.repo} "
        f"to {fixture_path(args.name)}"
    )
    print(f"summary: {json.dumps(document['summary'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
