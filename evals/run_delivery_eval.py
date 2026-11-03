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


def _mentions_number(answer: str, value: Any) -> bool:
    """True if the answer states `value`, allowing light rounding.

    Candidates have to appear as a number in their own right. Matching on a
    bare substring let a rounded form such as "2" (from 1.5422 at zero
    decimal places) hit any digit anywhere in the text, so whole-number
    rounding is not offered and every candidate is anchored.
    """
    if value is None or isinstance(value, bool):
        return False

    text = answer.replace(",", "")
    candidates = {str(value)}
    if isinstance(value, float):
        candidates |= {f"{value:.{places}f}" for places in (1, 2, 3, 4)}
        if 0 < value < 1:
            # A ratio is often reported as a percentage.
            candidates |= {f"{value * 100:.{places}f}" for places in (1, 2)}

    return any(
        re.search(rf"(?<![\d.]){re.escape(candidate)}(?![\d.])", text)
        for candidate in candidates
    )


def score_real_answer(question: dict, answer: str, expected: dict[str, Any]) -> list[str]:
    """Rule checks over a free-text answer. No model grades anything."""
    failures: list[str] = []
    want = question["expect"]
    lowered = answer.lower()

    if want.get("refused"):
        refusing = any(
            phrase in lowered
            for phrase in ("not been ingested", "no data", "cannot", "can't", "unable",
                           "nothing to report", "not available", "unknown metric",
                           "not in the ingested",
                           # A policy denial is also a refusal: the governed
                           # server can turn a request down before the tool
                           # ever gets a chance to.
                           "denied by policy", "not in the allowed list",
                           "may not call")
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
            except Exception as exc:
                failures = [f"{exc.__class__.__name__}: {exc}"]
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
        "--db",
        default=None,
        help="use this existing database instead of building one from the "
             "recorded fixture; expected values will not match live data",
    )
    args = parser.parse_args(argv)

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
