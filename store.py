"""Shared SQLite connection handling for the audit log and usage meter.

Both live in one database file so a single run can be inspected in one place.
The path comes from GOVERNANCE_DB, defaulting to governance.db beside this
module.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

DEFAULT_DB_NAME = "governance.db"


def db_path(explicit: str | os.PathLike[str] | None = None) -> Path:
    if explicit is not None:
        return Path(explicit)
    from_env = os.getenv("GOVERNANCE_DB")
    if from_env:
        return Path(from_env)
    return Path(__file__).parent / DEFAULT_DB_NAME


def connect(explicit: str | os.PathLike[str] | None = None) -> sqlite3.Connection:
    """Open the governance database, creating parent directories if needed."""
    path = db_path(explicit)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn
