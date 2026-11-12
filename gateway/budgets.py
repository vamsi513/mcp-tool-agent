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
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from types import TracebackType
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


class Admission:
    """One held check-and-charge window, or nothing at all.

    Releasing more than once is harmless; never releasing would hold the
    key's lock forever, so every caller releases in a `finally` or by using
    this as a context manager.
    """

    __slots__ = ("_lock", "_held")

    def __init__(self, lock: threading.Lock | None) -> None:
        self._lock = lock
        self._held = False

    @property
    def held(self) -> bool:
        return self._held

    def acquire(self) -> None:
        if self._lock is not None and not self._held:
            self._lock.acquire()
            self._held = True

    def release(self) -> None:
        if self._lock is not None and self._held:
            self._held = False
            self._lock.release()

    def __enter__(self) -> Admission:
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release()


class AdmissionGate:
    """Keeps a key's budget check and its charge from coming apart.

    `check` reads month-to-date spend back out of the usage rows and
    `record` writes the row for a call, with the upstream call in between.
    Nothing tied the two together, so requests arriving at once all read the
    same spend, all saw room and all went through: in a measured run a key
    whose budget allowed one call took 24 of 30 concurrent requests and
    recorded 24 times its limit.

    A call's cost is only known after it returns, so there is no amount to
    reserve up front and no way to admit several at once and still bound the
    total. What is left is to let one request at a time hold the window
    between the check and the charge for a given key, which caps the
    overshoot at the single call that was in flight when the limit was
    crossed.

    The cost is paid only where it was asked for: keys wait on themselves
    and never on each other, and a key with no budget takes no lock at all,
    so unlimited keys keep running fully in parallel.
    """

    def __init__(self) -> None:
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    def _lock_for(self, key_id: str) -> threading.Lock:
        with self._guard:
            lock = self._locks.get(key_id)
            if lock is None:
                lock = threading.Lock()
                self._locks[key_id] = lock
            return lock

    def admit(self, key: KeyConfig) -> Admission:
        """A window for this key: a real one if it has a budget to enforce."""
        if key.monthly_budget_usd is None:
            return Admission(None)
        return Admission(self._lock_for(key.id))


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
