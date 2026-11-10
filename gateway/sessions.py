"""Conversation state for /v1/responses, keyed by response id.

OpenAI's Responses API remembers a conversation server-side, so
`previous_response_id` is enough to continue it. Anthropic has no equivalent,
so the gateway keeps the turns itself. Doing that for both providers means
`previous_response_id` behaves the same either way, and a conversation can be
continued without the caller resending it.

Two backends behind one interface. SQLite is the default and lives in the same
database as the rest of the repository's state. Redis is optional, for running
more than one gateway process.

Expiry is enforced on read as well as by the backend, so an expired id is a
404 rather than a silently empty conversation.
"""

from __future__ import annotations

import json
import os
from contextlib import closing
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from store import connect

_SCHEMA = """
CREATE TABLE IF NOT EXISTS gateway_sessions (
    response_id  TEXT PRIMARY KEY,
    created_ts   TEXT NOT NULL,
    expires_ts   TEXT NOT NULL,
    key_id       TEXT NOT NULL,
    model        TEXT NOT NULL,
    provider     TEXT NOT NULL,
    instructions TEXT NOT NULL DEFAULT '',
    messages     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS gateway_sessions_expiry_idx
    ON gateway_sessions (expires_ts);
"""


@dataclass
class Session:
    """One point in a conversation: everything needed to continue it."""

    response_id: str
    key_id: str
    model: str
    provider: str
    messages: list[dict] = field(default_factory=list)
    instructions: str = ""

    def to_json(self) -> str:
        return json.dumps(
            {
                "response_id": self.response_id,
                "key_id": self.key_id,
                "model": self.model,
                "provider": self.provider,
                "messages": self.messages,
                "instructions": self.instructions,
            }
        )

    @classmethod
    def from_json(cls, raw: str) -> Session:
        data = json.loads(raw)
        return cls(
            response_id=data["response_id"],
            key_id=data.get("key_id", ""),
            model=data.get("model", ""),
            provider=data.get("provider", ""),
            messages=data.get("messages") or [],
            instructions=data.get("instructions", ""),
        )


class SessionStore(Protocol):
    """Where conversations live between requests."""

    def put(self, session: Session, ttl_seconds: int) -> None: ...

    def get(self, response_id: str) -> Session | None:
        """The session, or None when it is unknown or expired."""
        ...

    def delete(self, response_id: str) -> None: ...

    def purge_expired(self) -> int:
        """Drop expired rows. Returns how many went."""
        ...


class SqliteSessionStore:
    """The default. Shares the database the audit log and meter use."""

    backend = "sqlite"

    def __init__(self, db: str | os.PathLike[str] | None = None) -> None:
        self._db = db
        self._ready = False

    def _init(self) -> None:
        if self._ready:
            return
        with closing(connect(self._db)) as conn, conn:
            conn.executescript(_SCHEMA)
        self._ready = True

    def put(self, session: Session, ttl_seconds: int) -> None:
        self._init()
        now = datetime.now(UTC)
        expires = now + timedelta(seconds=ttl_seconds)
        with closing(connect(self._db)) as conn, conn:
            conn.execute(
                "INSERT INTO gateway_sessions"
                " (response_id, created_ts, expires_ts, key_id, model, provider,"
                "  instructions, messages)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(response_id) DO UPDATE SET"
                "   expires_ts=excluded.expires_ts, model=excluded.model,"
                "   provider=excluded.provider, instructions=excluded.instructions,"
                "   messages=excluded.messages",
                (
                    session.response_id,
                    now.isoformat(timespec="milliseconds"),
                    expires.isoformat(timespec="milliseconds"),
                    session.key_id,
                    session.model,
                    session.provider,
                    session.instructions,
                    json.dumps(session.messages),
                ),
            )

    def get(self, response_id: str) -> Session | None:
        self._init()
        with closing(connect(self._db)) as conn:
            row = conn.execute(
                "SELECT * FROM gateway_sessions WHERE response_id = ?", (response_id,)
            ).fetchone()
        if row is None:
            return None
        # Checked here too: a row whose TTL has passed is gone as far as a
        # caller is concerned, whether or not it has been purged yet.
        if row["expires_ts"] <= datetime.now(UTC).isoformat(timespec="milliseconds"):
            return None
        return Session(
            response_id=row["response_id"],
            key_id=row["key_id"],
            model=row["model"],
            provider=row["provider"],
            messages=json.loads(row["messages"]),
            instructions=row["instructions"],
        )

    def delete(self, response_id: str) -> None:
        self._init()
        with closing(connect(self._db)) as conn, conn:
            conn.execute(
                "DELETE FROM gateway_sessions WHERE response_id = ?", (response_id,)
            )

    def purge_expired(self) -> int:
        self._init()
        now = datetime.now(UTC).isoformat(timespec="milliseconds")
        with closing(connect(self._db)) as conn, conn:
            cursor = conn.execute(
                "DELETE FROM gateway_sessions WHERE expires_ts <= ?", (now,)
            )
            return int(cursor.rowcount or 0)


class RedisSessionStore:
    """Optional backend, for running more than one gateway process.

    The TTL is Redis's own, so expiry needs no sweeping. `purge_expired`
    therefore has nothing to do and reports zero.
    """

    backend = "redis"
    key_prefix = "gateway:session:"

    def __init__(self, url: str, client: Any | None = None) -> None:
        if client is not None:
            self._client = client
        else:
            try:
                import redis
            except ImportError as exc:  # pragma: no cover - import guard
                raise RuntimeError(
                    "the redis backend needs the 'redis' package; install it or "
                    "set sessions.backend to sqlite"
                ) from exc
            self._client = redis.Redis.from_url(url, decode_responses=True)

    def _key(self, response_id: str) -> str:
        return f"{self.key_prefix}{response_id}"

    def put(self, session: Session, ttl_seconds: int) -> None:
        self._client.set(self._key(session.response_id), session.to_json(), ex=ttl_seconds)

    def get(self, response_id: str) -> Session | None:
        raw = self._client.get(self._key(response_id))
        if raw is None:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode()
        return Session.from_json(raw)

    def delete(self, response_id: str) -> None:
        self._client.delete(self._key(response_id))

    def purge_expired(self) -> int:
        return 0


def build_store(backend: str, redis_url: str = "", db: Any = None) -> SessionStore:
    """The store named by config. Raises ValueError on an unknown backend."""
    if backend == "sqlite":
        return SqliteSessionStore(db)
    if backend == "redis":
        return RedisSessionStore(redis_url)
    raise ValueError(f"unknown session backend {backend!r}")
