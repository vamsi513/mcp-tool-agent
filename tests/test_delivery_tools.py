"""Tool behaviour: citations on every answer, refusal when data is missing."""

import delivery_tools as dt
from tests import independent_metrics as im
from tests.conftest import FIXTURE_REPO


def test_list_repos_reports_what_was_ingested(delivery_db):
    result = dt.list_delivery_repos()
    assert result["count"] == 1
    assert result["repos"][0]["repo"] == FIXTURE_REPO
    assert "delivery_metric" not in result["available_metrics"]  # metrics, not tools
    assert "pr_cycle_time" in result["available_metrics"]


def test_metric_answer_carries_citations(delivery_db):
    result = dt.delivery_metric(FIXTURE_REPO, "pr_cycle_time")

    assert result["refused"] is False
    assert result["value"] == im.median(im.cycle_times().values())
    assert result["citations"]["pull_requests"] == sorted(im.cycle_times())
    assert result["definition"]
    assert result["unit"] == "hours"
    # The raw sources key is replaced by citations so there is one place to look.
    assert "sources" not in result


def test_failure_rate_cites_the_failing_runs(delivery_db):
    completed, failed = im.workflow_outcomes()
    result = dt.delivery_metric(FIXTURE_REPO, "workflow_failure_rate")

    assert result["citations"]["failed_runs"] == sorted(failed)
    assert result["failures"] == len(failed)
    assert result["completed_runs"] == len(completed)


def test_refuses_a_repo_that_was_never_ingested(delivery_db):
    result = dt.delivery_metric("nobody/nothing", "pr_cycle_time")

    assert result["refused"] is True
    assert "not been ingested" in result["reason"]
    assert result["ingested_repos"] == [FIXTURE_REPO]
    assert "value" not in result


def test_refuses_an_empty_window_rather_than_reporting_zero(delivery_db):
    result = dt.delivery_metric(
        FIXTURE_REPO, "pr_cycle_time", since="1990-01-01", until="1990-12-31"
    )

    assert result["refused"] is True
    assert result["sample_size"] == 0
    assert result["value"] is None
    assert "nothing to report" in result["reason"]


def test_refuses_an_unknown_metric(delivery_db):
    result = dt.delivery_metric(FIXTURE_REPO, "velocity")
    assert result["refused"] is True
    assert "pr_cycle_time" in result["available_metrics"]


def test_refuses_a_malformed_window(delivery_db):
    result = dt.delivery_metric(FIXTURE_REPO, "pr_cycle_time", since="yesterday")
    assert result["refused"] is True
    assert "YYYY-MM-DD" in result["reason"]


def test_pull_request_detail_resolves_a_citation(delivery_db):
    cited = sorted(im.cycle_times())[0]
    result = dt.pull_request_detail(FIXTURE_REPO, cited)

    assert result["refused"] is False
    assert result["pull_request"]["number"] == cited
    assert result["pull_request"]["merged_at"]
    assert result["review_count"] == len(
        im.raw()["reviews"].get(cited, [])
    )


def test_pull_request_detail_refuses_an_unknown_number(delivery_db):
    result = dt.pull_request_detail(FIXTURE_REPO, 999999)
    assert result["refused"] is True
    assert "not in the ingested data" in result["reason"]


def test_pull_request_detail_refuses_an_unknown_repo(delivery_db):
    result = dt.pull_request_detail("nobody/nothing", 1)
    assert result["refused"] is True


def test_citations_are_truncated_honestly(delivery_db, monkeypatch):
    monkeypatch.setattr(dt, "MAX_CITED", 2)
    result = dt.delivery_metric(FIXTURE_REPO, "pr_cycle_time")

    assert len(result["citations"]["pull_requests"]) == 2
    note = result["citations"]["pull_requests_truncated"]
    assert note["shown"] == 2
    assert note["total"] == len(im.cycle_times())
    assert "used all of them" in note["note"]


def test_every_tool_is_registered_on_the_governed_server():
    import governed_server

    for tool in dt.TOOLS:
        assert tool.__name__ in governed_server.TOOL_REGISTRY
