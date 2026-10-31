import json

import audit
import govctl
import metering


def test_render_table_handles_empty_and_none():
    assert govctl.render_table([], ["a"]) == "(no rows)"
    out = govctl.render_table([{"a": None, "b": 1.5}], ["a", "b"])
    assert "-" in out
    assert "1.500000" in out


def test_audit_command_table(governance_db, capsys):
    audit.record("bob", "reader", "list_issues", {"repo": "octocat/x"}, "deny",
                 reason="nope", outcome="denied")
    assert govctl.main(["audit"]) == 0

    out = capsys.readouterr().out
    assert "bob" in out
    assert "list_issues" in out
    assert "deny" in out
    assert "1 row(s)" in out


def test_audit_command_json_and_filters(governance_db, capsys):
    audit.record("bob", "reader", "query_books", {}, "allow")
    audit.record("alice", "maintainer", "list_issues", {}, "deny", reason="x", outcome="denied")

    assert govctl.main(["audit", "--user", "alice", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert len(rows) == 1
    assert rows[0]["user"] == "alice"
    assert rows[0]["decision"] == "deny"


def test_usage_command_table(governance_db, capsys):
    metering.record_llm_turn("bob", "gpt-4o-mini", 1000, 500)
    assert govctl.main(["usage", "--group", "user", "--prices", "prices.example.yaml"]) == 0

    out = capsys.readouterr().out
    assert "bob" in out
    assert "1000" in out
    assert "cost basis" in out
    assert "estimate" in out


def test_usage_command_json(governance_db, capsys):
    metering.record_llm_turn("bob", "gpt-4o-mini", 1000, 500)
    metering.record_tool_call("bob", "query_books")

    assert govctl.main(
        ["usage", "--group", "tool", "--json", "--prices", "prices.example.yaml"]
    ) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["rows"] == [{"user": "bob", "tool": "query_books", "calls": 1}]
    assert payload["totals"]["calls"] == 1


def test_usage_command_reports_bad_price_file(governance_db, capsys):
    assert govctl.main(["usage", "--prices", "/nonexistent/prices.yaml"]) == 2
    assert "price table error" in capsys.readouterr().err
