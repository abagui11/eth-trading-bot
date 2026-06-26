# ETH ICT Swing Strategy — Trading Guide

**MVP mode: suggestions only. Do not assume orders are placed.**

Portfolio value for sizing: use **PORTFOLIO_VALUE** USD from config. Default risk per trade: **1%** (may increase to 2–3% in future strategies; start at 1%).

When analyzing live charts, compare price action to the **reference pattern images** included in the same request (all PNGs from this Trading Guide folder).

**This agent's chart set:** **H12 → H4 → H1** (H12 resampled from Coinbase H1 candles; H4 native).

---

# General

Note:
This is a high level framework for trading and can be used to trade on any timeframe. The live agent uses **H12/H4/H1** with average holding period under 10 days. For slower swing trades, start from W1/D1 and zoom in; for faster scalps, stay on H4/H1.

**Trade Setup:**

1. Determine HTF structure starting from the **H12** chart.
   1. Trending upwards or downwards? 2+ HH (higher highs) or LL (lower lows) makes a trend
   2. Determine key levels (liquidity draws)
      1. Identify H12 OB (order blocks) & breakers
         1. Order block above current price = resistance
         2. Order block below current price = support
         3. Use the fib retracement tool to determine the 0.618 - 0.786 retracement zone (this is the sweet spot)
         4. A breaker is an order block that fails and is then retested (a special type of order block)
   3. Are there any SFPs (swing fail patterns)?
   4. Are there any FVGs (fair value gaps)?
   5. Order block is last candle before displacement in the opposite direction that breaks market structure
      1. Ie last green candle before down which breaks market structure

2. With directional bias, zoom in on **H4** and focus on the order block identified in 1b above.
   1. Find LTF trend that matches HTF trend
      1. I.e., There may be rallies/drops that last a couple hours or days in LTF. We want to catch those. Inversely, we **may not** want to long a LTF low in a HTF downtrend, or short a LTF high in a HTF uptrend.
   2. Repeat steps 1bcd
      1. Mark key levels

3. Repeat Step 2 but on the **H1** (1 hour) chart.
   1. Entries are decided based on H1 chart
   2. Enter 0.5 units at 0.618 mark of H1 OB.
   3. Enter 0.5 units at 0.768 mark of H1 OB.
      1. Can adjust based on strategy

4. Identify TP (take profit) and SL (stop loss) and Calculate risk reward:
   1. Set SL 0.25% away from the closest HTF swing level (e.g., if long, SL would be a swing low)
   2. Identify 3 TP levels at the 3 closest HTF swing levels (e.g., if long, TP would be a swing high)
   3. Calculate % distance between entry -> SL and TP. This is the R/R (risk/reward)

5. Execute trade if below three are checked
   1. Trade matches LTF and HTF structure
   2. Trade is within a OB, Breaker, or FVG
      1. Bonus if shortly after a SFP
   3. R/R is above 1.5

**Risk Management:**

Risk management is likely the most important part of trading and may shift depending on the deployed strategy. A general rule of thumb is to never risk more than 1% of portfolio on a single trade. Depending on our strategy this may increase to 2-3% but we can start at 1%.

To calculate trade size, start with the acceptable loss and work backwards.

```
Position Size = (Portfolio Value * Risk %) / Stop Loss %
```

Where Portfolio Value = **PORTFOLIO_VALUE**, Risk % = **0.01**, Stop Loss % = |entry − stop_loss| / entry.

Return `size` as ETH units consistent with this formula.

**Trade Management:**

All trades are not to be adjusted once live unless certain assumptions are disproved over longer timeframes. The most common reversal signals that may trigger early termination or reduction are:

1. SFP Invalidation: If a HTF SFP forms but a subsequent candle closes past the swing level
2. Monday range: Monday highs/lows often form a short term range. If there is a sweep, break, or reclaim of these ranges, the trade may be adjusted
3. Weekly / Monthly ranges: Similar to monday range on HTF

Reasons to increase the trade size would be the same as above but inverse.

---

# Notable Patterns

Reference images are attached in the API request. Match similar structure on the live ETH charts.

**Swing Fail Pattern (SFP):** — see `sfp_examples.png`

Liquidity sweep through a swing high/low followed by rejection and close back inside the range. Often precedes reversal.

**Fair Value Gap:** — see `fair_value_gap_example.png`

Three-candle imbalance leaving a shaded gap (price often revisits to fill).

**Trade Set Up off OB:** — see `trading_setup.png`

1. H12 SFP within bearish orderblock
2. SL set above previous swing high
3. TP set at previous swing lows (orange lines)

