# Sentinel options

Sentinel options are deterministic at the edges: agents choose from pre-built candidates, while Python computes prices, Greeks, IV, max loss, collateral, and lifecycle exits.

## Math notes

- **BSM** prices European calls/puts with continuous dividend yield `q`; parity is checked as `C - P = S e^-qT - K e^-rT`.
- **CRR binomial** prices American exercise cases, especially puts where early exercise can matter.
- **Implied volatility** uses bracketed Brent/bisection-style inversion over BSM after checking no-arbitrage bounds.
- **Realized volatility** defaults to Yang-Zhang because it includes overnight, open-close, and Rogers-Satchell intraday components.
- **Units:** vega and rho are per `1.00` volatility/rate; divide by `100` for one vol/rate point. Theta is per year; divide by `365` for calendar-day theta. Contract-level Greeks are multiplied by contracts and the 100 multiplier.

Golden reference: `S=100, K=100, T=1, r=0.05, q=0, sigma=0.20` gives call `10.4506`, put `5.5735`, gamma `0.018762`, vega `37.524`, call theta `-6.414`/year, and call rho `53.232`.

## Mandate knobs

`[mandate.options]` controls:

- `enabled`
- `underlying_universe`
- `defined_risk_only` (validator rejects `false`)
- `max_loss_per_position_usd`
- `max_total_options_max_loss_pct_equity`
- `max_contracts_per_order`
- `min_open_interest`
- `max_rel_spread_pct`
- `min_dte`, `max_dte`
- `max_net_portfolio_delta_abs`
- `max_net_portfolio_vega_abs`
- `max_option_orders_per_day`

`[options]` controls modeling defaults such as commission, slippage fraction, `force_close_dte`, and fallback risk-free rate.

## Strategy menu and max loss

All max-loss formulas are per structure using the 100 contract multiplier.

| id | Structure | Max loss |
|---|---|---|
| `long_call` | buy 1 call | premium paid |
| `long_put` | buy 1 put | premium paid |
| `covered_call` | own 100 shares + sell 1 call | stock downside to zero minus premium |
| `cash_secured_put` | sell 1 put + escrow `strike * 100` cash | `strike * 100 - premium` |
| `bull_call_spread` | buy call K1, sell call K2>K1 | net debit |
| `bear_put_spread` | buy put K2, sell put K1<K2 | net debit |
| `bull_put_spread` | sell put K2, buy put K1<K2 | `(K2-K1) * 100 - net credit` |
| `bear_call_spread` | sell call K1, buy call K2>K1 | `(K2-K1) * 100 - net credit` |
| `long_straddle` | buy call + put same strike | total premium paid |
| `long_strangle` | buy OTM call + OTM put | total premium paid |

`iron_condor` and `calendar_spread` are enum stubs only.

## Candidate builder rules

Candidates are built, filtered, and labeled before agent selection:

1. Select an expiry in `min_dte..max_dte`, preferring monthly expiries.
2. Keep liquid quotes only: open interest at least `min_open_interest`, positive bid, valid bid/ask, and relative spread under `max_rel_spread_pct`.
3. Choose strategy plans from direction and IV context: bullish, bearish, or neutral; high IV favors credit/short-premium defined-risk structures.
4. Choose legs by delta targets and strike ordering.
5. Compute net premium, max loss, max gain, breakevens, net Greeks, liquidity score, and a BSM probability proxy.
6. Return only finite-risk candidates that pass the mandate gate.

## Settlement and lifecycle

- Positions can close on premium stop, premium take-profit, time horizon, or DTE rule.
- Sentinel force-closes options when `DTE <= force_close_dte` (default `1`) to avoid expiry/assignment surprises.
- If an option reaches expiry in paper/backtest mode, deterministic settlement uses intrinsic value by leg.
- Long ITM options settle to intrinsic value; OTM longs expire worthless.
- Short ITM covered calls may assign against held shares; short ITM cash-secured puts may assign shares for strike cash; OTM shorts expire with premium retained.
- Synthetic backtest option marks are `synthetic_bsm` and are **synthetic pricing — not indicative of live fills**.
