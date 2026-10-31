"""Per-user usage and estimated cost.

The agent writes one row per LLM turn (token counts as reported by the API)
and one row per tool call. Cost is computed from a price table in YAML and is
only ever an estimate: it is as accurate as the numbers in that file, and the
file ships with zeros so nothing is implied about any provider's pricing.
"""

from __future__ import annotations

import os
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from store import connect

DEFAULT_PRICES_PATH = Path(__file__).parent / "prices.yaml"
EXAMPLE_PRICES_PATH = Path(__file__).parent / "prices.example.yaml"

TOKENS_PER_PRICE_UNIT = 1_000_000

_SCHEMA = """
CREATE TABLE IF NOT EXISTS usage_llm (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            TEXT    NOT NULL,
    day           TEXT    NOT NULL,
    user          TEXT    NOT NULL,
    model         TEXT    NOT NULL,
    input_tokens  INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS usage_tool_calls (
    id   INTEGER PRIMARY KEY AUTOINCREMENT,
    ts   TEXT NOT NULL,
    day  TEXT NOT NULL,
    user TEXT NOT NULL,
    tool TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS usage_llm_user_idx  ON usage_llm (user);
CREATE INDEX IF NOT EXISTS usage_llm_day_idx   ON usage_llm (day);
CREATE INDEX IF NOT EXISTS usage_tool_user_idx ON usage_tool_calls (user);
"""


class PriceError(Exception):
    """Raised when the price table is unreadable or malformed."""


@dataclass(frozen=True)
class ModelPrice:
    input_per_million: float
    output_per_million: float

    def cost(self, input_tokens: int, output_tokens: int) -> float:
        return (
            input_tokens / TOKENS_PER_PRICE_UNIT * self.input_per_million
            + output_tokens / TOKENS_PER_PRICE_UNIT * self.output_per_million
        )


@dataclass(frozen=True)
class PriceTable:
    currency: str
    models: dict[str, ModelPrice]

    def cost(self, model: str, input_tokens: int, output_tokens: int) -> float | None:
        """Estimated cost, or None when the model is not in the table."""
        price = self.models.get(model)
        if price is None:
            return None
        return price.cost(input_tokens, output_tokens)


def parse_prices(raw: Any) -> PriceTable:
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise PriceError("price document must be a mapping")

    currency = raw.get("currency", "USD")
    if not isinstance(currency, str) or not currency:
        raise PriceError("currency must be a non-empty string")

    models_raw = raw.get("models") or {}
    if not isinstance(models_raw, dict):
        raise PriceError("models must be a mapping of model name to prices")

    models: dict[str, ModelPrice] = {}
    for name, entry in models_raw.items():
        if not isinstance(entry, dict):
            raise PriceError(f"models.{name} must be a mapping")
        try:
            models[str(name)] = ModelPrice(
                input_per_million=float(entry.get("input_per_million", 0.0)),
                output_per_million=float(entry.get("output_per_million", 0.0)),
            )
        except (TypeError, ValueError) as exc:
            raise PriceError(f"models.{name} has non-numeric prices: {exc}") from exc

    return PriceTable(currency=currency, models=models)


def load_prices(path: str | os.PathLike[str] | None = None) -> PriceTable:
    """Read the price table: explicit path, then PRICES_FILE, then
    prices.yaml, then the committed prices.example.yaml."""
    candidates: list[Path] = []
    if path is not None:
        candidates.append(Path(path))
    else:
        from_env = os.getenv("PRICES_FILE")
        if from_env:
            candidates.append(Path(from_env))
        candidates.append(DEFAULT_PRICES_PATH)
        candidates.append(EXAMPLE_PRICES_PATH)

    for candidate in candidates:
        if candidate.is_file():
            try:
                raw = yaml.safe_load(candidate.read_text())
            except yaml.YAMLError as exc:
                raise PriceError(f"{candidate} is not valid YAML: {exc}") from exc
            return parse_prices(raw)

    tried = ", ".join(str(c) for c in candidates)
    raise PriceError(f"no price file found (tried: {tried})")


