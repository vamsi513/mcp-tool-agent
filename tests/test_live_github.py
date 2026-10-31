"""One optional test against the real GitHub API.

Skipped unless GITHUB_TOKEN is set. It only reads from a public repository.
Nothing here writes: the write path needs a pending id from
prepare_issue_comment, and that is never called.
"""

import os

import pytest

import connector_github as gh

pytestmark = pytest.mark.skipif(
    not os.getenv("GITHUB_TOKEN"),
    reason="set GITHUB_TOKEN to run the live GitHub read test",
)

PUBLIC_REPO = "octocat/Hello-World"


@pytest.fixture(autouse=True)
def real_api(monkeypatch):
    """Make sure a stub base URL left over from the evals is not in play."""
    monkeypatch.delenv("GITHUB_API_BASE", raising=False)


def test_list_issues_against_public_repo():
    result = gh.list_issues(PUBLIC_REPO, state="all", limit=3)

    assert result["repo"] == PUBLIC_REPO
    assert result["count"] <= 3
    for issue in result["issues"]:
        assert isinstance(issue["number"], int)
        assert issue["state"] in ("open", "closed")


def test_get_issue_against_public_repo():
    listing = gh.list_issues(PUBLIC_REPO, state="all", limit=1)
    if not listing["issues"]:
        pytest.skip(f"{PUBLIC_REPO} has no issues to read")

    number = listing["issues"][0]["number"]
    issue = gh.get_issue(PUBLIC_REPO, number)

    assert issue["number"] == number
    assert issue["repo"] == PUBLIC_REPO
    assert "untrusted_content" in issue
    assert len(issue["comments"]) <= gh.MAX_COMMENTS
