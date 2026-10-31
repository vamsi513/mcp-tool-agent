"""Per-user access rules for MCP tools.

A policy file maps users to roles and roles to the tools they may call, with
optional per-tool argument rules. Everything not explicitly allowed is denied.

The caller's identity arrives as configuration (an environment variable or a
CLI flag passed when the server is spawned), not as an authenticated
credential. See the governance section of the README for what that does and
does not buy you.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

DEFAULT_POLICY_PATH = Path(__file__).parent / "policy.yaml"
EXAMPLE_POLICY_PATH = Path(__file__).parent / "policy.example.yaml"

# Argument rule keys understood by `_check_arguments`. Anything else in a
# rules block is a policy authoring mistake and is reported as such.
KNOWN_RULE_KEYS = frozenset({"repos", "owners", "max_limit", "max_body_chars"})


class PolicyError(Exception):
    """Raised when the policy file is missing, unreadable or malformed."""


@dataclass(frozen=True)
class Decision:
    """The outcome of one authorization check."""

    allowed: bool
    user: str
    role: str
    tool: str
    reason: str = ""

    @property
    def status(self) -> str:
        return "allow" if self.allowed else "deny"


@dataclass(frozen=True)
class Policy:
    users: dict[str, str]
    roles: dict[str, frozenset[str]]
    rules: dict[str, dict[str, dict[str, Any]]]

    def role_for(self, user: str) -> str | None:
        return self.users.get(user)

    def tools_for(self, user: str) -> frozenset[str]:
        """Tool names this user may call at all, ignoring argument rules."""
        role = self.role_for(user)
        if role is None:
            return frozenset()
        return self.roles.get(role, frozenset())

    def check(self, user: str, tool: str, arguments: dict[str, Any] | None = None) -> Decision:
        """Decide whether `user` may call `tool` with `arguments`."""
        role = self.role_for(user)
        if role is None:
            return Decision(False, user, "", tool, f"unknown user '{user}'")
        if role not in self.roles:
            return Decision(False, user, role, tool, f"role '{role}' is not defined")
        if tool not in self.roles[role]:
            return Decision(False, user, role, tool, f"role '{role}' may not call '{tool}'")

        rule = self.rules.get(role, {}).get(tool, {})
        if rule:
            problem = _check_arguments(rule, arguments or {})
            if problem:
                return Decision(False, user, role, tool, problem)
        return Decision(True, user, role, tool)


def _check_arguments(rule: dict[str, Any], arguments: dict[str, Any]) -> str | None:
    """Return a denial reason if `arguments` break `rule`, else None."""
    repo = arguments.get("repo")
    owners = rule.get("owners")
    repos = rule.get("repos")

    if repos is not None and repo is not None and repo not in repos:
        return f"repo '{repo}' is not in the allowed list for this role"

    if owners is not None:
        # A repo argument is checked on its owner half; search_github_repos
        # takes a bare owner in `username`.
        candidate = repo.split("/")[0] if isinstance(repo, str) and "/" in repo else None
        if candidate is None:
            username = arguments.get("username")
            candidate = username if isinstance(username, str) else None
        if candidate is not None and candidate not in owners:
            return f"owner '{candidate}' is not in the allowed list for this role"

    max_limit = rule.get("max_limit")
    limit = arguments.get("limit")
    if max_limit is not None and isinstance(limit, int) and limit > max_limit:
        return f"limit {limit} exceeds the maximum of {max_limit} for this role"

    max_body = rule.get("max_body_chars")
    body = arguments.get("body")
    if max_body is not None and isinstance(body, str) and len(body) > max_body:
        return f"body of {len(body)} characters exceeds the maximum of {max_body} for this role"

    return None


def _require_mapping(value: Any, label: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise PolicyError(f"{label} must be a mapping, got {type(value).__name__}")
    return value


def parse_policy(raw: Any) -> Policy:
    """Build a Policy from already-loaded YAML data."""
    document = _require_mapping(raw, "policy document")

    users: dict[str, str] = {}
    for name, entry in _require_mapping(document.get("users"), "users").items():
        entry = _require_mapping(entry, f"users.{name}")
        role = entry.get("role")
        if not isinstance(role, str) or not role:
            raise PolicyError(f"users.{name} needs a non-empty 'role'")
        users[str(name)] = role

    roles: dict[str, frozenset[str]] = {}
    rules: dict[str, dict[str, dict[str, Any]]] = {}
    for name, entry in _require_mapping(document.get("roles"), "roles").items():
        entry = _require_mapping(entry, f"roles.{name}")
        tools = entry.get("tools") or []
        if not isinstance(tools, list) or not all(isinstance(t, str) for t in tools):
            raise PolicyError(f"roles.{name}.tools must be a list of tool names")
        roles[str(name)] = frozenset(tools)

        role_rules: dict[str, dict[str, Any]] = {}
        for tool, rule in _require_mapping(entry.get("rules"), f"roles.{name}.rules").items():
            rule = _require_mapping(rule, f"roles.{name}.rules.{tool}")
            unknown = set(rule) - KNOWN_RULE_KEYS
            if unknown:
                raise PolicyError(
                    f"roles.{name}.rules.{tool} has unknown keys: {sorted(unknown)}"
                )
            role_rules[str(tool)] = dict(rule)
        rules[str(name)] = role_rules

    missing = {role for role in users.values() if role not in roles}
    if missing:
        raise PolicyError(f"users reference undefined roles: {sorted(missing)}")

    return Policy(users=users, roles=roles, rules=rules)


def load_policy(path: str | os.PathLike[str] | None = None) -> Policy:
    """Read and parse the policy file.

    Resolution order: the explicit `path`, then POLICY_FILE from the
    environment, then policy.yaml beside this module, then the committed
    policy.example.yaml.
    """
    candidates: list[Path] = []
    if path is not None:
        candidates.append(Path(path))
    else:
        from_env = os.getenv("POLICY_FILE")
        if from_env:
            candidates.append(Path(from_env))
        candidates.append(DEFAULT_POLICY_PATH)
        candidates.append(EXAMPLE_POLICY_PATH)

    for candidate in candidates:
        if candidate.is_file():
            try:
                raw = yaml.safe_load(candidate.read_text())
            except yaml.YAMLError as exc:
                raise PolicyError(f"{candidate} is not valid YAML: {exc}") from exc
            return parse_policy(raw)

    tried = ", ".join(str(c) for c in candidates)
    raise PolicyError(f"no policy file found (tried: {tried})")


def current_user() -> str:
    """Identity of the caller, from configuration. Empty string if unset."""
    return os.getenv("MCP_AGENT_USER", "").strip()
