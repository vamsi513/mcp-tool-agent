import sqlite3

import httpx
import pytest

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
