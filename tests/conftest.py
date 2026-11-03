import sqlite3

import httpx
import pytest

import connector_github
import delivery_fixtures
import seed_books
import server


@pytest.fixture
def books_db(tmp_path, monkeypatch):
    """Point the server at a fresh throwaway database seeded with the sample rows."""
    db_path = tmp_path / "books.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE books (title TEXT, author TEXT, year INTEGER, genre TEXT, rating REAL)"
    )
    conn.executemany(
        "INSERT INTO books VALUES (?, ?, ?, ?, ?)", seed_books.BOOKS
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(server, "DB_PATH", db_path)
    return db_path


@pytest.fixture
def mock_http(monkeypatch):
    """Replace httpx.Client inside server with one backed by a caller-supplied handler."""

    def install(handler):
        real_client = httpx.Client

        def factory(*args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            return real_client(*args, **kwargs)

        monkeypatch.setattr(server.httpx, "Client", factory)

    return install


@pytest.fixture
def governance_db(tmp_path, monkeypatch):
    """Point the audit log, usage meter and pending actions at a temp database."""
    path = tmp_path / "governance.db"
    monkeypatch.setenv("GOVERNANCE_DB", str(path))
    return path


@pytest.fixture
def mock_github(monkeypatch):
    """Replace httpx.Client inside connector_github and record every request.

    Returns the list of (method, path) pairs the connector issued, so a test
    can assert that no POST went out.
    """
    calls: list[tuple[str, str]] = []

    def install(handler):
        real_client = httpx.Client

        def recording(request: httpx.Request) -> httpx.Response:
            calls.append((request.method, request.url.path))
            return handler(request)

        def factory(*args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(recording)
            return real_client(*args, **kwargs)

        monkeypatch.setattr(connector_github.httpx, "Client", factory)
        return calls

    install.calls = calls  # type: ignore[attr-defined]
    return install


FIXTURE_NAME = "github_delivery"
# One source of truth: the page sizes come from the fixture that was recorded.
_FIXTURE_META = delivery_fixtures.fixture_metadata(FIXTURE_NAME)
FIXTURE_REPO = _FIXTURE_META["repo"]
FIXTURE_MAX_PRS = _FIXTURE_META["max_prs"]
FIXTURE_MAX_RUNS = _FIXTURE_META["max_runs"]


@pytest.fixture
def delivery_api():
    """Point delivery_ingest at the recorded GitHub responses, not the network."""
    with delivery_fixtures.replaying(FIXTURE_NAME) as transport:
        yield transport


@pytest.fixture
def delivery_db(governance_db):
    """A database with the recorded repository already ingested.

    Built through delivery_fixtures.ingested_database, which is the same
    helper evals/run_delivery_eval.py uses, so the runner and the tests
    cannot drift apart.
    """
    with delivery_fixtures.ingested_database(FIXTURE_NAME, db=governance_db) as path:
        yield path
