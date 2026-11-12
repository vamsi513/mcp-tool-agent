"""Command line administration for the gateway.

    python -m gateway.cli keys create --id ci --budget 10
    python -m gateway.cli keys list
    python -m gateway.cli budget set --id ci --budget 25
    python -m gateway.cli usage show
    python -m gateway.cli requests tail -n 20

Every command takes --json, and every command exits non-zero with a message
on stderr when it cannot do what was asked.

There is no second store. Keys are the ones in the gateway config file, which
is the only place the running gateway reads them from, and usage comes from
the same `usage_llm` rows the gateway writes and the budgets are derived from.

Two consequences of that, which the commands say out loud rather than paper
over:

* Writing a key or a budget rewrites the config file through a YAML load and
  dump, so comments and layout in it are not preserved. The file is written
  atomically and keeps its permissions, and the committed example is never
  written to.
* The meter records which key called which model with how many tokens, and
  nothing else. There is no per-request latency or HTTP status in it, so
  `requests tail` does not invent either; see its help text.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

import metering
from gateway import budgets
from gateway.config import (
    DEFAULT_CONFIG_PATH,
    EXAMPLE_CONFIG_PATH,
    ConfigError,
    GatewayConfig,
    load_config,
    parse_config,
)

KEY_PREFIX = "gw-"
KEY_BYTES = 32


class CliError(Exception):
    """Something the user asked for that cannot be done, with a reason."""


# --------------------------------------------------------------- the config


def resolve_config_path(explicit: str | os.PathLike[str] | None) -> Path:
    """Where the config is, following the same order `load_config` uses."""
    if explicit is not None:
        return Path(explicit)
    from_env = os.getenv("GATEWAY_CONFIG")
    if from_env:
        return Path(from_env)
    if DEFAULT_CONFIG_PATH.is_file():
        return DEFAULT_CONFIG_PATH
    return EXAMPLE_CONFIG_PATH


def writable_config_path(explicit: str | os.PathLike[str] | None) -> Path:
    """The config to write to, refusing the committed example."""
    path = resolve_config_path(explicit)
    if path.resolve() == EXAMPLE_CONFIG_PATH.resolve():
        raise CliError(
            f"refusing to write to {path}, which is the committed example. "
            f"Copy it to {DEFAULT_CONFIG_PATH.name} and edit that instead."
        )
    if not path.is_file():
        raise CliError(f"no config file at {path}")
    return path


def read_document(path: Path) -> dict[str, Any]:
    try:
        raw = yaml.safe_load(path.read_text())
    except OSError as exc:
        raise CliError(f"could not read {path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise CliError(f"{path} is not valid YAML: {exc}") from exc
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise CliError(f"{path} must contain a mapping at the top level")
    return raw


def write_document(path: Path, document: dict[str, Any]) -> None:
    """Replace the config, atomically, keeping its permissions.

    Validated before it lands: a document that would not load is a bug worth
    catching here rather than at the gateway's next start.
    """
    try:
        parse_config(document)
    except ConfigError as exc:
        raise CliError(f"refusing to write a config that would not load: {exc}") from exc

    mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
    temporary = path.with_name(path.name + ".tmp")
    try:
        temporary.write_text(yaml.safe_dump(document, sort_keys=False))
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        raise CliError(f"could not write {path}: {exc}") from exc


def load_settings(explicit: str | os.PathLike[str] | None) -> GatewayConfig:
    try:
        return load_config(resolve_config_path(explicit))
    except ConfigError as exc:
        raise CliError(str(exc)) from exc


def key_entries(document: dict[str, Any]) -> list[dict[str, Any]]:
    entries = document.setdefault("keys", [])
    if not isinstance(entries, list):
        raise CliError("keys in the config file must be a list")
    return entries


def find_entry(document: dict[str, Any], key_id: str) -> dict[str, Any]:
    for entry in key_entries(document):
        if isinstance(entry, dict) and entry.get("id") == key_id:
            return entry
    known = sorted(
        str(entry.get("id"))
        for entry in key_entries(document)
        if isinstance(entry, dict) and entry.get("id")
    )
    raise CliError(
        f"no key with id {key_id!r}. Configured ids: {', '.join(known) or 'none'}"
    )


def load_prices(explicit: str | os.PathLike[str] | None) -> metering.PriceTable:
    try:
        return metering.load_prices(explicit)
    except metering.PriceError as exc:
        raise CliError(str(exc)) from exc


# ----------------------------------------------------------------- printing


def render_table(rows: list[dict[str, Any]], columns: list[str]) -> str:
    """A plain fixed-width table. Nothing here is ever a key."""
    if not rows:
        return "(no rows)"
    widths = {
        column: max(len(column), *(len(str(row.get(column, ""))) for row in rows))
        for column in columns
    }
    lines = ["  ".join(column.ljust(widths[column]) for column in columns).rstrip()]
    lines.append("  ".join("-" * widths[column] for column in columns))
    for row in rows:
        lines.append(
            "  ".join(
                str(row.get(column, "")).ljust(widths[column]) for column in columns
            ).rstrip()
        )
    return "\n".join(lines)


def emit(payload: Any, as_json: bool, text: str) -> None:
    if as_json:
        json.dump(payload, sys.stdout, indent=2)
        sys.stdout.write("\n")
    else:
        print(text)


def shown(value: Any) -> str:
    return "-" if value is None else str(value)


# ----------------------------------------------------------------- commands


def cmd_keys_create(args: argparse.Namespace) -> int:
    path = writable_config_path(args.config)
    document = read_document(path)

    for entry in key_entries(document):
        if isinstance(entry, dict) and entry.get("id") == args.id:
            raise CliError(f"a key with id {args.id!r} already exists")

    secret = KEY_PREFIX + secrets.token_urlsafe(KEY_BYTES)
    entry = {
        "key": secret,
        "id": args.id,
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    if args.name:
        entry["name"] = args.name
    if args.budget is not None:
        entry["monthly_budget_usd"] = args.budget
    key_entries(document).append(entry)
    write_document(path, document)

    payload = {
        "id": args.id,
        "name": args.name,
        "monthly_budget_usd": args.budget,
        "created_at": entry["created_at"],
        "key": secret,
        "note": "this is the only time the key is shown; store it now",
        "config": str(path),
    }
    text = (
        f"created key {args.id!r} in {path}\n"
        f"  name    {shown(args.name)}\n"
        f"  budget  {shown(args.budget)}\n"
        f"  key     {secret}\n"
        f"\nThis is the only time the key is shown. Store it now; the other\n"
        f"commands only ever print its last four characters."
    )
    emit(payload, args.json, text)
    return 0


def cmd_keys_list(args: argparse.Namespace) -> int:
    settings = load_settings(args.config)
    keys = sorted(settings.keys.values(), key=lambda key: key.id)
    # JSON keeps real types and nulls; the table shows a dash instead.
    rows = [
        {
            "id": key.id,
            "name": key.name,
            "key": key.masked_secret,
            "monthly_budget_usd": key.monthly_budget_usd,
            "created_at": key.created_at,
        }
        for key in keys
    ]
    payload = {
        "config": str(resolve_config_path(args.config)),
        "keys": rows,
        "note": "keys are masked to their last four characters",
    }
    display = [{field: shown(value) for field, value in row.items()} for row in rows]
    emit(
        payload,
        args.json,
        render_table(display, ["id", "name", "key", "monthly_budget_usd", "created_at"]),
    )
    return 0


def cmd_budget_set(args: argparse.Namespace) -> int:
    path = writable_config_path(args.config)
    document = read_document(path)
    entry = find_entry(document, args.id)

    previous = entry.get("monthly_budget_usd")
    if args.unlimited:
        entry.pop("monthly_budget_usd", None)
        new: float | None = None
    else:
        new = args.budget
        entry["monthly_budget_usd"] = new
    write_document(path, document)

    payload = {
        "id": args.id,
        "previous_monthly_budget_usd": previous,
        "monthly_budget_usd": new,
        "config": str(path),
    }
    emit(
        payload,
        args.json,
        f"key {args.id!r}: monthly budget {shown(previous)} -> {shown(new)}",
    )
    return 0


def cmd_usage_show(args: argparse.Namespace) -> int:
    settings = load_settings(args.config)
    prices = load_prices(args.prices)
    since = args.since or budgets.month_start()

    report = metering.report(group="user", since=since, prices=prices, db=args.db)

    # A report row is one key and one model, so a key that used two models
    # has two rows. They are added up here, and a model with no configured
    # price contributes tokens but no cost, which the partial flag says.
    totals: dict[str, dict[str, Any]] = {}
    for row in report["rows"]:
        entry = totals.setdefault(
            str(row.get("user")),
            {"turns": 0, "input_tokens": 0, "output_tokens": 0,
             "cost": 0.0, "partial": False},
        )
        entry["turns"] += int(row.get("turns") or 0)
        entry["input_tokens"] += int(row.get("input_tokens") or 0)
        entry["output_tokens"] += int(row.get("output_tokens") or 0)
        cost = row.get("estimated_cost")
        if cost is None:
            entry["partial"] = True
        else:
            entry["cost"] += float(cost)

    def describe(key_id: str, name: str, budget: Any) -> dict[str, Any]:
        recorded = totals.pop(
            key_id,
            {"turns": 0, "input_tokens": 0, "output_tokens": 0,
             "cost": 0.0, "partial": False},
        )
        return {
            "id": key_id,
            "name": name,
            "turns": recorded["turns"],
            "input_tokens": recorded["input_tokens"],
            "output_tokens": recorded["output_tokens"],
            "estimated_cost": round(recorded["cost"], 6),
            "cost_is_partial": recorded["partial"],
            "monthly_budget_usd": budget,
        }

    rows = [
        describe(key.id, shown(key.name), key.monthly_budget_usd)
        for key in sorted(settings.keys.values(), key=lambda key: key.id)
    ]
    # Usage recorded against an id that is no longer in the config still
    # counts and is still shown, rather than quietly dropped.
    rows.extend(
        describe(user, "(not in the config)", None) for user in sorted(totals)
    )

    payload = {
        "since": since,
        "currency": report["currency"],
        "cost_basis": report["cost_basis"],
        "rows": rows,
        "models_without_prices": report["models_without_prices"],
    }
    display = [
        {
            **row,
            "estimated_cost": (
                f"{row['estimated_cost']}*" if row["cost_is_partial"]
                else str(row["estimated_cost"])
            ),
            "monthly_budget_usd": shown(row["monthly_budget_usd"]),
        }
        for row in rows
    ]
    text = render_table(
        display,
        [
            "id",
            "name",
            "turns",
            "input_tokens",
            "output_tokens",
            "estimated_cost",
            "monthly_budget_usd",
        ],
    )
    text += f"\n\nsince {since}, currency {report['currency']}"
    text += f"\ncost basis: {report['cost_basis']}"
    if any(row["cost_is_partial"] for row in rows):
        text += (
            "\n* this key also used a model with no configured price, so its "
            "cost covers\n  only part of what it spent"
        )
    if report["models_without_prices"]:
        text += "\nno prices configured for: " + ", ".join(
            report["models_without_prices"]
        )
    emit(payload, args.json, text)
    return 0


def cmd_requests_tail(args: argparse.Namespace) -> int:
    if args.number <= 0:
        raise CliError("-n must be a positive number of requests")
    settings = load_settings(args.config)
    turns = metering.recent_llm_turns(limit=args.number, db=args.db)

    rows = []
    for turn in turns:
        model = str(turn.get("model", ""))
        total = int(turn.get("input_tokens") or 0) + int(turn.get("output_tokens") or 0)
        rows.append(
            {
                "ts": turn.get("ts"),
                "key_id": turn.get("user"),
                # Not stored per row: worked out from the routing table the
                # same way the gateway picked it at the time.
                "provider": settings.provider_for(model),
                "model": model,
                # A row exists only for a call that completed and was
                # charged. Zero tokens means the upstream reported none.
                "status": "ok" if total else "no usage reported",
                "input_tokens": turn.get("input_tokens"),
                "output_tokens": turn.get("output_tokens"),
            }
        )

    payload = {
        "rows": rows,
        "notes": [
            "one row per completed upstream call; a refused request is not "
            "recorded here",
            "provider is derived from the configured routes, not stored per "
            "request",
            "the meter records no per-request latency or HTTP status, so "
            "neither is shown",
            "no message content is stored or shown",
        ],
    }
    text = render_table(
        [{field: shown(value) for field, value in row.items()} for row in rows],
        ["ts", "key_id", "provider", "model", "status", "input_tokens", "output_tokens"],
    )
    text += (
        "\n\nprovider is derived from the configured routes. The meter keeps no "
        "latency\nor HTTP status per request, so neither is shown, and no "
        "message content is\nstored anywhere in it."
    )
    emit(payload, args.json, text)
    return 0


# ------------------------------------------------------------------ parsing


def positive_budget(raw: str) -> float:
    try:
        value = float(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{raw!r} is not a number") from exc
    if value < 0:
        raise argparse.ArgumentTypeError("a budget must not be negative")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m gateway.cli",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--config", default=None, help="gateway config path (or GATEWAY_CONFIG)"
    )
    parser.add_argument(
        "--db", default=None, help="governance database path (or GOVERNANCE_DB)"
    )
    parser.add_argument("--json", action="store_true", help="print JSON instead")
    groups = parser.add_subparsers(dest="group", required=True)

    keys = groups.add_parser("keys", help="create and list API keys").add_subparsers(
        dest="action", required=True
    )

    create = keys.add_parser("create", help="create a key and print it once")
    create.add_argument("--id", required=True, help="the id used in usage rows and logs")
    create.add_argument("--name", default=None, help="a note about who it is for")
    create.add_argument(
        "--budget", type=positive_budget, default=None,
        help="monthly budget in the price table's currency; omit for no limit",
    )
    create.set_defaults(func=cmd_keys_create)

    listing = keys.add_parser("list", help="list keys, masked to their last four")
    listing.set_defaults(func=cmd_keys_list)

    budget = groups.add_parser("budget", help="change a key's budget").add_subparsers(
        dest="action", required=True
    )
    setter = budget.add_parser("set", help="set or clear a key's monthly budget")
    setter.add_argument("--id", required=True)
    amount = setter.add_mutually_exclusive_group(required=True)
    amount.add_argument("--budget", type=positive_budget, default=None)
    amount.add_argument(
        "--unlimited", action="store_true", help="remove the budget entirely"
    )
    setter.set_defaults(func=cmd_budget_set)

    usage = groups.add_parser("usage", help="usage and estimated cost").add_subparsers(
        dest="action", required=True
    )
    show = usage.add_parser("show", help="usage per key for the current month")
    show.add_argument("--prices", default=None, help="price table path (or PRICES_FILE)")
    show.add_argument(
        "--since", default=None,
        help="YYYY-MM-DD lower bound; the first of this month by default",
    )
    show.set_defaults(func=cmd_usage_show)

    requests = groups.add_parser(
        "requests", help="recent recorded requests"
    ).add_subparsers(dest="action", required=True)
    tail = requests.add_parser("tail", help="the most recent recorded calls")
    tail.add_argument("-n", "--number", type=int, default=20)
    tail.set_defaults(func=cmd_requests_tail)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result = args.func(args)
    except CliError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return int(result)


if __name__ == "__main__":
    raise SystemExit(main())
