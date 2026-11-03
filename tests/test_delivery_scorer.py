"""Scorer rules, exercised on the exact strings that exposed each defect.

The strings here are the ones the diagnosis was run against: a number ending
a sentence, an integer-valued float written as an integer or a word, and the
ways a model declines a metric it does not have.
"""

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "evals"))

import run_delivery_eval as runner  # noqa: E402

# --- a number at the end of a sentence --------------------------------


@pytest.mark.parametrize(
    "answer",
    [
        "The average number of pull requests merged per week in the repository is 3.5.",
        "...is 3.5 merged pull requests per week.",
        "3.5.",
        "3.50",
        "3.5",
        "about 3.5!",
        "(3.5)",
    ],
)
def test_three_point_five_is_recognised(answer):
    assert runner._mentions_number(answer, 3.5)


@pytest.mark.parametrize(
    "answer",
    [
        "3.57",
        "3.55",
        "13.5",
        "the value is 3.512 hours",
        "23.5",
    ],
)
def test_a_longer_number_is_not_three_point_five(answer):
    assert not runner._mentions_number(answer, 3.5)


def test_trailing_period_was_the_defect():
    """Previously the lookahead treated a full stop as part of the number."""
    assert runner._mentions_number("3.5.", 3.5)
    assert not runner._mentions_number("3.57", 3.5)


# --- integer-valued floats and number words ---------------------------


@pytest.mark.parametrize(
    "answer, value",
    [
        ("The cycle time is based on 7 merged pull requests.", 7),
        ("It is based on 7.", 7),
        ("seven merged pull requests", 7),
        ("Seven merged pull requests", 7),
        ("The median number of reviews per pull request is 0.", 0.0),
        ("The median is 0", 0.0),
        ("The median is 0.0", 0.0),
        ("zero reviews", 0.0),
        ("It covers twelve pull requests", 12),
        ("over 12 pull requests", 12),
        ("five pull requests had a review", 5),
    ],
)
def test_integer_forms_and_words_are_recognised(answer, value):
    assert runner._mentions_number(answer, value)


@pytest.mark.parametrize(
    "answer, value",
    [
        # 0.5 must not satisfy an expected 0.0 via the leading zero.
        ("The median number of reviews per pull request is 0.5.", 0.0),
        ("the ratio is 0.75", 0.0),
        ("17 runs failed", 7),
        ("seventeen runs", 7),
        ("1.54 hours", 5),
    ],
)
def test_near_misses_are_still_rejected(answer, value):
    assert not runner._mentions_number(answer, value)


def test_a_wrong_median_does_not_pass_as_zero():
    """The saved q07 answer said 0.5 where 0.0 was expected."""
    saved = (
        "The median number of reviews per pull request in the "
        "`psf/cachecontrol` repository is 0.5."
    )
    assert not runner._mentions_number(saved, 0.0)


def test_booleans_and_none_never_match():
    assert not runner._mentions_number("true 1", True)
    assert not runner._mentions_number("anything", None)


# --- refusal vocabulary -----------------------------------------------


REFUSAL_QUESTION = {
    "id": "q19-refuse-unknown-metric",
    "expect": {"refused": True, "reason_contains": "unknown metric"},
}


def _expected():
    from tests.delivery_eval_core import expectations

    return expectations()


@pytest.mark.parametrize(
    "answer",
    [
        "There is no team velocity metric available for psf/cachecontrol.",
        "I don't have a velocity metric. The available metrics are pr_cycle_time, "
        "time_to_first_review, review_count, workflow_failure_rate and merge_frequency.",
        "Team velocity is not one of the metrics I can compute.",
        "The tool reported an unknown metric 'velocity'.",
        "I am unable to compute team velocity.",
        "Velocity isn't supported here.",
        "That metric does not exist in this dataset.",
        "Team velocity is not available for this repository.",
        "I do not have a velocity metric.",
    ],
)
def test_refusal_phrasings_are_accepted(answer, delivery_db):
    assert runner.score_real_answer(REFUSAL_QUESTION, answer, _expected()) == []


def test_an_invented_answer_is_still_not_a_refusal(delivery_db):
    """The saved q19 answer fabricated a velocity from merge frequency."""
    saved = (
        "The team velocity for the `psf/cachecontrol` repository is "
        "approximately 3.5 merged pull requests per week."
    )
    failures = runner.score_real_answer(REFUSAL_QUESTION, saved, _expected())

    assert failures
    assert any("did not" in failure for failure in failures)
    assert any("merges_per_week" in failure for failure in failures)


def test_a_refusal_that_quotes_a_figure_is_flagged(delivery_db):
    answer = "I cannot compute velocity, but the cycle time median is 1.5422 hours."
    failures = runner.score_real_answer(REFUSAL_QUESTION, answer, _expected())
    assert any("cycle_time_median" in failure for failure in failures)


# --- re-scoring saved answers -----------------------------------------


SAVED_RUN = REPO_ROOT / "evals" / "results" / "delivery-real-run2-before-fixes.json"


@pytest.mark.skipif(not SAVED_RUN.is_file(), reason="saved run not present")
def test_rescoring_the_saved_run_needs_no_database_or_network():
    """Re-scoring only replays stored answers through the current rules."""
    results = runner.rescore(str(SAVED_RUN))

    assert len(results) == 20
    assert all("was_passing" in row for row in results)

    as_recorded = sum(1 for row in results if row["was_passing"])
    rescored = sum(1 for row in results if row["passed"])
    assert as_recorded == 13

    # The scorer fixes changed no question outcome on this run: every failure
    # had at least one cause the rules were right to flag.
    assert rescored == as_recorded
    assert [row["id"] for row in results if row["was_passing"] != row["passed"]] == []


@pytest.mark.skipif(not SAVED_RUN.is_file(), reason="saved run not present")
def test_rescoring_resolves_only_the_trailing_period_failure():
    results = {row["id"]: row for row in runner.rescore(str(SAVED_RUN))}

    q12 = results["q12-merge-frequency"]
    resolved = set(q12["previous_failures"]) - set(q12["failures"])
    assert resolved == {"answer does not state the expected value"}
    # It still fails, because the answer never states the total.
    assert not q12["passed"]
    assert q12["failures"] == ["answer does not state the expected total_merges"]
