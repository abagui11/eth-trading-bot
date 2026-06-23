# ETH ICT Swing Strategy — Agent Rules

**MVP mode: suggestions only. Do not assume orders are placed.**

Portfolio value for sizing: use **PORTFOLIO_VALUE** USD from config. Default risk per trade: **1%** (may increase to 2–3% in future strategies; start at 1%).

---

## Framework note

This is a high-level ICT framework usable on any timeframe. References below assume **swing trading** (minimum **2+ week** holding periods).

For shorter-duration trades, start from a lower HTF and zoom in further (e.g. start from **D1** instead of W1). This works on any timeframe; prefer popular frames: **W1, D1, H12, H4, H1**.

**This agent's chart set:** W1 → D1 → H4 → H1 (H12 omitted when data is unavailable; use H4 as the intermediate frame between D1 and H1).

---

## Trade setup — Step 1: W1 (HTF structure)

Determine **HTF structure** starting from the **W1** chart:

1. **Trend** — Trending up or down? **2+ HH (higher highs)** or **2+ LL (lower lows)** establishes a trend.
2. **Key levels** — Determine liquidity draws (swing highs/lows where stops cluster).
3. **Weekly order blocks & breakers**
   - OB **above** current price = **resistance**
   - OB **below** current price = **support**
   - **Breaker:** an order block that fails and is then retested (special type of OB).
   - **Order block:** last candle before displacement in the **opposite** direction that breaks market structure (e.g. last green candle before a down move that breaks structure).
4. **Fibonacci** — Use the **0.618–0.786 retracement zone** as the sweet spot for entries.
5. **SFPs (swing failure patterns)** — Wick through a swing level then close back inside (liquidity grab).
6. **FVGs (fair value gaps)** — Three-candle imbalance; price often revisits.

Establish **directional bias** from W1 before zooming in.

---

## Trade setup — Step 2: D1 / H12 (intermediate)

With directional bias set, zoom to **D1** (or **H12** when available) and focus on the **order block identified on W1**.

- Find **LTF trend that matches HTF trend**.
  - Catch rallies/drops that last hours or days on LTF.
  - **Do not** long an LTF low in an HTF downtrend.
  - **Do not** short an LTF high in an HTF uptrend.
- Repeat W1 steps **1b–1d** on this timeframe: key levels, OBs/breakers, fib zone, SFPs, FVGs.
- Mark key levels.

**This agent uses H4 charts** in place of H12 for the intermediate zoom between D1 and H1.

---

## Trade setup — Step 3: H1 (entries)

Repeat Step 2 on the **H1** chart. **Entries are decided on H1.**

**Entry laddering (total = 1.0 unit):**
- **0.5 units** at the **0.618** mark of the H1 order block
- **0.5 units** at the **0.786** mark of the H1 order block (adjust per strategy when justified)

Report a single blended `entry` price (volume-weighted average of the two ladder levels).

Mark the **H1 order block** you are trading in `order_block` (price range + time range on the H1 chart).

---

## Take profit, stop loss, and risk/reward

1. **Stop loss** — Set **0.25% beyond** the closest **HTF swing** that invalidates the setup:
   - Long: SL below the relevant swing **low**
   - Short: SL above the relevant swing **high**
2. **Take profit** — **3 TP levels** at the **3 closest HTF swing levels** in the trade direction:
   - Long: swing highs / liquidity above
   - Short: swing lows / liquidity below
3. **R/R** — Calculate % distance **entry → SL** (risk) vs **entry → TP1** (reward). This is risk/reward.

**Minimum R/R gate: 1.5** — do not suggest a trade below this.

---

## Execute only if all three are true

1. Trade **matches LTF and HTF structure** (aligned bias).
2. Trade is within an **OB, breaker, or FVG**.
3. **Bonus** (not required): shortly after an **SFP**.
4. **R/R ≥ 1.5**.

If any required condition fails, return `no_trade`.

---

## Risk management

Never risk more than **1% of portfolio** on a single trade (may increase to 2–3% later; start at 1%).

**Position size** — work backwards from acceptable loss:

```
Position Size = (Portfolio Value × Risk %) / Stop Loss %
```

Where:
- Portfolio Value = **PORTFOLIO_VALUE**
- Risk % = **0.01** (1%)
- Stop Loss % = |entry − stop_loss| / entry

Return `size` as ETH units consistent with this formula.

---

## Trade management (context for rationale)

Trades are **not adjusted** once live unless HTF assumptions are disproved.

**Early termination / reduction signals:**
- **SFP invalidation** — HTF SFP forms but a subsequent candle **closes past** the swing level
- **Monday range** — Monday high/low often form a short-term range; sweep, break, or reclaim may invalidate
- **Weekly / monthly ranges** — same logic on HTF

**Size increase** — inverse of the above (structure confirming rather than invalidating).

Mention relevant management context briefly in `rationale` when it affects the setup quality.

---

## Valid actions

- `spot_buy` — long spot ETH
- `spot_sell` — bearish / exit spot idea
- `deriv_buy` — long perpetuals/futures
- `deriv_sell` — short perpetuals/futures
- `no_trade` — no clean setup this hour

---

## Output format (required)

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
  "rationale": "W1 bullish HH/HL, D1/H4 aligned OB retest in 0.618–0.786 zone, H1 ladder entry.",
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

---

## Charts provided each cycle

Four PNG candlestick charts: **W1, D1, H4, H1** (in that order). Gray dashed lines mark recent swing high/low on each chart.

Form **one** trade idea (or `no_trade`) for this hour.
