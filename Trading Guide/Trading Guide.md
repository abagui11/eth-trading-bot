# ETH ICT Swing Strategy — Trading Guide

**MVP mode: suggestions only. Do not assume orders are placed.**

Portfolio value for sizing: use **live paper equity** (cash + open positions marked to spot). Sizing is **fixed-fraction**: each trade deploys **25% of current equity** as notional (independent of stop distance). The engine recomputes and enforces `size` regardless of the value returned here.

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
         3. Use the fib retracement tool: **entry band 0.25–0.50**, optional **0.718** scale-in (watchdog)
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
   2. **Staged fib entries (watchdog + paper):** deploy **12.5%** of equity at **0.25** fib of the H1 OB, then another **12.5%** at **0.50** fib (total **25%** base exposure).
   3. **Scale-in:** if price reaches **0.718** fib on the same H1 OB, add another **25%** (max **1.25×** the base deploy on that idea).
   4. **H12 vs H1 OB:** Green/pink boxes labeled **H12 OB** on charts are HTF structure only. The `order_block` JSON field must reference a candle on the **H1** chart. If an H1 OB overlaps an H12 OB in price, say so explicitly — never call an H12 box an "H1 OB".
   5. If price is inside an H12 OB but **outside** the H1 OB **0.25–0.50** entry band, default **`no_trade`** (wait for fib retest). Exception: deliberate HTF key-level entry per deviations below.
   6. **Sweep-reversal (watchdog):** when a confirmed H1 SFP sweeps a swing and price **reclaims** inside the H1 OB (but outside the 0.25–0.50 band), the watchdog may enter with stop **below/above the swept level** — not the distant H12 swing.

4. Identify TP (take profit) and SL (stop loss) and Calculate risk reward:
   1. Set SL 0.25% away from the closest HTF swing level (e.g., if long, SL would be a swing low)
   2. Identify 3 TP levels at the 3 closest HTF swing levels (e.g., if long, TP would be a swing high)
   3. Calculate % distance between entry -> SL and TP. This is the R/R (risk/reward)

5. Execute trade if below three are checked
   1. Trade matches LTF and HTF structure
   2. Trade is within a OB, Breaker, or FVG
      1. Bonus if shortly after a SFP
   3. R/R is at least 1.0

**Risk Management:**

Position sizing is **fixed-fraction**: every trade deploys the same fraction of **live paper equity** as notional, regardless of stop distance. Equity = cash + open positions marked to current spot, so winners compound and losers shrink position size. R/R still governs whether a setup is worth taking (first TP must be at least 1.0× the stop distance away).

```
Notional = Live Equity * Deploy %
Position Size (ETH) = Notional / Entry
```

Where Deploy % = **0.25** (25%). Live equity is read from the paper portfolio at validation time.

Return `size` as ETH units consistent with this formula. The engine recomputes and clamps `size` (with min/max ETH guardrails), so an approximate value here is fine.

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
3. **Identify the potential order block** — use **H12 OB/BRKR boxes** on the marked charts when present; they are detected programmatically from H12 structure and cited for **HTF bias only**. For **entries**, use **H1 OBs** from programmatic context (`Detected H1 order blocks`) or infer on H1 using the same displacement rules.
4. **Alert a potential short inside the H1 OB entry band** when HTF/LTF structure aligns (e.g., bearish H1 OB retest in the **0.25–0.50** zone with R/R ≥ 1.0). Being inside an H12 OB alone is not sufficient for entry.

**Deviations / Adjustments:**

1. Short term SFP strategy:
   1. Enter on H1 SFP immediately on close and TP at 2% profit.
2. DXY Correlation:
   1. Dollar strength inversely correlated with crypto
3. SPX / NASDAQ Correlation
4. Key Macro Events - Do not trade without specific plan
   1. FOMC
   2. Clarity July 17th
   3. **Automated macro feed (advisory)** — headlines from RSS/webhook are scored and classified; injected as supplementary context only. Chart structure (H12/H1 OB, SFP, fib) remains primary.
   4. Macro may **confirm** structure (size up conviction) or **conflict** (prefer no_trade, tighten SL, avoid adds) — never flip bias on news alone.
   5. Open positions: prefer tighten stop / partial logic over panic flat unless H1 structure also breaks.
   6. High-severity macro may block new watchdog entries that conflict with macro bias (soft gate).
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

