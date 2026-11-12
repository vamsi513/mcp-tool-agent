"""Opening the governance database from several threads at once.

`connect` used to run `PRAGMA journal_mode=WAL` on every connection. SQLite
takes an exclusive lock to change the journal mode and does not run the busy
handler for it, so connections opening a not-yet-converted database at the
same moment raced and the losers raised "database is locked" rather than
waiting. Under the gateway that was an intermittent 500 whenever parallel
requests recorded usage against a fresh database.

The trigger is a database that is not in WAL mode yet, so each test here
starts from a path that does not exist.
"""

from __future__ import annotations

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

import store
from store import connect

THREADS = 24
TRIALS = 6


def race_once(db) -> list[str]:
    """Open, write and close from THREADS threads released together."""
    barrier = threading.Barrier(THREADS)
    failures: list[str] = []

    def worker(index: int) -> None:
        barrier.wait(timeout=30)
        try:
            conn = connect(db)
            with conn:
                conn.execute("CREATE TABLE IF NOT EXISTS t (x INTEGER)")
                conn.execute("INSERT INTO t (x) VALUES (?)", (index,))
            conn.close()
        except BaseException as exc:  # noqa: BLE001 - collected, then asserted on
            failures.append(f"{type(exc).__name__}: {exc}")

    with ThreadPoolExecutor(max_workers=THREADS) as pool:
        list(pool.map(worker, range(THREADS)))
    return failures


class Cursor:
    """Just enough of a sqlite3 cursor for the pragma reads below."""

    def __init__(self, value: str) -> None:
        self._value = value

    def fetchone(self) -> tuple[str]:
        return (self._value,)


class RacingConnection:
    """A connection that loses the race to switch the journal mode.

    The real race is rare enough that driving it through threads catches the
    old behaviour in roughly one run in eight, which is too thin to rely on.
    This stands in for the losing connection directly: the mode change raises
    exactly as SQLite's does when another connection holds the exclusive
    lock, and `winner_converts` says whether that other connection succeeded.
    """

    def __init__(self, failures: int, winner_converts: bool) -> None:
        self.mode = "delete"
        self.failures = failures
        self.winner_converts = winner_converts
        self.attempts = 0

    def execute(self, sql: str):
        if sql == "PRAGMA journal_mode":
            return Cursor(self.mode)
        if sql == "PRAGMA journal_mode=WAL":
            self.attempts += 1
            if self.failures > 0:
                self.failures -= 1
                if self.winner_converts:
                    # Whoever held the lock finished the conversion.
                    self.mode = "wal"
                raise sqlite3.OperationalError("database is locked")
            self.mode = "wal"
            return Cursor(self.mode)
        raise AssertionError(f"unexpected statement: {sql}")


def test_losing_the_race_to_a_connection_that_converts_is_not_an_error():
    """The case that used to raise: another connection got there first."""
    conn = RacingConnection(failures=1, winner_converts=True)
    store._enable_wal(conn, timeout_seconds=1.0)
    assert conn.mode == "wal"
    # One attempt, then the retry sees the mode is already right.
    assert conn.attempts == 1


def test_losing_the_race_to_a_connection_that_does_not_convert_retries():
    conn = RacingConnection(failures=2, winner_converts=False)
    store._enable_wal(conn, timeout_seconds=1.0)
    assert conn.mode == "wal"
    assert conn.attempts == 3


def test_a_mode_change_that_never_succeeds_raises():
    conn = RacingConnection(failures=10_000, winner_converts=False)
    with pytest.raises(sqlite3.OperationalError):
        store._enable_wal(conn, timeout_seconds=0.05)


def test_simultaneous_first_connections_to_a_new_database_all_succeed(tmp_path):
    """The whole path under threads.

    This is a smoke test rather than the regression guard: the underlying
    race is timing-dependent and the pre-fix code survived it in most runs.
    The deterministic guards are the three tests above.
    """
    for trial in range(TRIALS):
        db = tmp_path / f"fresh{trial}.db"
        failures = race_once(db)
        assert not failures, f"trial {trial} failed: {failures[:3]}"

        check = connect(db)
        assert check.execute("SELECT COUNT(*) FROM t").fetchone()[0] == THREADS
        check.close()


def test_the_database_is_still_put_into_wal_mode(tmp_path):
    conn = connect(tmp_path / "mode.db")
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    finally:
        conn.close()


def test_a_database_already_in_wal_mode_is_left_alone(tmp_path):
    """The common path must not take an exclusive lock at all.

    A second connection opening while an exclusive write transaction is in
    flight has to succeed, which it only can if it skips the mode change.
    """
    db = tmp_path / "already.db"
    first = connect(db)
    first.execute("CREATE TABLE t (x INTEGER)")
    first.commit()

    holder = connect(db)
    holder.execute("BEGIN EXCLUSIVE")
    holder.execute("INSERT INTO t (x) VALUES (1)")
    try:
        second = connect(db)
        assert second.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 0
        second.close()
    finally:
        holder.rollback()
        holder.close()
        first.close()


def test_a_database_that_cannot_be_converted_still_raises(tmp_path):
    """The tolerance is narrow: an unconvertible database is still an error.

    `_enable_wal` is called directly with a short deadline, because going
    through `connect` would spend the full production timeout waiting for a
    lock this test never releases.
    """
    db = tmp_path / "stuck.db"
    first = sqlite3.connect(db)
    first.execute("PRAGMA journal_mode=DELETE")
    first.execute("CREATE TABLE t (x INTEGER)")
    first.commit()

    holder = sqlite3.connect(db)
    holder.execute("BEGIN EXCLUSIVE")
    holder.execute("INSERT INTO t (x) VALUES (1)")
    try:
        blocked = sqlite3.connect(db)
        with pytest.raises(sqlite3.OperationalError):
            store._enable_wal(blocked, timeout_seconds=0.05)
        blocked.close()
    finally:
        holder.rollback()
        holder.close()
        first.close()
