import json
import sqlite3

import pytest

import audit


def test_allow_and_deny_are_both_recorded(governance_db):
    audit.record("bob", "reader", "query_books", {"genre": "fantasy"}, "allow")
    audit.record("bob", "reader", "confirm_issue_comment", {}, "deny", reason="not permitted",
                 outcome="denied")

    rows = audit.query(limit=10)
    assert [r["decision"] for r in rows] == ["deny", "allow"]  # newest first
    assert rows[0]["reason"] == "not permitted"
    assert rows[1]["reason"] == ""


def test_secrets_are_never_stored(governance_db):
    audit.record(
        "alice",
        "maintainer",
        "list_issues",
        {
            "repo": "octocat/x",
            "github_token": "ghp_realsecret",
            "GITHUB_TOKEN": "ghp_realsecret",
            "Authorization": "Bearer ghp_realsecret",
            "api_key": "sk-realsecret",
            "password": "hunter2",
            "nested_secret": "shh",
        },
        "allow",
    )

    stored = audit.query(limit=1)[0]["arguments"]
    blob = json.dumps(stored)
    assert "ghp_realsecret" not in blob
    assert "sk-realsecret" not in blob
    assert "hunter2" not in blob
    assert "shh" not in blob
    assert stored["repo"] == "octocat/x"
    assert stored["github_token"] == audit.REDACTED

    # And not anywhere in the raw database file either.
    assert b"ghp_realsecret" not in governance_db.read_bytes()


def test_comment_body_is_hashed_not_stored(governance_db):
    body = "Please take a look at this, it is quite long and possibly sensitive."
    audit.record("alice", "maintainer", "prepare_issue_comment",
                 {"repo": "octocat/x", "number": 1, "body": body}, "allow")

    stored = audit.query(limit=1)[0]["arguments"]
    assert stored["body"]["length"] == len(body)
    assert len(stored["body"]["sha256"]) == 64
    assert body not in json.dumps(stored)
    assert body.encode() not in governance_db.read_bytes()


def test_long_strings_are_truncated(governance_db):
    audit.record("bob", "reader", "fetch_url_text", {"url": "u" * 500}, "allow")
    stored = audit.query(limit=1)[0]["arguments"]
    assert "truncated 500 chars" in stored["url"]
    assert len(stored["url"]) < 500


def test_query_filters(governance_db):
    audit.record("alice", "maintainer", "list_issues", {}, "allow")
    audit.record("bob", "reader", "list_issues", {}, "deny", reason="no", outcome="denied")
    audit.record("bob", "reader", "query_books", {}, "allow")

    assert len(audit.query(user="bob")) == 2
    assert len(audit.query(tool="list_issues")) == 2
    assert len(audit.query(decision="deny")) == 1
    assert len(audit.query(user="bob", tool="query_books")) == 1
    assert audit.query(limit=1)[0]["tool"] == "query_books"


def test_time_range_filter(governance_db):
    audit.record("bob", "reader", "query_books", {}, "allow")
    ts = audit.query(limit=1)[0]["ts"]

    assert len(audit.query(since=ts)) == 1
    assert len(audit.query(until=ts)) == 1
    assert len(audit.query(since="2999-01-01T00:00:00+00:00")) == 0


def test_log_is_append_only(governance_db):
    audit.record("bob", "reader", "query_books", {}, "allow")

    conn = sqlite3.connect(governance_db)
    try:
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            conn.execute("DELETE FROM audit_log")
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            conn.execute("UPDATE audit_log SET user = 'someone-else'")
    finally:
        conn.close()

    assert audit.query(limit=10)[0]["user"] == "bob"


def test_decision_column_rejects_junk(governance_db):
    audit.init()
    conn = sqlite3.connect(governance_db)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO audit_log"
                " (ts, user, role, tool, arguments, decision, reason, outcome, duration_ms)"
                " VALUES ('t', 'u', 'r', 'x', '{}', 'maybe', '', 'ok', 0)"
            )
    finally:
        conn.close()


def test_redact_handles_empty_and_none():
    assert audit.redact(None) == {}
    assert audit.redact({}) == {}
