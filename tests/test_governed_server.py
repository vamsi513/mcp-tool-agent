"""The policy wrapper, and one real stdio session to prove tools/list filters."""

import json
import sys

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import get_default_environment, stdio_client

import audit
import governed_server
from policy import parse_policy

POLICY = {
    "users": {"alice": {"role": "maintainer"}, "bob": {"role": "reader"}},
    "roles": {
        "reader": {
            "tools": ["query_books", "list_issues"],
            "rules": {"list_issues": {"owners": ["octocat"], "max_limit": 5}},
        },
        "maintainer": {"tools": ["query_books", "list_issues", "confirm_issue_comment"]},
    },
}


@pytest.fixture
def policy():
    return parse_policy(POLICY)


def test_allowed_call_reaches_the_tool_and_is_audited(governance_db, policy):
    seen = []

    def tool(repo: str, limit: int = 3) -> dict:
        """A tool."""
        seen.append((repo, limit))
        return {"ok": True}

    wrapped = governed_server.govern("list_issues", tool, "bob", policy)
    assert wrapped(repo="octocat/x") == {"ok": True}
    assert seen == [("octocat/x", 3)]

    rows = audit.query(limit=1)
    assert rows[0]["decision"] == "allow"
    assert rows[0]["outcome"] == "ok"
    assert rows[0]["user"] == "bob"
    assert rows[0]["arguments"] == {"repo": "octocat/x", "limit": 3}


def test_denied_call_never_reaches_the_tool(governance_db, policy):
    reached = []

    def tool(pending_action_id: str) -> dict:
        """A write tool."""
        reached.append(pending_action_id)
        return {"written": True}

    wrapped = governed_server.govern("confirm_issue_comment", tool, "bob", policy)
    result = wrapped(pending_action_id="abc")

    assert reached == []  # the function body never ran
    assert result["decision"] == "deny"
    assert "may not call" in result["reason"]

    rows = audit.query(limit=1)
    assert rows[0]["decision"] == "deny"
    assert rows[0]["outcome"] == "denied"


def test_denied_argument_never_reaches_the_tool(governance_db, policy):
    reached = []

    def tool(repo: str, limit: int = 3) -> dict:
        """A tool."""
        reached.append(repo)
        return {"ok": True}

    wrapped = governed_server.govern("list_issues", tool, "bob", policy)
    result = wrapped(repo="forbidden/x")

    assert reached == []
    assert result["decision"] == "deny"
    assert "forbidden" in result["reason"]


def test_default_arguments_are_policy_checked(governance_db, policy):
    """A limit the caller never sent still has to satisfy max_limit."""
    reached = []

    def tool(repo: str, limit: int = 50) -> dict:
        """A tool whose default exceeds the role's max_limit of 5."""
        reached.append(limit)
        return {"ok": True}

    wrapped = governed_server.govern("list_issues", tool, "bob", policy)
    result = wrapped(repo="octocat/x")

    assert reached == []
    assert "exceeds the maximum" in result["reason"]


def test_unknown_user_is_denied(governance_db, policy):
    def tool() -> dict:
        """A tool."""
        return {"ok": True}

    wrapped = governed_server.govern("query_books", tool, "mallory", policy)
    result = wrapped()
    assert result["decision"] == "deny"
    assert "unknown user" in result["reason"]


def test_audit_record_survives_a_tool_exception(governance_db, policy):
    def tool(repo: str, limit: int = 3) -> dict:
        """A tool that blows up."""
        raise RuntimeError("boom")

    wrapped = governed_server.govern("list_issues", tool, "bob", policy)
    with pytest.raises(RuntimeError, match="boom"):
        wrapped(repo="octocat/x")

    rows = audit.query(limit=1)
    assert rows[0]["decision"] == "allow"
    assert rows[0]["outcome"] == "error: RuntimeError"
    assert rows[0]["duration_ms"] >= 0


def test_build_registers_only_allowed_tools(policy):
    import asyncio

    names = {t.name for t in asyncio.run(governed_server.build("bob", policy).list_tools())}
    assert names == {"query_books", "list_issues"}

    names = {t.name for t in asyncio.run(governed_server.build("alice", policy).list_tools())}
    assert "confirm_issue_comment" in names


def test_wrapper_preserves_the_tool_schema(policy):
    import asyncio

    tools = asyncio.run(governed_server.build("bob", policy).list_tools())
    query_books = next(t for t in tools if t.name == "query_books")
    # Same schema the ungoverned server advertises.
    assert query_books.inputSchema["properties"]["min_rating"]["default"] is None
    assert "genre" in query_books.inputSchema["properties"]


@pytest.mark.asyncio
async def test_stdio_session_filters_tools_and_denies_arguments(tmp_path):
    """Spawn the real governed server and drive it over the protocol."""
    env = {
        **get_default_environment(),
        "MCP_AGENT_USER": "bob",
        "POLICY_FILE": "policy.example.yaml",
        "GOVERNANCE_DB": str(tmp_path / "governance.db"),
    }
    params = StdioServerParameters(
        command=sys.executable, args=["governed_server.py", "--user", "bob"], env=env
    )

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            names = {t.name for t in (await session.list_tools()).tools}
            allowed = await session.call_tool("query_books", {"genre": "fantasy", "limit": 3})
            denied = await session.call_tool("list_issues", {"repo": "forbidden/x"})

    # The reader role in policy.example.yaml has no write tools.
    assert "confirm_issue_comment" not in names
    assert "prepare_issue_comment" not in names
    assert {"query_books", "list_issues", "get_issue"} <= names

    assert json.loads(allowed.content[0].text)["count"] == 3
    assert json.loads(denied.content[0].text)["decision"] == "deny"

    rows = audit.query(db=tmp_path / "governance.db", limit=10)
    decisions = {(r["tool"], r["decision"]) for r in rows}
    assert ("query_books", "allow") in decisions
    assert ("list_issues", "deny") in decisions
