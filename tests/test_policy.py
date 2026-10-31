import pytest
import yaml

from policy import Policy, PolicyError, load_policy, parse_policy

POLICY = {
    "version": 1,
    "users": {"alice": {"role": "maintainer"}, "bob": {"role": "reader"}},
    "roles": {
        "reader": {
            "tools": ["query_books", "list_issues"],
            "rules": {"list_issues": {"owners": ["octocat"], "max_limit": 5}},
        },
        "maintainer": {
            "tools": ["query_books", "list_issues", "confirm_issue_comment"],
            "rules": {
                "list_issues": {"repos": ["octocat/hello-world"]},
                "prepare_issue_comment": {"max_body_chars": 10},
            },
        },
    },
}


@pytest.fixture
def policy() -> Policy:
    return parse_policy(POLICY)


def test_allowed_call(policy):
    decision = policy.check("bob", "list_issues", {"repo": "octocat/x", "limit": 5})
    assert decision.allowed
    assert decision.status == "allow"
    assert decision.role == "reader"
    assert decision.reason == ""


def test_denied_tool_not_in_role(policy):
    decision = policy.check("bob", "confirm_issue_comment", {})
    assert not decision.allowed
    assert "may not call" in decision.reason


def test_denied_argument_by_owner(policy):
    decision = policy.check("bob", "list_issues", {"repo": "someoneelse/x"})
    assert not decision.allowed
    assert "someoneelse" in decision.reason


def test_denied_argument_by_max_limit(policy):
    decision = policy.check("bob", "list_issues", {"repo": "octocat/x", "limit": 50})
    assert not decision.allowed
    assert "exceeds the maximum" in decision.reason


def test_denied_argument_by_exact_repo_list(policy):
    assert policy.check("alice", "list_issues", {"repo": "octocat/hello-world"}).allowed
    denied = policy.check("alice", "list_issues", {"repo": "octocat/other"})
    assert not denied.allowed
    assert "not in the allowed list" in denied.reason


def test_denied_argument_by_body_length(policy):
    rule_role = parse_policy(
        {
            "users": {"alice": {"role": "maintainer"}},
            "roles": {
                "maintainer": {
                    "tools": ["prepare_issue_comment"],
                    "rules": {"prepare_issue_comment": {"max_body_chars": 10}},
                }
            },
        }
    )
    denied = rule_role.check("alice", "prepare_issue_comment", {"body": "x" * 11})
    assert not denied.allowed
    assert "characters exceeds" in denied.reason
    assert rule_role.check("alice", "prepare_issue_comment", {"body": "short"}).allowed


def test_username_argument_is_checked_against_owners():
    p = parse_policy(
        {
            "users": {"bob": {"role": "reader"}},
            "roles": {
                "reader": {
                    "tools": ["search_github_repos"],
                    "rules": {"search_github_repos": {"owners": ["python"]}},
                }
            },
        }
    )
    assert p.check("bob", "search_github_repos", {"username": "python"}).allowed
    assert not p.check("bob", "search_github_repos", {"username": "someone"}).allowed


def test_unknown_user_is_denied(policy):
    decision = policy.check("mallory", "query_books", {})
    assert not decision.allowed
    assert "unknown user" in decision.reason
    assert policy.tools_for("mallory") == frozenset()


def test_tools_for_filters_by_role(policy):
    assert policy.tools_for("bob") == frozenset({"query_books", "list_issues"})
    assert "confirm_issue_comment" in policy.tools_for("alice")


def test_missing_policy_file_raises():
    with pytest.raises(PolicyError, match="no policy file found"):
        load_policy("/nonexistent/path/to/policy.yaml")


def test_malformed_policy_file_raises(tmp_path):
    bad = tmp_path / "policy.yaml"
    bad.write_text("users: [not, a, mapping]\n")
    with pytest.raises(PolicyError, match="must be a mapping"):
        load_policy(bad)


def test_invalid_yaml_raises(tmp_path):
    bad = tmp_path / "policy.yaml"
    bad.write_text("users: {unclosed\n")
    with pytest.raises(PolicyError, match="not valid YAML"):
        load_policy(bad)


def test_unknown_rule_key_is_rejected():
    with pytest.raises(PolicyError, match="unknown keys"):
        parse_policy(
            {
                "users": {"bob": {"role": "reader"}},
                "roles": {"reader": {"tools": ["x"], "rules": {"x": {"allow_everything": True}}}},
            }
        )


def test_user_referencing_undefined_role_is_rejected():
    with pytest.raises(PolicyError, match="undefined roles"):
        parse_policy({"users": {"bob": {"role": "ghost"}}, "roles": {}})


def test_user_without_role_is_rejected():
    with pytest.raises(PolicyError, match="non-empty 'role'"):
        parse_policy({"users": {"bob": {}}, "roles": {}})


def test_committed_example_policy_is_valid():
    loaded = load_policy("policy.example.yaml")
    assert loaded.role_for("alice") == "maintainer"
    assert "confirm_issue_comment" in loaded.tools_for("alice")
    assert "confirm_issue_comment" not in loaded.tools_for("bob")


def test_example_policy_round_trips_through_yaml(tmp_path):
    path = tmp_path / "policy.yaml"
    path.write_text(yaml.safe_dump(POLICY))
    assert load_policy(path).tools_for("bob") == frozenset({"query_books", "list_issues"})
