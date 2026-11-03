"""The GraphQL endpoint is read-only and reports the same numbers as the SQL."""

import json
import threading
import urllib.request

import pytest

import delivery_graphql as dg
from tests import independent_metrics as im
from tests.conftest import FIXTURE_REPO


def test_repos_query(delivery_db):
    result = dg.execute("{ repos { name pullRequests reviews workflowRuns complete } }")
    assert "errors" not in result
    repo = result["data"]["repos"][0]
    assert repo["name"] == FIXTURE_REPO
    assert repo["pullRequests"] == 12
    assert repo["complete"] is True


def test_metric_query_matches_independent_value(delivery_db):
    result = dg.execute(
        f'{{ metric(repo: "{FIXTURE_REPO}", metric: PR_CYCLE_TIME)'
        " { value unit sampleSize statistic sourcePullRequests } }"
    )
    metric = result["data"]["metric"]
    expected = im.cycle_times()

    assert metric["value"] == im.median(expected.values())
    assert metric["sampleSize"] == len(expected)
    assert metric["sourcePullRequests"] == sorted(expected)
    assert metric["unit"] == "hours"


def test_large_run_ids_survive_as_ids(delivery_db):
    _, failed = im.workflow_outcomes()
    result = dg.execute(
        f'{{ metric(repo: "{FIXTURE_REPO}", metric: WORKFLOW_FAILURE_RATE)'
        " { value failures completedRuns failedWorkflowRuns } }"
    )
    metric = result["data"]["metric"]
    assert "errors" not in result
    assert metric["failedWorkflowRuns"] == [str(i) for i in sorted(failed)]
    assert max(failed) > 2**31  # would overflow a GraphQL Int


def test_merge_frequency_weeks(delivery_db):
    result = dg.execute(
        f'{{ metric(repo: "{FIXTURE_REPO}", metric: MERGE_FREQUENCY)'
        " { totalMerges weeks { weekStarting merges pullRequests } } }"
    )
    weeks = {
        w["weekStarting"]: sorted(w["pullRequests"])
        for w in result["data"]["metric"]["weeks"]
    }
    assert weeks == im.merges_by_week()


def test_unknown_repo_is_null_not_a_guess(delivery_db):
    result = dg.execute('{ metric(repo: "nobody/nothing", metric: PR_CYCLE_TIME) { value } }')
    assert result["data"]["metric"] is None


@pytest.mark.parametrize(
    "query",
    [
        "mutation { anything }",
        "mutation Wipe { deleteRepo(name: \"x\") }",
        "subscription { metrics }",
    ],
)
def test_non_query_operations_are_refused(delivery_db, query):
    result = dg.execute(query)
    assert "data" not in result
    assert "read-only" in result["errors"][0]["message"]


def test_schema_has_no_mutation_type():
    assert dg.schema.mutation_type is None
    assert dg.schema.subscription_type is None


def test_syntax_error_is_reported(delivery_db):
    result = dg.execute("{ repos {{ ")
    assert "syntax error" in result["errors"][0]["message"]


def test_oversized_query_is_refused(delivery_db):
    result = dg.execute("{ repos { name } }" + " " * (dg.MAX_QUERY_CHARS + 1))
    assert result["errors"][0]["message"] == "query too long"


def test_definitions_are_exposed(delivery_db):
    result = dg.execute("{ definitions units }")
    assert any(d.startswith("pr_cycle_time:") for d in result["data"]["definitions"])
    assert any("creation to merge" in d for d in result["data"]["definitions"])
    assert any("hours" in u for u in result["data"]["units"])


def test_over_http(delivery_db):
    """Drive the real HTTP handler, not just execute()."""
    httpd = dg.serve("127.0.0.1", 0)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        body = json.dumps({"query": "{ repos { name } }"}).encode()
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/graphql",
            data=body,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            payload = json.loads(response.read())
        assert payload["data"]["repos"][0]["name"] == FIXTURE_REPO

        bad = urllib.request.Request(
            f"http://127.0.0.1:{port}/graphql",
            data=json.dumps({"query": "mutation { x }"}).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(bad, timeout=10) as response:
            payload = json.loads(response.read())
        assert "read-only" in payload["errors"][0]["message"]
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)