def init(db: str | os.PathLike[str] | None = None) -> None:
    with closing(connect(db)) as conn, conn:
        conn.executescript(_SCHEMA)


def record_llm_turn(
    user: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    db: str | os.PathLike[str] | None = None,
) -> None:
    """Record the token counts the API reported for one completion."""
    init(db)
    now = datetime.now(UTC)
    with closing(connect(db)) as conn, conn:
        conn.execute(
            "INSERT INTO usage_llm (ts, day, user, model, input_tokens, output_tokens)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (
                now.isoformat(timespec="milliseconds"),
                now.date().isoformat(),
                user,
                model,
                int(input_tokens),
                int(output_tokens),
            ),
        )


def record_tool_call(
    user: str, tool: str, db: str | os.PathLike[str] | None = None
) -> None:
    """Record that `user` invoked `tool` once."""
    init(db)
    now = datetime.now(UTC)
    with closing(connect(db)) as conn, conn:
        conn.execute(
            "INSERT INTO usage_tool_calls (ts, day, user, tool) VALUES (?, ?, ?, ?)",
            (now.isoformat(timespec="milliseconds"), now.date().isoformat(), user, tool),
        )


def _where(user: str | None, since: str | None, until: str | None) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    args: list[Any] = []
    if user:
        clauses.append("user = ?")
        args.append(user)
    if since:
        clauses.append("day >= ?")
        args.append(since)
    if until:
        clauses.append("day <= ?")
        args.append(until)
    return (f"WHERE {' AND '.join(clauses)}" if clauses else "", args)


def report(
    group: str = "user",
    user: str | None = None,
    since: str | None = None,
    until: str | None = None,
    prices: PriceTable | None = None,
    db: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Aggregate usage. `group` is one of user, day or tool.

    Rows carry token totals plus an `estimated_cost` computed from the price
    table. A model absent from that table yields a null cost and is listed
    under `models_without_prices`.
    """
    if group not in ("user", "day", "tool"):
        raise ValueError("group must be one of: user, day, tool")
    if prices is None:
        prices = load_prices()

    init(db)
    where, args = _where(user, since, until)

    rows: list[dict[str, Any]] = []
    missing: set[str] = set()

    if group == "tool":
        sql = (
            "SELECT user, tool, COUNT(*) AS calls FROM usage_tool_calls "
            f"{where} GROUP BY user, tool ORDER BY calls DESC, user, tool"
        )
        with closing(connect(db)) as conn:
            for row in conn.execute(sql, args).fetchall():
                rows.append({"user": row["user"], "tool": row["tool"], "calls": row["calls"]})
    else:
        key = "user" if group == "user" else "day"
        sql = (
            f"SELECT {key} AS key, model, COUNT(*) AS turns,"
            " SUM(input_tokens) AS input_tokens, SUM(output_tokens) AS output_tokens"
            f" FROM usage_llm {where} GROUP BY {key}, model ORDER BY {key}, model"
        )
        with closing(connect(db)) as conn:
            fetched = conn.execute(sql, args).fetchall()
        for row in fetched:
            input_tokens = int(row["input_tokens"] or 0)
            output_tokens = int(row["output_tokens"] or 0)
            cost = prices.cost(row["model"], input_tokens, output_tokens)
            if cost is None:
                missing.add(row["model"])
            rows.append(
                {
                    group: row["key"],
                    "model": row["model"],
                    "turns": row["turns"],
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "estimated_cost": None if cost is None else round(cost, 6),
                }
            )

    totals: dict[str, Any] = {}
    if group != "tool":
        totals = {
            "input_tokens": sum(r["input_tokens"] for r in rows),
            "output_tokens": sum(r["output_tokens"] for r in rows),
            "estimated_cost": round(
                sum(r["estimated_cost"] or 0.0 for r in rows), 6
            ),
        }
    else:
        totals = {"calls": sum(r["calls"] for r in rows)}

    return {
        "group": group,
        "currency": prices.currency,
        "cost_basis": (
            "estimate computed from the configured price table; accurate only "
            "if those prices are current"
        ),
        "rows": rows,
        "totals": totals,
        "models_without_prices": sorted(missing),
    }
