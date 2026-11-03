"""Score the hand-written delivery questions.

No ingest is needed first. The runner builds its own temporary database from
the recorded fixture in tests/fixtures, using the same ingest code path the
tests use, and deletes it when it exits. It never reads or writes the default
database, and it never touches a database it did not create unless you point
it at one with --db.

Two modes:

  tools (default)  calls the tools directly. Deterministic, no model, no
                   network. This is what the test suite runs.
  real             drives the agent against the governed server with a real
                   model, then rule-checks the final answer for the expected
                   number and at least one cited identifier.

    python evals/run_delivery_eval.py
    python evals/run_delivery_eval.py --json
    python evals/run_delivery_eval.py --mode real --model gpt-4o-mini --user bob
    python evals/run_delivery_eval.py --db ./governance.db   # see the warning
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import audit  # noqa: E402
import delivery_fixtures  # noqa: E402
from tests.delivery_eval_core import (  # noqa: E402
    expectations,
    load_questions,
    run_all,
)

CITATION_PATTERN = re.compile(r"\d{2,}")

NUMBER_WORDS = {
    0: "zero", 1: "one", 2: "two", 3: "three", 4: "four", 5: "five", 6: "six",
    7: "seven", 8: "eight", 9: "nine", 10: "ten", 11: "eleven", 12: "twelve",
    13: "thirteen", 14: "fourteen", 15: "fifteen", 16: "sixteen",
    17: "seventeen", 18: "eighteen", 19: "nineteen", 20: "twenty",
}

# Ways an answer can decline, beyond the plain substrings below.
REFUSAL_PATTERNS = (
    re.compile(r"\bno\b[^.]{0,60}\bmetric"),
    re.compile(r"(?:not|isn't|aren't|does not|doesn't|do not|don't)\s+support"),
)

REFUSAL_PHRASES = (
    "not been ingested", "no data", "cannot", "can't", "unable",
    "nothing to report", "not available", "unknown metric",
    "not in the ingested",
    # A policy denial is also a refusal: the governed server can turn a
    # request down before the tool ever gets a chance to.
    "denied by policy", "not in the allowed list", "may not call",
    # Ways a model declines in its own words.
    "not one of", "does not exist", "doesn't exist", "don't have",
    "do not have", "isn't available", "is not available", "not supported",
)


def _mentions_number(answer: str, value: Any) -> bool:
    """True if the answer states `value`, allowing light rounding.

    A candidate has to appear as a number in its own right, so a rounded form
    does not match a digit buried in some other number. Two refinements on
    top of that:

    * A trailing "." is sentence punctuation unless a digit follows it, so
      "3.5." states 3.5 while "3.57" still does not.
    * An integer-valued float is also accepted written as an integer, and a
      small whole number spelled out as a word, because that is how an answer
      in prose usually puts it.
    """
    if value is None or isinstance(value, bool):
        return False

    text = answer.replace(",", "")
    candidates = {str(value)}
    whole: int | None = value if isinstance(value, int) else None

    if isinstance(value, float):
        candidates |= {f"{value:.{places}f}" for places in (1, 2, 3, 4)}
        if 0 < value < 1:
            # A ratio is often reported as a percentage.
            candidates |= {f"{value * 100:.{places}f}" for places in (1, 2)}
        if value.is_integer():
            whole = int(value)
            candidates.add(str(whole))

    for candidate in candidates:
        if re.search(rf"(?<![\d.]){re.escape(candidate)}(?!\d)(?!\.\d)", text):
            return True

    if whole is not None and whole in NUMBER_WORDS:
        if re.search(rf"\b{NUMBER_WORDS[whole]}\b", text, re.IGNORECASE):
            return True
    return False


def score_real_answer(question: dict, answer: str, expected: dict[str, Any]) -> list[str]:
    """Rule checks over a free-text answer. No model grades anything."""
    failures: list[str] = []
    want = question["expect"]
    lowered = answer.lower()

    if want.get("refused"):
        refusing = any(phrase in lowered for phrase in REFUSAL_PHRASES) or any(
            pattern.search(lowered) for pattern in REFUSAL_PATTERNS
        )
        if not refusing:
            failures.append("expected the answer to refuse, it did not")
        for name in ("cycle_time_median", "failure_rate", "merges_per_week"):
            if _mentions_number(answer, expected[name]):
                failures.append(f"a refusal quoted the {name} figure anyway")
        return failures

    for field in ("value", "mean", "total_merges", "failures", "completed_runs",
                  "sample_size"):
        if field in want and not _mentions_number(answer, expected[want[field]]):
            failures.append(f"answer does not state the expected {field}")

    if "citations" in want and not CITATION_PATTERN.search(answer):
        failures.append("answer cites no pull request or run identifier")

    return failures


def describe_exception(exc: BaseException) -> list[str]:
    """Flatten an exception into the underlying causes, one line each.

    anyio wraps a failure inside the agent in nested ExceptionGroups, so the
    bare message is "unhandled errors in a TaskGroup" and says nothing about
    what actually went wrong. This walks the group and the __cause__ chain
    and names the real error with the frame it came from.
    """
    if isinstance(exc, BaseExceptionGroup):
        lines: list[str] = []
        for sub in exc.exceptions:
            lines.extend(describe_exception(sub))
        return lines or [f"{type(exc).__name__}: {exc}"]

    where = ""
    traceback = exc.__traceback__
    last = None
    while traceback is not None:
        last = traceback
        traceback = traceback.tb_next
    if last is not None:
        frame = last.tb_frame
        where = f" at {Path(frame.f_code.co_filename).name}:{last.tb_lineno}"

    described = [f"{type(exc).__module__}.{type(exc).__name__}: {exc}{where}"]
    cause = exc.__cause__ or exc.__context__
    if cause is not None and cause is not exc:
        described.extend(f"  caused by {line}" for line in describe_exception(cause))
    return described


@contextmanager
def _record_tool_calls() -> Iterator[list[dict]]:
    """Capture every tool call the agent makes, with its arguments and result.

    Wraps agent.call_tool_safely for the duration, which is the one place
    every call goes through. The list is shared, so a caller clears it
    between questions.
    """
    import agent

    calls: list[dict] = []
    original = agent.call_tool_safely

    async def recording(session: Any, name: str, arguments_json: str) -> str:
        result = await original(session, name, arguments_json)
        calls.append({"tool": name, "arguments": arguments_json, "result": result})
        return result

    agent.call_tool_safely = recording  # type: ignore[assignment]
    try:
        yield calls
    finally:
        agent.call_tool_safely = original  # type: ignore[assignment]


async def _run_real(model: str, user: str, max_turns: int) -> list[dict]:
    import agent

    _, questions = load_questions()
    expected = expectations()
    results = []

    with _record_tool_calls() as calls:
        for question in questions:
            calls.clear()
            started = datetime.now(UTC).isoformat(timespec="milliseconds")
            answer = ""
            failures: list[str]
            try:
                answer = await agent.run(
                    question["question"], model=model, max_turns=max_turns, user=user
                )
            except BaseException as exc:
                # Report the sub-exception, not "unhandled errors in a TaskGroup".
                failures = describe_exception(exc)
            else:
                failures = score_real_answer(question, answer, expected)

            # The governed server writes one audit row per attempt, so this
            # shows what the policy decided even when the tool never ran.
            try:
                decisions = audit.query(since=started, limit=100)
            except Exception:
                decisions = []

            results.append(
                {
                    "id": question["id"],
                    "question": question["question"],
                    "tool": question["tool"],
                    "passed": not failures,
                    "failures": failures,
                    "answer": answer,
                    "trace": list(calls),
                    "audit": list(reversed(decisions)),
                }
            )
    return results


@contextmanager
def eval_database(explicit: str | None) -> Iterator[Path]:
    """Make the database the tools will read, and point them at it.

    Without --db this is a throwaway built from the recorded fixture and
    removed afterwards. GOVERNANCE_DB is set for the duration so both the
    in-process tools and the governed server the agent spawns read that file
    and nothing else; the previous value is restored on the way out.
    """
    previous = os.environ.get("GOVERNANCE_DB")

    def point_at(path: Path) -> None:
        os.environ["GOVERNANCE_DB"] = str(path)

    try:
        if explicit:
            target = Path(explicit).expanduser().resolve()
            print(
                "warning: --db reads an existing database. The expected answers "
                "come from the recorded fixture in tests/fixtures, so they will "
                "not match live data and questions will be reported as failures "
                "even when the metrics are correct.",
                file=sys.stderr,
            )
            if not target.is_file():
                print(f"warning: {target} does not exist yet", file=sys.stderr)
            point_at(target)
            yield target
        else:
            with delivery_fixtures.ingested_database() as target:
                point_at(target)
                yield target
    finally:
        if previous is None:
            os.environ.pop("GOVERNANCE_DB", None)
        else:
            os.environ["GOVERNANCE_DB"] = previous


def _clip(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit] + f"...[{len(text)} chars]"


def print_trace(results: list[dict], mode: str, width: int = 1200) -> None:
    """Per question: the calls the model made, what came back, what policy said."""
    print("=" * 78)
    print("TRACE")
    if mode == "tools":
        print(
            "Tools mode calls the tools in process, so the governed server is "
            "not involved and there are no policy decisions to show. Use "
            "--mode real to see them."
        )
    print("=" * 78)

    for row in results:
        mark = "pass" if row["passed"] else "FAIL"
        print()
        print(f"[{mark}] {row['id']}")
        print(f"  question    : {row['question']}")

        calls = row.get("trace") or []
        if not calls:
            print("  tool calls  : none")
        for index, call in enumerate(calls, start=1):
            arguments = call["arguments"]
            if not isinstance(arguments, str):
                arguments = json.dumps(arguments, sort_keys=True)
            print(f"  call {index}      : {call['tool']}({_clip(arguments, 300)})")
            print(f"  result {index}    : {_clip(call['result'], width)}")

        decisions = row.get("audit") or []
        if decisions:
            for decision in decisions:
                reason = f"  reason={decision['reason']}" if decision["reason"] else ""
                print(
                    f"  policy      : {decision['tool']} -> {decision['decision']}"
                    f" ({decision['outcome']}){reason}"
                )
                print(f"                args={json.dumps(decision['arguments'], sort_keys=True)}")
        elif mode == "real":
            print("  policy      : no audit rows (the tool was never reached)")

        if "answer" in row:
            print(f"  answer      : {_clip(row['answer'], width)}")
        for failure in row["failures"]:
            print(f"  failure     : {failure}")


def rescore(path: str) -> list[dict]:
    """Re-score answers saved by an earlier run, with the current scorer.

    Isolates the scorer's effect: the answers are fixed, so any change in the
    outcome comes from the rules alone. Each saved row is matched to its
    question by id, so the expectations are the current ones.
    """
    document = json.loads(Path(path).read_text())
    saved = document.get("results") or []
    _, questions = load_questions()
    by_id = {question["id"]: question for question in questions}
    expected = expectations()

    rescored = []
    for row in saved:
        question = by_id.get(row["id"])
        if question is None:
            rescored.append(
                {
                    "id": row["id"],
                    "question": row.get("question", ""),
                    "tool": row.get("tool", ""),
                    "passed": False,
                    "failures": ["no question with this id in the current file"],
                    "was_passing": bool(row.get("passed")),
                    "previous_failures": row.get("failures") or [],
                    "answer": row.get("answer", ""),
                }
            )
            continue
        failures = score_real_answer(question, row.get("answer", ""), expected)
        rescored.append(
            {
                "id": row["id"],
                "question": question["question"],
                "tool": question["tool"],
                "passed": not failures,
                "failures": failures,
                "was_passing": bool(row.get("passed")),
                "previous_failures": row.get("failures") or [],
                "answer": row.get("answer", ""),
            }
        )
    return rescored


def report_rescore(results: list[dict], path: str) -> None:
    width = max(len(row["id"]) for row in results)
    before = sum(1 for row in results if row["was_passing"])
    after = sum(1 for row in results if row["passed"])

    print(f"re-scored {path}")
    print(f"saved answers: {len(results)}")
    print()
    for row in results:
        was = "pass" if row["was_passing"] else "FAIL"
        now = "pass" if row["passed"] else "FAIL"
        moved = "  <- changed" if was != now else ""
        print(f"{was} -> {now}  {row['id'].ljust(width)}{moved}")
        for failure in row["previous_failures"]:
            print(f"      was : {failure}")
        for failure in row["failures"]:
            print(f"      now : {failure}")
    print()
    print(f"as recorded          : {before}/{len(results)}")
    print(f"re-scored             : {after}/{len(results)}")
    changed = [row["id"] for row in results if row["was_passing"] != row["passed"]]
    print(f"questions that changed: {changed if changed else 'none'}")


def report(results: list[dict], mode: str) -> None:
    width = max(len(r["id"]) for r in results)
    print(f"mode={mode}  questions={len(results)}")
    print()
    for row in results:
        mark = "pass" if row["passed"] else "FAIL"
        print(f"{mark}  {row['id'].ljust(width)}  {row['tool']}")
        for failure in row["failures"]:
            print(f"        - {failure}")
    passed = sum(1 for r in results if r["passed"])
    print()
    print(f"{passed}/{len(results)} passed")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--mode", choices=["tools", "real"], default="tools")
    parser.add_argument("--model", default=os.getenv("OPENAI_MODEL", "gpt-4o-mini"))
    parser.add_argument("--user", default="bob", help="policy identity for --mode real")
    parser.add_argument("--max-turns", type=int, default=6)
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--show-trace",
        action="store_true",
        help="print the tool calls, their results and the policy decisions "
             "behind every question",
    )
    parser.add_argument(
        "--rescore",
        default=None,
        metavar="FILE",
        help="re-score answers saved by an earlier run with the current "
             "scorer, instead of running anything",
    )
    parser.add_argument(
        "--db",
        default=None,
        help="use this existing database instead of building one from the "
             "recorded fixture; expected values will not match live data",
    )
    args = parser.parse_args(argv)

    if args.rescore:
        rescored = rescore(args.rescore)
        if args.json:
            json.dump(
                {
                    "rescored_from": args.rescore,
                    "as_recorded": sum(1 for r in rescored if r["was_passing"]),
                    "rescored": sum(1 for r in rescored if r["passed"]),
                    "total": len(rescored),
                    "results": rescored,
                },
                sys.stdout,
                indent=2,
            )
            sys.stdout.write("\n")
        else:
            report_rescore(rescored, args.rescore)
        return 0

    if args.mode == "real" and not os.getenv("OPENAI_API_KEY"):
        print("OPENAI_API_KEY is not set; --mode real needs it", file=sys.stderr)
        return 2

    with eval_database(args.db):
        if args.mode == "real":
            results = asyncio.run(_run_real(args.model, args.user, args.max_turns))
        else:
            results = run_all()

    if args.json:
        json.dump(
            {
                "mode": args.mode,
                "passed": sum(1 for r in results if r["passed"]),
                "total": len(results),
                "results": results,
            },
            sys.stdout,
            indent=2,
        )
        sys.stdout.write("\n")
    else:
        if args.show_trace:
            print_trace(results, args.mode)
            print()
        report(results, args.mode)

    return 0 if all(r["passed"] for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
