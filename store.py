"""Shared SQLite connection handling for the audit log and usage meter.

Both live in one database file so a single run can be inspected in one place.
The path comes from GOVERNANCE_DB, defaulting to governance.db beside this
module.
"""

from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path

DEFAULT_DB_NAME = "governance.db"

# How long a connection waits for another one to finish switching the
# database into WAL mode before giving up. Only ever spent on a database
# that is not in WAL mode yet, which in practice means the first connections
# to a brand new file.
WAL_SWITCH_TIMEOUT_SECONDS = 5.0


def db_path(explicit: str | os.PathLike[str] | None = None) -> Path:
    if explicit is not None:
        return Path(explicit)
    from_env = os.getenv("GOVERNANCE_DB")
    if from_env:
        return Path(from_env)
    return Path(__file__).parent / DEFAULT_DB_NAME


def _journal_mode(conn: sqlite3.Connection) -> str:
    return str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower()


def _enable_wal(
    conn: sqlite3.Connection, timeout_seconds: float = WAL_SWITCH_TIMEOUT_SECONDS
) -> None:
    """Put the database in WAL mode, waiting out a concurrent attempt.

    Switching the journal mode takes an exclusive lock, and SQLite does not
    run the busy handler for that statement: it gives up at once instead of
    waiting out whoever holds the lock. Several connections opening a
    not-yet-converted database at the same moment therefore raced, and the
    losers raised "database is locked" instead of waiting. Under the gateway
    that was an intermittent 500 whenever parallel requests first recorded
    usage.

    The journal mode is a property of the file rather than of a connection,
    so it only ever has to be set once: checking first means a database
    already in WAL mode costs one lock-free read and no exclusive lock at
    all. Losing the race is then harmless, because the winner leaves the
    database in the mode this wanted, so a loser retries until it observes
    that. Only a database still not in WAL mode when the deadline passes is
    a real failure, and that failure is raised rather than swallowed.
    """
    deadline = time.monotonic() + timeout_seconds
    while True:
        if _journal_mode(conn) == "wal":
            return
        try:
            if str(conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]).lower() == "wal":
                return
        except sqlite3.OperationalError:
            if time.monotonic() >= deadline:
                raise
        if time.monotonic() >= deadline:
            raise sqlite3.OperationalError(
                f"could not switch {conn} to WAL mode within "
                f"{timeout_seconds:g}s; it is in {_journal_mode(conn)!r} mode"
            )
        time.sleep(0.005)


def connect(explicit: str | os.PathLike[str] | None = None) -> sqlite3.Connection:
    """Open the governance database, creating parent directories if needed."""
    path = db_path(explicit)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    _enable_wal(conn)
    conn.execute("PRAGMA foreign_keys=ON")
    return conn
