"""LLM token-cost metering and persistence helpers."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from decimal import Decimal

from sentinel.store.repos.costs_repo import CostRecord, insert_cost

TOKEN_PRICES_PER_MILLION: dict[str, tuple[Decimal, Decimal]] = {
    "claude-haiku-4-5-20251001": (Decimal("1.00"), Decimal("5.00")),
    "claude-sonnet-5": (Decimal("3.00"), Decimal("15.00")),
    "gpt-5.4-mini": (Decimal("0.25"), Decimal("2.00")),
    "gpt-5.5": (Decimal("5.00"), Decimal("15.00")),
}

_MILLION = Decimal("1000000")
_CENT_MICRO = Decimal("0.00000001")


def cost_usd(model: str, tokens_in: int, tokens_out: int) -> Decimal:
    """Return the estimated USD cost for input/output token usage."""

    input_price, output_price = TOKEN_PRICES_PER_MILLION.get(
        model,
        (Decimal("0"), Decimal("0")),
    )
    cost = (Decimal(tokens_in) * input_price / _MILLION) + (
        Decimal(tokens_out) * output_price / _MILLION
    )
    return cost.quantize(_CENT_MICRO)


def record_cost(
    conn: sqlite3.Connection,
    *,
    run_id: str | None,
    agent: str,
    model: str,
    tokens_in: int,
    tokens_out: int,
    cost: Decimal | None = None,
    latency_ms: int = 0,
) -> Decimal:
    """Persist a single LLM cost row and return the cost written.

    Free-tier / open-weight backends bill $0, so token counts and ``latency_ms`` are
    recorded regardless of dollar cost to keep per-decision metering meaningful.
    """

    incurred = cost if cost is not None else cost_usd(model, tokens_in, tokens_out)
    insert_cost(
        conn,
        CostRecord(
            ts=datetime.now(UTC),
            run_id=run_id,
            agent=agent,
            model=model,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=incurred,
            latency_ms=latency_ms,
        ),
    )
    return incurred
