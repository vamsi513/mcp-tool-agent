"""Monthly budget per API key, on top of the repository's metering.

Spend is not counted twice. The gateway writes one `usage_llm` row per
upstream call through `metering.record_llm_turn`, exactly as the agent does,
and month-to-date spend is read back out of those rows with
`metering.report`. There is no second counter to drift.

Every figure is an estimate: it is the recorded token counts multiplied by
whatever is in the configured price table, and the shipped table is all zeros.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import metering
from gateway.config import KeyConfig

logger = logging.getLogger("gateway.budgets")


def month_start(now: datetime | None = None) -> str:
    moment = now or datetime.now(UTC)
    return moment.date().replace(day=1).isoformat()


@dataclass(frozen=True)
class BudgetState:
    key_id: str
    limit: float | None
    spent: float
    currency: str

    @property
    def exceeded(self) -> bool:
        return self.limit is not None and self.spent >= self.limit

    @property
    def remaining(self) -> float | None:
        if self.limit is None:
            return None
        return max(0.0, self.limit - self.spent)


def spend_this_month(
    key_id: str,
    prices: metering.PriceTable | None = None,
    db: str | os.PathLike[str] | None = None,
    now: datetime | None = None,
) -> tuple[float, str]:
    """Estimated spend for this key since the first of the month."""
    table = prices or metering.load_prices()
    report = metering.report(
        group="user",
        user=key_id,
        since=month_start(now),
        prices=table,
        db=db,
    )
    spent = report["totals"].get("estimated_cost") or 0.0
    return float(spent), report["currency"]


def check(
    key: KeyConfig,
    prices: metering.PriceTable | None = None,
    db: str | os.PathLike[str] | None = None,
    now: datetime | None = None,
) -> BudgetState:
    """Where this key stands against its monthly budget."""
    if key.monthly_budget_usd is None:
        table = prices or metering.load_prices()
        return BudgetState(key.id, None, 0.0, table.currency)
    spent, currency = spend_this_month(key.id, prices, db, now)
    return BudgetState(key.id, key.monthly_budget_usd, spent, currency)


def record(
    key: KeyConfig,
    model: str,
    input_tokens: int,
    output_tokens: int,
    db: str | os.PathLike[str] | None = None,
) -> None:
    """Write the usage row. The key's id is the metering user.

    The secret never reaches storage or a log line.
    """
    metering.record_llm_turn(
        user=key.id,
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        db=db,
    )


def log_refusal(state: BudgetState, model: str, endpoint: str) -> None:
    """A structured line for a refusal, with no credential in it."""
    logger.warning(
        "budget exceeded",
        extra={
            "key_id": state.key_id,
            "model": model,
            "endpoint": endpoint,
            "estimated_spend": state.spent,
            "limit": state.limit,
            "currency": state.currency,
        },
    )


def summary(state: BudgetState) -> dict[str, Any]:
    return {
        "key_id": state.key_id,
        "limit": state.limit,
        "estimated_spend": round(state.spent, 6),
        "remaining": None if state.remaining is None else round(state.remaining, 6),
        "currency": state.currency,
        "basis": (
            "estimate from the configured price table; accurate only if those "
            "prices are current"
        ),
    }
