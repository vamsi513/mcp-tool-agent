"""Append-only audit log of every tool call, allowed or denied.

One row per attempt. Arguments are redacted before they are stored: secrets
are dropped entirely and free text that could be large or sensitive (an issue
comment body) is reduced to a hash and a length. UPDATE and DELETE are
blocked by triggers, so the table can only grow.
"""

from __future__ import annotations

import hashlib
import json
import os
from contextlib import closing
from datetime import UTC, datetime
from typing import Any

from store import connect

# Argument names whose values are never stored, matched case-insensitively as
# substrings so `github_token` and `Authorization` are both caught.
SECRET_HINTS = ("token", "secret", "password", "authorization", "api_key", "apikey", "credential")

# Argument names stored as a hash plus a length instead of their text.
HASHED_ARGS = ("body",)

# Any other string argument is truncated to this many characters.
MAX_ARG_CHARS = 200

REDACTED = "[redacted]"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT    NOT NULL,
    user        TEXT    NOT NULL,
    role        TEXT    NOT NULL,
    tool        TEXT    NOT NULL,
    arguments   TEXT    NOT NULL,
    decision    TEXT    NOT NULL CHECK (decision IN ('allow', 'deny')),
    reason      TEXT    NOT NULL,
    outcome     TEXT    NOT NULL,
    duration_ms REAL    NOT NULL
);

CREATE INDEX IF NOT EXISTS audit_log_ts_idx   ON audit_log (ts);
CREATE INDEX IF NOT EXISTS audit_log_user_idx ON audit_log (user);
CREATE INDEX IF NOT EXISTS audit_log_tool_idx ON audit_log (tool);

CREATE TRIGGER IF NOT EXISTS audit_log_no_update
BEFORE UPDATE ON audit_log
BEGIN
    SELECT RAISE(ABORT, 'audit_log is append-only');
END;

CREATE TRIGGER IF NOT EXISTS audit_log_no_delete
BEFORE DELETE ON audit_log
BEGIN
    SELECT RAISE(ABORT, 'audit_log is append-only');
END;
"""


def init(db: str | os.PathLike[str] | None = None) -> None:
    """Create the table, indexes and append-only triggers if absent."""
    with closing(connect(db)) as conn, conn:
        conn.executescript(_SCHEMA)


def redact(arguments: dict[str, Any] | None) -> dict[str, Any]:
    """Strip secrets and bulk text out of tool arguments before storage."""
    if not arguments:
        return {}

    clean: dict[str, Any] = {}
    for key, value in arguments.items():
        lowered = str(key).lower()
        if any(hint in lowered for hint in SECRET_HINTS):
            clean[key] = REDACTED
        elif key in HASHED_ARGS and isinstance(value, str):
            digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
            clean[key] = {"sha256": digest, "length": len(value)}
        elif isinstance(value, str) and len(value) > MAX_ARG_CHARS:
            clean[key] = value[:MAX_ARG_CHARS] + f"...[truncated {len(value)} chars]"
        else:
            clean[key] = value
    return clean


def record(
    user: str,
    role: str,
    tool: str,
    arguments: dict[str, Any] | None,
    decision: str,
    reason: str = "",
    outcome: str = "ok",
    duration_ms: float = 0.0,
    db: str | os.PathLike[str] | None = None,
) -> None:
    """Append one row. Arguments are redacted here, so callers may pass raw."""
    init(db)
    row = (
        datetime.now(UTC).isoformat(timespec="milliseconds"),
        user,
        role,
        tool,
        json.dumps(redact(arguments), sort_keys=True),
        decision,
        reason,
        outcome,
        round(float(duration_ms), 3),
    )
    with closing(connect(db)) as conn, conn:
        conn.execute(
            "INSERT INTO audit_log"
            " (ts, user, role, tool, arguments, decision, reason, outcome, duration_ms)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            row,
        )


def query(
    user: str | None = None,
    tool: str | None = None,
    decision: str | None = None,
    since: str | None = None,
    until: str | None = None,
    limit: int = 100,
    db: str | os.PathLike[str] | None = None,
) -> list[dict[str, Any]]:
    """Read rows back, newest first. Time bounds are ISO-8601 strings."""
    init(db)
    clauses: list[str] = []
    args: list[Any] = []
    for column, value in (("user", user), ("tool", tool), ("decision", decision)):
        if value:
            clauses.append(f"{column} = ?")
            args.append(value)
    if since:
        clauses.append("ts >= ?")
        args.append(since)
    if until:
        clauses.append("ts <= ?")
        args.append(until)

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    args.append(max(1, limit))
    sql = f"SELECT * FROM audit_log {where} ORDER BY id DESC LIMIT ?"

    with closing(connect(db)) as conn:
        rows = conn.execute(sql, args).fetchall()

    out = []
    for row in rows:
        item = dict(row)
        item["arguments"] = json.loads(item["arguments"])
        out.append(item)
    return out
