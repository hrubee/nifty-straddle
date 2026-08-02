# The Strategy — devised from the 2026-07 deep research

Built on `CRYPTO_EDGE_RESEARCH_2026.md` (5-agent web survey) + this repo's own faithful backtests. The
research killed every "single high-return edge" and left exactly three real, retail-reachable families. The
strategy is therefore a **diversified multi-sleeve book**, not one signal — because the only honest path to
better *risk-adjusted* return is combining low-correlated edges, and the only honest path to *higher* return
is fractional-Kelly leverage on that combined book (never on a single sleeve).

## ⚠️ Drawdown / ruin first (standing rule)

- **Target is NOT 15%/month.** 435%/yr beats the closed Medallion fund ~6.6× and requires leverage that
  Kelly math turns into guaranteed ruin (4× over 1,000 bets: $100 → <$2 via volatility drag). Any product
  promising it is short-gamma bagholding, martingale, or a Ponzi.
- **Honest target for this book: ~15–30%/year at <12% max drawdown**, fractional-Kelly (½ or less) sized.
  That is already better than most crypto hedge funds (which trailed BTC in 2023–24).
- Every sleeve is sized by its **worst historical month + longest losing streak**, not its Sharpe.

## The three sleeves (all research-validated, low mutual correlation ≈ 0.00)

### Sleeve A — CARRY (market-neutral core, ~50–60% of risk budget)
- **Trade:** delta-neutral funding/basis carry — long spot, short perp, notional-matched, on the *same*
  venue to avoid cross-exchange settlement risk. Harvest funding while trailing funding is positive; go flat
  (or rotate to the next coin) when it turns negative.
- **Coin selection:** top-10 liquid perps by funding-yield rank; equal-weight 4–6 at a time (dispersion is
  huge per the study — venue/coin selection dominates).
- **Honest expectation:** ~5–15% net APY unlevered; run at a *modest* 2× with hard liquidation buffers → ~10–25%.
- **The real risk (model it, don't ignore it):** funding inversion + short-leg liquidation + venue de-peg,
  all of which co-occurred Oct 10–11 2025. Hard rules: isolated margin, ≥40% liquidation buffer on the perp
  leg, cap per-venue exposure, and a kill-switch if funding goes deeply negative across the basket.

### Sleeve B — TREND (directional convexity, ~25–30% of risk budget)
- **Trade:** the existing `momentum-chase` — long-only breakout + ATR chandelier trail on liquid majors +
  top-60 rotation. The one directional family that passed this repo's clean-split gauntlet AND the literature.
- **Honest expectation:** Sharpe ~0.5–1.0, but 40–70% peak-to-trough on the sleeve — kept to <30% of risk so
  the book-level DD stays bounded. It's insurance you get paid for: bleeds small in chop, pays big in trends,
  and is *uncorrelated to carry* (carry dies in violent moves; trend loves them → natural hedge).

### Sleeve C — SHORT-VOL (premium harvest, ~15–20% of risk budget, optional/canary)
- **Trade:** IV-rank-gated, **defined-risk** iron condor on BTC/ETH (Deribit USDC or Delta), sell only when
  IV-rank ≥ 0.5, TP at 50% credit, hard stop, **plus a small standing long-tail hedge** (cheap far-OTM puts)
  to cap the March-2020-style left tail.
- **Honest expectation:** mid-single-digit %/yr net after real fees — modest, but *all-regime* and
  uncorrelated to A and B. Run it yourself (never via a DOV — the Friday auction gives away 5–15% of premium).
- **Never:** naked strangles, cheap-OTM buying (30–50% friction), or vaults.

## Why a portfolio, not a single bet
- Research verdict: no single crypto edge clears **~2%/month** net. But three edges at Sharpe ~0.5–1.0 each,
  correlation ≈ 0, combine toward a **book Sharpe meaningfully >1** (uncorrelated Sharpes add in quadrature).
- **Quality-weight (Sharpe-proportional), not equal-weight** — equal-weight dilutes the best sleeve.
- Then, and only then, apply modest book-level leverage sized at fractional Kelly to lift *return* — with the
  drawdown consequence stated up front, never hidden.

## What was explicitly rejected (and why) — so we don't re-explore dead ends
- Cross-exchange / triangular / latency arb — dead for retail (ms latency race).
- MEV, market-making, order-flow, liquidation-hunting — specialist-gated or sub-fee at retail latency.
- ML/DL price forecasting — most overfit category; low RMSE ≠ P&L; net edge ~0.
- Cross-sectional momentum (raw) — alpha lives in un-tradeable microcaps; +69% IS → −2.35% OOS.
- Grid / martingale / DCA / copy-trade / "guaranteed %" — short-gamma bagholding or outright fraud.
- DOVs, naive OTM option buying — structural loss to fees/auction.

## Build order (concrete next steps)
1. **Carry paper/canary runner** — Binance (or Delta) spot + perp, delta-neutral, the safest sleeve and the
   one not yet live. Wire funding-rank selection + flat-on-negative + isolated-margin liq buffer.
2. Keep `momentum-chase-delta.service` running as Sleeve B (already live).
3. Add the IV-rank defined-risk condor as a small canary (Sleeve C) with the standing tail hedge.
4. Combine into one risk-budgeted book; size at ½-Kelly; monitor book-level DD, not per-sleeve.

Backtests: `crypto_funding_carry.py`, `crypto_master_strategy.py`, `crypto_options_sell_research.py`,
`crypto_residual_mr.py`. Research base: `CRYPTO_EDGE_RESEARCH_2026.md`.
