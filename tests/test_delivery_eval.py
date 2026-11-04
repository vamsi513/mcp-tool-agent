"""The 20 hand-written delivery questions, scored against independent values."""

import pytest

from tests.delivery_eval_core import (
    check,
    expectations,
    load_questions,
    resolve_arguments,
    run_all,
)

REPO, QUESTIONS = load_questions()


def test_there_are_twenty_hand_written_questions():
    assert len(QUESTIONS) == 20
    assert len({q["id"] for q in QUESTIONS}) == 20
    # A useful set needs refusals in it, not only happy answers.
    assert sum(1 for q in QUESTIONS if q["expect"].get("refused")) >= 4


@pytest.mark.parametrize("question", QUESTIONS, ids=[q["id"] for q in QUESTIONS])
def test_question(delivery_db, question):
    from tests.delivery_eval_core import call_tool

    expected = expectations()
    arguments = resolve_arguments(question.get("arguments") or {}, expected)
    result = call_tool(question["tool"], arguments)
    failures = check(question, result, expected)
    assert not failures, f"{question['id']}: " + "; ".join(failures)


def test_whole_eval_reports_a_score(delivery_db):
    results = run_all()
    passed = sum(1 for r in results if r["passed"])
    failing = [(r["id"], r["failures"]) for r in results if not r["passed"]]
    assert passed == len(results), failing
    assert passed == 20


def test_expected_values_come_from_the_fixture_not_from_sql():
    """The expectations must not be produced by the code under test.

    Checked on the import graph rather than the text, so a module may name
    delivery_metrics in a comment while never importing it.
    """
    import ast
    from pathlib import Path

    here = Path(__file__).parent
    for name in ("independent_metrics.py", "delivery_eval_core.py"):
        tree = ast.parse((here / name).read_text())
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        assert "delivery_metrics" not in imported, f"{name} imports the code under test"
        assert "delivery_graphql" not in imported, name
