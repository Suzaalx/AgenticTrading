# Sentinel live trading

> **Research use only. Not financial advice.** Live mode gives software authority over real money in your own Robinhood account. Losses are yours. Use only official Robinhood rails and start tiny.

## Graduation ladder

| Stage | Scope | Rail | Prerequisites |
|---|---|---|---|
| 0 paper | all assets | PaperBroker | none |
| 1 live crypto | BTC-USD, ETH-USD | official Robinhood Crypto Trading API | at least 30 paper crypto runs, crypto P&L history, `sentinel doctor --live`, live mandate, typed confirmation |
| 2 live equities | mandate stock universe | Robinhood Agentic Trading MCP | at least 60 days paper equity track, agentic account linked and funded, explicit Robinhood preview/approval choice, typed confirmation |
| 3 live options | defined-risk options | Agentic MCP when Robinhood exposes options tools | Stage 2 active at least 30 days, at least 50 paper option positions closed, capability probe, typed confirmation |

Modes can mix by asset class. Paper remains the fallback and default.

## Robinhood setup

### Stage 1: crypto

Create official Robinhood Crypto Trading API credentials and set them via env or `.env`:

```powershell
$env:ROBINHOOD_API_KEY="..."
$env:ROBINHOOD_PRIVATE_KEY="..."
uv run sentinel doctor --live
```

### Stage 2/3: Agentic Trading MCP

Link the Robinhood Agentic Trading account manually in Robinhood’s own Agentic Trading flow. Choose funding limits, trade preview, manual approval, notifications, and disconnect controls in Robinhood. Sentinel uses the MCP account only through official MCP tools.

Set the MCP endpoint/credential variable named by `execution.robinhood_agentic.mcp_endpoint_env` (default `RH_AGENTIC_MCP_URL`).

### Funding cap

Use Robinhood’s funding cap as the hard outer limit. In `mandate.toml`, set `[mandate.live].max_account_allocation_usd` to a value **less than or equal to** that Robinhood cap. Sentinel also applies `max_live_order_notional_usd`, daily loss, order count, quote freshness, and limit-order rules.

## Reconciliation semantics

- In live mode, the broker is the source of truth.
- Sentinel’s SQLite portfolio is a mirror updated by reconciliation.
- A cash, position, option-contract, or order mismatch halts new live risk.
- Sentinel never silently adopts broker state.
- Use `sentinel reconcile --adopt-broker` only after reviewing the diff; the adoption is audited.
- Crash recovery reconciles before new live orders are allowed.

## Incident playbook

1. **Kill switch:** `uv run sentinel kill on --flatten` if immediate de-risking is required. Without `--flatten`, kill blocks new orders and cancels live open orders.
2. **Cancel all:** confirm open live orders are cancelled in Sentinel and Robinhood.
3. **Reconcile:** `uv run sentinel reconcile`; if the broker state is correct, run `uv run sentinel reconcile --adopt-broker`.
4. **Demote:** `uv run sentinel demote crypto`, `equity`, or `options` to turn off live routing instantly.
5. **Post-mortem:** record what happened, broker ids, fills, diffs, root cause, and what mandate/config change prevents recurrence.

## First real ~$10 crypto smoke checklist

1. Keep Stage 0 paper green and run `uv run pytest -q`.
2. Fund only a tiny amount in Robinhood; cap it in Robinhood first.
3. Set `max_account_allocation_usd <= Robinhood funding cap` and `max_live_order_notional_usd` around `$10`.
4. Set Robinhood Crypto API env vars.
5. Run `uv run sentinel doctor --live`; confirm keys are present, kill path writable, and live allocation is tiny.
6. Run `uv run sentinel graduate crypto` and type the required confirmation only if every prerequisite is expected.
7. Place one tiny BTC-USD or ETH-USD run using the live crypto route.
8. Confirm order status in Robinhood, then in Sentinel’s live orders/reconciliation output.
9. Run `uv run sentinel reconcile` and ensure it is clean.
10. Demote or leave the kill switch on until you are ready for another tiny test.
