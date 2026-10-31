"""Command line access to the audit log and the usage meter.

    python govctl.py audit --user bob --decision deny
    python govctl.py audit --tool list_issues --json
    python govctl.py usage --group user
    python govctl.py usage --group tool --json
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

import audit
import metering


def render_table(rows: list[dict[str, Any]], columns: list[str]) -> str:
    """Plain fixed-width table. No dependencies, no truncation surprises."""
    if not rows:
        return "(no rows)"

    def cell(value: Any) -> str:
        if value is None:
            return "-"
        if isinstance(value, float):
            return f"{value:.6f}"
        return str(value)

    widths = {c: len(c) for c in columns}
    for row in rows:
        for c in columns:
            widths[c] = max(widths[c], len(cell(row.get(c))))

    header = "  ".join(c.ljust(widths[c]) for c in columns)
    rule = "  ".join("-" * widths[c] for c in columns)
    body = [
        "  ".join(cell(row.get(c)).ljust(widths[c]) for c in columns) for row in rows
    ]
    return "\n".join([header, rule, *body])


def cmd_audit(args: argparse.Namespace) -> int:
    rows = audit.query(
        user=args.user,
        tool=args.tool,
        decision=args.decision,
        since=args.since,
        until=args.until,
        limit=args.limit,
        db=args.db,
    )
    if args.json:
        json.dump(rows, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0

    flat = [
        {
            "ts": r["ts"],
            "user": r["user"],
            "role": r["role"],
            "tool": r["tool"],
            "decision": r["decision"],
            "outcome": r["outcome"],
            "ms": f"{r['duration_ms']:.1f}",
            "reason": r["reason"],
            "arguments": json.dumps(r["arguments"], sort_keys=True),
        }
        for r in rows
    ]
    columns = ["ts", "user", "role", "tool", "decision", "outcome", "ms", "reason"]
    print(render_table(flat, columns))
    print(f"\n{len(rows)} row(s)")
    return 0


def cmd_usage(args: argparse.Namespace) -> int:
    try:
        prices = metering.load_prices(args.prices)
    except metering.PriceError as exc:
        print(f"price table error: {exc}", file=sys.stderr)
        return 2

    result = metering.report(
        group=args.group,
        user=args.user,
        since=args.since,
        until=args.until,
        prices=prices,
        db=args.db,
    )
    if args.json:
        json.dump(result, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0

    if args.group == "tool":
        columns = ["user", "tool", "calls"]
    else:
        columns = [args.group, "model", "turns", "input_tokens", "output_tokens",
                   "estimated_cost"]
    print(render_table(result["rows"], columns))
    print()
    print(f"totals: {json.dumps(result['totals'])}  currency: {result['currency']}")
    print(f"cost basis: {result['cost_basis']}")
    if result["models_without_prices"]:
        print(f"no prices configured for: {', '.join(result['models_without_prices'])}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default=None, help="governance database path (or GOVERNANCE_DB)")
    sub = parser.add_subparsers(dest="command", required=True)

    log = sub.add_parser("audit", help="query the audit log")
    log.add_argument("--user")
    log.add_argument("--tool")
    log.add_argument("--decision", choices=["allow", "deny"])
    log.add_argument("--since", help="ISO-8601 lower bound on the timestamp")
    log.add_argument("--until", help="ISO-8601 upper bound on the timestamp")
    log.add_argument("--limit", type=int, default=50)
    log.add_argument("--json", action="store_true", help="export as JSON")
    log.set_defaults(func=cmd_audit)

    use = sub.add_parser("usage", help="report token usage and estimated cost")
    use.add_argument("--group", choices=["user", "day", "tool"], default="user")
    use.add_argument("--user")
    use.add_argument("--since", help="YYYY-MM-DD lower bound")
    use.add_argument("--until", help="YYYY-MM-DD upper bound")
    use.add_argument("--prices", default=None, help="price table path (or PRICES_FILE)")
    use.add_argument("--json", action="store_true", help="export as JSON")
    use.set_defaults(func=cmd_usage)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result: int = args.func(args)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