Telegram `/research` shows a **topic catalog** when sent with no args. Reports use a standardized format (headline, metrics, interpretation, sources).

## Market snapshot (text)

| Topic | Command | Data |
|---|---|---|
| Full digest | `/research digest` | macro + funding + volume + dominance + miner |
| Macro | `/research macro` | active headlines, posture, pulses (`macro/`) |
| Funding | `/research funding` | ETH perp funding (Binance ETHUSDT) |
| Volume | `/research volume` | Coinbase spot vs Binance perp 24h |
| Dominance | `/research dominance` | BTC.D + USDT.D (CoinGecko) |
| Miner breakeven | `/research miner` | BTC miner breakeven estimate (hashprice proxy) |

Natural language also works, e.g. "What's ETH funding right now?" or "BTC dominance".

## Pattern studies (chart + stats)

Requires `python backfill.py --all` on the server (`ohlc.db`).

1. `weekly_sfp` — weekly SFP reversal stats (4 years, W-FRI bars)
2. `h12_sfp` — H12 SFP reversal stats (4 years, resampled from H1)

SFP scoring: Outcome A = reversal vs invalidation within N bars; B = ≥5% move; C = structure break.

## Coming soon

- `h12_invalidations` — last 10 H12 SFP invalidations + forward outcomes

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
  "entry": 2408.0,
  "stop_loss": 2350.0,
  "take_profits": [2500.0, 2600.0, 2700.0],
  "risk_reward": 2.0,
  "rationale": "H12 bullish HH/HL for bias. H1 OB 2380-2420 fib 0.25-0.50 entry. Weekly Open confluence.",
  "decision_charts": ["H12", "H4", "H1"],
  "structure_chart": "H12",
  "entry_chart": "H1",
  "order_block": {
    "low": 2380.0,
    "high": 2420.0,
    "start_ts": "2026-06-20T12:00:00Z",
    "end_ts": "2026-06-20T12:00:00Z"
  }
}
```

`order_block` must be an **H1 OB** (timestamps on the H1 chart). Entry must fall on fib **0.25** or **0.50** tranches or inside the **0.25–0.50** band (example entry 2395 inside 2390–2400). Do not copy H12 OB bounds into `order_block`.

**No trade:**
```json
{
  "action": "no_trade",
  "size": 0,
  "entry": null,
  "stop_loss": null,
  "take_profits": [],
  "risk_reward": null,
  "rationale": "HTF bearish; price inside H12 bullish OB but no H1 OB fib entry — wait for retest.",
  "decision_charts": ["H12", "H1"],
  "structure_chart": null,
  "entry_chart": null,
  "order_block": null
}
```

## Charts provided each cycle

Three live **marked** PNG candlestick charts: **H12, H4, H1** (in that order). These are full-width images sent to you for analysis — read overlays directly on the chart.

Plus all reference pattern images from this Trading Guide folder.

---

## Chart legend (marked input charts)

Read overlays on the marked charts before forming bias. Programmatic context text may summarize nearest levels and H12 zones — **verify every claim against the chart image**.

### Key levels (horizontal lines + edge labels)

SpacemanBTC calendar levels from UTC daily Coinbase candles. Only levels near the visible price range are drawn to reduce clutter.

Each label shows **name and price** (e.g. `Weekly Open 1,569.40`). Labels alternate **left and right** chart edges when several levels cluster at similar prices, so names and prices stay readable.

| Color | Labels |
|-------|--------|
| Cyan | Daily Open |
| White | Monday High, Monday Low, Monday Mid |
| Gold | Weekly Open, Prev Week High, Prev Week Low, Prev Week Mid |
| Green | Monthly Open, Prev Month High, Prev Month Low, Prev Month Mid |
| Red | Quarterly Open, Prev Quarter Mid, Yearly Open, Current Year Mid |

Light-colored labels use dark text on a tinted badge. When two levels share a price, the label merges both names (e.g. `Weekly Open / Prev Week Mid`).

### H12 order blocks & breakers (shaded rectangles)

Structure is **detected on H12** closed candles, then **projected** onto H12, H4, and H1. The same price zone appears on all three charts; horizontal width maps to the nearest bars on each timeframe.

| Visual | Meaning |
|--------|---------|
| Green box, green border, label **H12 OB** | Bullish order block — last **bearish** H12 candle before a bullish market structure break (close above prior swing high) |
| Pink box, red border, label **H12 OB** | Bearish order block — last **bullish** H12 candle before a bearish MSB (close below prior swing low) |
| Green box, label **H12 BRKR** | Bullish breaker — a **mitigated bearish OB** reclassified after a later bullish MSB |
| Pink/red box, label **H12 BRKR** | Bearish breaker — a **mitigated bullish OB** reclassified after a later bearish MSB |
| Faint line inside the box | Zone midpoint |
| Box stops before the right edge | Zone was **mitigated** (close traded through the block) |
| Box extends to the right edge | Zone is still **active** |

MSB uses **close only** — wick-only breaks through a swing level do not count.

Use H12 OB/BRKR boxes for HTF bias. For LTF entries, use **H1 OBs** (labeled **H1 OB** on marked H1 charts when detected). LTF blocks may not overlap an H12 zone — if they do, state the overlap in rationale.

### H1 order blocks (entries)

| Visual | Meaning |
|--------|---------|
| Green/pink rectangle, label **H1 OB** | H1 order block detected programmatically — use for `order_block` JSON and fib entries |
| No H1 OB label | No programmatic H1 OB in lookback — infer carefully or `no_trade` |

Entry fib band (bullish): `low + span×0.25` to `low + span×0.50`. Scale-in at `0.718`. Programmatic context lists exact levels.

### Other reference lines

- **Gray dashed lines**: recent swing high and swing low on that chart's timeframe (20-bar lookback). Reference only — not key levels.
- **Purple dotted lines** (output/entry charts only): 24h high and 24h low.

---

## Output proof charts (Telegram only — not re-sent to you)

Up to two full-width charts per cycle when a trade is taken:

1. **Structure chart** (`structure_chart` TF) — same overlays as marked charts (key levels + H12 OB/BRKR + swings). No rationale text on the image.
2. **Entry chart** (`entry_chart` TF) — same overlays plus trade markup:
   - **Gold box** + label `Fib 0.25–0.50`: entry band inside your chosen `order_block`
   - **Green dashed** line (left label): Entry
   - **Red solid** line (left label): Stop loss
   - **Blue dotted** lines (left labels): TP1, TP2, TP3

Rationale and action details belong in the JSON `rationale` field only — subscribers receive them as a Telegram text message below the chart photos.

Cite visible levels and H12 OB/BRKR zones in `rationale`.

### Rationale structure

Write `rationale` as **short paragraphs** separated by a blank line (`\n\n`). Do not write one long wall of text.

1. **HTF structure** — trend, swings, and key higher-timeframe levels
2. **Supply/demand** — active **H12** OB/BRKR zones (mitigated vs unmitigated); cite for bias only
3. **LTF context** — **H1 OB** (with fib zone), 24h range, setup state, pending or confirmed SFPs
4. **Decision** — why this trade or `no_trade`, and what would change the call

### Rationale anti-patterns (do not do this)

**Bad:** Citing multiple invented `H1 OB 1,569–1,572` ranges inside a wide H12 bearish zone when no matching H1 OB appears under *Detected H1 order blocks* in programmatic context. Sub-candles inside an H12 box are not separate H1 OBs unless detected programmatically.

**Bad:** Citing `H1 SFP` when Recent H1 SFPs is empty or only Live-invalidated SFPs exist in programmatic context.

**Good:** Cite the H12 bearish OB/BRKR for HTF bias; state clearly when no valid H1 SFP is in the window; wait for an H1 fib retest only on bounds listed in programmatic context.

Form **one** trade idea (or `no_trade`) for this hour.