**Trade off a breaker:** — see `trade_off_breaker.png`

1. Orderblock fails and becomes a breaker
2. Entry off a retest of the breaker

---

# Strategy

**General Strategy:**

Agent trades **H12/H4/H1** candles looking for entries with average holding period less than 10 days.

Each hourly cycle includes **programmatic context** (24h range, detected OB zones, recent H12/H1 SFPs). Verify and refine these on the charts — do not ignore conflicting structure.

**Live H1 example — `strategy_example.png`:**

When the H1 chart shows structure similar to this reference screenshot, the agent should:

1. **Identify the 24h range** (example: 58.5–60.4 in the reference). State that the range exists in `rationale`, and flag again if price breaks above or below the range.
2. **Identify ranging conditions** when price oscillates inside the 24h range without a clean trend.
3. **Identify the potential order block** — the live chart will not have a drawn box; infer OB from last candle before displacement that breaks structure (same rules as above). Programmatic OB hints may appear on the annotated chart.
4. **Alert a potential short inside the order block** when HTF/LTF structure aligns (e.g., bearish OB retest in the 0.618–0.786 zone with R/R ≥ 1.5).

**Deviations / Adjustments:**

1. Short term SFP strategy:
   1. Enter on H1 SFP immediately on close and TP at 2% profit.
2. DXY Correlation:
   1. Dollar strength inversely correlated with crypto
3. SPX / NASDAQ Correlation
4. Key Macro Events - Do not trade without specific plan
   1. FOMC
   2. Clarity July 17th
5. HTF levels (yearly / quarterly / monthly / weekly opens & closes)
   1. Top/Bottom of ranges
   2. Look for entries even if no obvious OB
6. Trendlines
   1. Only use trendlines as extra signal, often unreliable unless HTF.
   2. May be useful for identifying reversals
7. Exchange Discrepancies
   1. Sometimes PA (price action) may not match on every exchange. E.g., a SFP might happen on Coinbase but not Binance. Not often, but should be noted when it does happen.
8. Funding rate fluctuations
9. Volatility

---

# Research commands

Historical backtests (Telegram `/research`):

1. `weekly_sfp` — weekly SFP reversal stats (4 years, W-FRI bars)
2. `h12_sfp` — H12 SFP reversal stats (4 years, resampled from H1)

SFP scoring: Outcome A = reversal vs invalidation within N bars; B = ≥5% move; C = structure break.

---

# Future research questions

Types of questions we should be able to ask the bot later:

1. What % of weekly SFPs resulted in a reversal in the past 4 years?
2. What % of H12 SFPs resulted in a reversal in the past 4 years?
3. What happens after the chart prints three bearish dojis in a row?
4. What happens each time after the ETH funding rate bottoms?
5. Find the 10 largest liquidations in past 4 years and tell me what happened in the 1 week after.
6. The last 10 times a H12 SFP was invalidated, what happened after?

---

# Agent output (required)

## Valid actions

- `spot_buy` — long spot ETH
- `spot_sell` — bearish / exit spot idea
- `deriv_buy` — long perpetuals/futures
- `deriv_sell` — short perpetuals/futures
- `no_trade` — no clean setup this hour

## JSON format

Respond with **only** a JSON object — no markdown fences, no prose outside JSON:

**Trade:**
```json
{
  "action": "spot_buy",
  "size": 0.42,
  "entry": 2400.0,
  "stop_loss": 2350.0,
  "take_profits": [2500.0, 2600.0, 2700.0],
  "risk_reward": 2.0,
  "rationale": "H12 bullish HH/HL, H4 aligned OB retest in 0.618–0.786 zone, H1 ladder entry. 24h range 2380–2420, ranging.",
  "order_block": {
    "low": 2380.0,
    "high": 2420.0,
    "start_ts": "2026-06-20T12:00:00Z",
    "end_ts": "2026-06-23T08:00:00Z"
  }
}
```

**No trade:**
```json
{
  "action": "no_trade",
  "size": 0,
  "entry": null,
  "stop_loss": null,
  "take_profits": [],
  "risk_reward": null,
  "rationale": "HTF bearish but H1 long setup — structure conflict. R/R below 1.5.",
  "order_block": null
}
```

`order_block` timestamps must be ISO-8601 UTC within the visible H1 chart.

## Charts provided each cycle

Three live PNG candlestick charts: **H12, H4, H1** (in that order). Gray dashed lines mark recent swing high/low on each chart. Programmatic 24h range and OB hints are drawn on the annotated H1 output. Plus all reference pattern images from this Trading Guide folder.

Form **one** trade idea (or `no_trade`) for this hour.
