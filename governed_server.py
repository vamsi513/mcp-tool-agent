"""MCP server that enforces per-user access rules and audits every call.

Exposes the three tools from server.py plus the GitHub connector, each wrapped
in a policy check and an audit record. Two layers do the work:

  * tools/list is filtered — a user is only told about tools their role may
    call, so the model never sees a tool it cannot use.
  * every tools/call is checked again before the function runs, including
    argument rules, so a caller that guesses a name still gets denied.

The identity comes from --user or MCP_AGENT_USER. It is configuration on a
local stdio process, not authentication; see the README.

Run:

    MCP_AGENT_USER=alice python governed_server.py
    python governed_server.py --user alice --policy policy.yaml
"""

from __future__ import annotations

import argparse
import functools
import inspect
import os
import sys
import time
from collections.abc import Callable
from typing import Any

from mcp.server.fastmcp import FastMCP

import audit
import connector_github
import server
from policy import Policy, PolicyError, load_policy

# Every tool this server can offer, by the name it is exposed under.
TOOL_REGISTRY: dict[str, Callable[..., Any]] = {
    "search_github_repos": server.search_github_repos,
    "fetch_url_text": server.fetch_url_text,
    "query_books": server.query_books,
    "list_issues": connector_github.list_issues,
    "get_issue": connector_github.get_issue,
    "prepare_issue_comment": connector_github.prepare_issue_comment,
    "confirm_issue_comment": connector_github.confirm_issue_comment,
}


def effective_arguments(fn: Callable[..., Any], args: tuple, kwargs: dict) -> dict[str, Any]:
    """The arguments a call will really use, with defaults filled in.

    Policy rules such as max_limit have to see the value the tool will act on,
    not just what the caller happened to send.
    """
    try:
        bound = inspect.signature(fn).bind(*args, **kwargs)
    except TypeError:
        # Let the tool itself produce the argument error.
        return dict(kwargs)
    bound.apply_defaults()
    return dict(bound.arguments)


def govern(name: str, fn: Callable[..., Any], user: str, policy: Policy) -> Callable[..., Any]:
    """Wrap `fn` so each call is authorized, timed and audited."""

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        arguments = effective_arguments(fn, args, kwargs)
        decision = policy.check(user, name, arguments)

        if not decision.allowed:
            audit.record(
                user=user,
                role=decision.role,
                tool=name,
                arguments=arguments,
                decision="deny",
                reason=decision.reason,
                outcome="denied",
                duration_ms=0.0,
            )
            # The tool function is never reached.
            return {
                "error": "denied by policy",
                "decision": "deny",
                "reason": decision.reason,
                "tool": name,
                "user": user,
            }

        started = time.perf_counter()
        try:
            result = fn(*args, **kwargs)
        except Exception as exc:
            audit.record(
                user=user,
                role=decision.role,
                tool=name,
                arguments=arguments,
                decision="allow",
                reason="",
                outcome=f"error: {exc.__class__.__name__}",
                duration_ms=(time.perf_counter() - started) * 1000,
            )
            raise
        audit.record(
            user=user,
            role=decision.role,
            tool=name,
            arguments=arguments,
            decision="allow",
            reason="",
            outcome="ok",
            duration_ms=(time.perf_counter() - started) * 1000,
        )
        return result

    return wrapper


class GovernedMCP(FastMCP):
    """FastMCP that advertises only the tools the caller's role allows.

    Every tool is still registered, so a caller that names a tool it was not
    told about reaches the policy wrapper and gets an audited denial rather
    than an unlogged "unknown tool". Filtering happens in the listing only.
    """

    def __init__(self, *args: Any, allowed_tools: frozenset[str], **kwargs: Any) -> None:
        self._allowed_tools = allowed_tools
        super().__init__(*args, **kwargs)

    async def list_tools(self) -> list[Any]:
        tools = await super().list_tools()
        return [tool for tool in tools if tool.name in self._allowed_tools]


def build(user: str, policy: Policy) -> FastMCP:
    """Create a server that advertises only what `user` may call.

    All tools are registered behind the policy wrapper; tools/list is
    filtered, so the model is never shown a tool it cannot use, and a call to
    a hidden tool is still denied and audited.
    """
    mcp = GovernedMCP("tool-agent-governed", allowed_tools=policy.tools_for(user))

    for name, fn in TOOL_REGISTRY.items():
        mcp.add_tool(govern(name, fn, user, policy), name=name)

    # The book resources are reference context, not actions, and are not
    # policy-governed. See the README's limitations.
    mcp.resource("books://schema", mime_type="text/plain")(server.books_schema)
    mcp.resource("books://catalog", mime_type="application/json")(server.books_catalog)

    return mcp


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Policy-enforcing MCP server")
    parser.add_argument(
        "--user",
        default=os.getenv("MCP_AGENT_USER", "").strip(),
        help="caller identity to enforce policy for (or set MCP_AGENT_USER)",
    )
    parser.add_argument(
        "--policy",
        default=None,
        help="path to the policy file (or set POLICY_FILE)",
    )
    args = parser.parse_args(argv)

    if not args.user:
        sys.exit("no identity: pass --user or set MCP_AGENT_USER")

    try:
        policy = load_policy(args.policy)
    except PolicyError as exc:
        sys.exit(f"policy error: {exc}")

    if policy.role_for(args.user) is None:
        # Fail closed and say so, rather than starting with no tools and
        # leaving the caller to guess why.
        sys.exit(f"policy error: unknown user '{args.user}'")

    # Downstream code (pending actions, audit) reads the identity from here.
    os.environ["MCP_AGENT_USER"] = args.user

    audit.init()
    connector_github.init()
    build(args.user, policy).run()


if __name__ == "__main__":
    main()
