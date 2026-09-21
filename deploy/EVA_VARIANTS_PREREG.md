# Eva HQ variants — pre-registration

Registered **2026-09-14**, before any variant position existed. Experiment
epoch `EVA_EXPERIMENT_EPOCH = 2026-09-15T15:10:00Z`; anything closed earlier is
outside this analysis.

The epoch was re-based once, on 2026-09-15, and the hypotheses and thresholds
below were **not** touched when it was. The first day's positions were resolved
against M5 bars that predated their own entry — Coinbase honours `limit` ahead
of `start`, so the walk received ~29h of history for every request — which made
those closes artifacts of a bug rather than measurements. All 6 were deleted
and the epoch pinned to the minute the fix shipped. Control's one in-epoch
close was discarded on the same grounds. Re-basing an epoch is the kind of
move that quietly launders a bad start into a fresh one, so the rule applied
was: the reset is legitimate only because it discards data from *every* book
including control, and because it happened before any promotion decision was
in play.

This document exists so the promotion decision is made **now**, against stated
thresholds, rather than later against whichever book happens to be ahead. The
Eva Lab tab deliberately labels its front-runner a "candidate only" for the
same reason. Evidence for the hypotheses is in `EVA_VARIANTS_PLAN.md` §1.

**Amendment 2026-09-16 — a fourth arm registered: `eva_geom` (vol-conditioned
brackets on control's entries). All existing arms and thresholds unchanged.**
For the record: the rule briefly shipped onto control/live/swing-llm directly
(16:56Z–17:5xZ) and was rolled back the same hour — every cycle in the window
said `no_trade`, so zero positions were booked under it, the epoch did not
move, and no book's sample was touched. The lesson stands with the rollback:
the rule overrides the LLM's structural ICT levels, which is a hypothesis this
experiment must test, not a fact it may assume.

- **Arm:** `eva_geom` — control's exact entries; stop = max(LLM stop,
  7×ATR24) capped at 4.5%; TP rung k = min(LLM rung, k×8×ATR24); 48h hard
  close (the replay's measurement horizon). Skips (never opens a raw twin)
  when ATR is unavailable.
- **Registered evidence:** 52-entry replay (`trade_ideas/analysis/
  _q0916_dynamic_geometry.py`): +0.251 mean R vs control's +0.128, placebo
  delta +0.33; 11 tail winners truncated, none flipped negative. Parameters
  are structural anchors on a broad plateau, not swept optima.
- **Hypothesis H4:** `eva_geom` beats control in mean R at equal $ risk, with
  a lower stopped-then-paid rate, *without* the LLM's structural levels
  proving load-bearing (i.e. the truncation cost staying below the noise-stop
  savings, as the replay claims).
- **Falsified if:** after ≥30 resolved geom positions, mean R does not exceed
  control's over the same entries, or the book's stopped-then-paid rate is
  not lower than control's.
- **Promotion bar:** same as §4 — no part of this rule touches control or
  live until the bar clears on the forward paper book.

---

**Amendment 2026-09-21 — live capital extended to `eva_swing_llm` and
`eva_day`, ahead of the §4 bar. An operator risk decision, recorded as such.**

- **What changed:** both books now mirror their qualifying opens onto the live
  sleeve (`live_trades` sources `hq_swing` / `hq_day`), at fixed
  `LIVE_VARIANT_RISK_USD` per clip, behind per-book kill-switches
  (`EVA_SWING_LLM_LIVE_ENABLED`, `EVA_DAY_LIVE_ENABLED`). `eva_day` mirrors
  its `vision_rebracket` source only — `m1_trigger` keeps its §2 negative
  prior and stays paper-only inside the same book.
- **What did NOT change:** `EVA_LIVE_VARIANT` remains `control`; the §4 bar
  still governs promotion; the epoch is unmoved; the paper books remain the
  measurement instrument and are written before, and independently of, the
  mirror. **Live results are excluded from the §4 statistics.**
- **Why, honestly:** the books led at n=9 apiece (+3.05R / +3.28R), which is
  inside one standard error of zero and proves nothing — recorded here as
  context, not justification. The decision is a bet on better P&L while the
  experiment runs, with bounded risk: fixed clips, a family stacking cap
  (`LIVE_VARIANT_STACK_RISK_CAP_USD` — day re-brackets control's own entries,
  so one idea can be live twice), a pooled family daily-loss halt, and a
  chase guard so a mirror that cannot fill near the paper entry does not fill
  at all.
- **Exit, stated in advance:** if a mirrored book fails §4 at n=60, its
  mirror shuts off and the live P&L it accumulated is reported in the
  changelog as the cost of running ahead of the evidence — not netted away,
  not re-based.
- **Contract priority (same day, later):** every live sleeve shares one CDE
  contract per product, so opposite directions cannot both hold it — the
  venue rejects the second with `PREVIEW_ORDER_SIZE_EXCEEDS_BRACKETED_POSITION`
  (control BTC short into the swing mirror at 18:30Z, same day the mirrors
  shipped). Operator order, recorded here: swing > day > control > mill
  (`execute._SOURCE_PRIORITY`). An entry flattens what it outranks and
  refuses as a logged skip when it does not. **The cost lands on control:**
  its live entries can be blocked and its open positions flattened mid-trade
  by either mirror (booked `hq_priority`). The LIVE row on Eva Trades is
  therefore no longer a clean read of what control would have done alone.
  The paper books are untouched; live results stay excluded from §4; this
  does not change the promotion bar.

## 1. Books

| book | mode | writes | new LLM cost |
|---|---|---|---|
| `control` | **LIVE (real money)** | `paper.py`, untouched by variant code | — |
| `eva_swing_mech` | paper | `variant_positions` | zero |
| `eva_swing_llm` | paper | `variant_positions` | ~12 calls/day (own 2h cycle) |
| `eva_day` | paper | `variant_positions` | zero |

`EVA_LIVE_VARIANT` names the only book permitted to touch real money. It is
`control` and stays `control` until §4 clears.

## 2. Hypotheses, each with the metric that can actually settle it

Stated in the direction we expect, with the primary metric fixed in advance.
Mean-R comparisons are listed as **secondary throughout** because at the
sample sizes this book accumulates they cannot resolve anything within the
experiment's horizon (see §5).

### H1 — Eva's stop is too tight (the swing claim)

> Trades that are directionally right are being closed by the stop before the
> thesis resolves.

- **Primary (mechanism):** *stopped-then-paid rate* — of positions closed at
  the stop, the fraction whose MFE later exceeded 0R. In control this was
  **7 of 10** pre-experiment. H1 predicts `eva_swing_mech` shows a
  **materially lower** rate on the same entries.
  - Chosen because both books take the *same entries*, so this is a paired
    comparison and needs no cross-book equivalence assumption. It also
    converges far faster than mean R.
- **Secondary:** mean R, total R, median hold, target-completion depth.
- **Falsified if:** `eva_swing_mech`'s stopped-then-paid rate is not lower, or
  its mean R is below control's while the stopped rate is unchanged — that
  would mean the wider stop only bought bigger losses.

### H2 — Eva's HTF context is underused (the swing mandate)

> Given H12/D1 and told to place stops at structural invalidation, the same
> model writes plans that survive longer.

- **Primary:** stopped-then-paid rate, as H1.
- **Secondary:** mean R vs `eva_swing_mech`. This is the comparison that says
  whether the LLM adds anything *over* mechanical re-bracketing. If
  `eva_swing_llm` does not beat `eva_swing_mech`, the mandate arm is not worth
  its tokens and gets retired regardless of how it looks against control.
- **Falsified if:** its plans are rejected by `validate_swing_plan` at a high
  rate (the mandate is not producing coherent geometry), or it fails to beat
  the mechanical arm.

### H3 — There is tradeable alpha inside 4 hours (the day claim)

> Half to two-thirds of Eva's per-trade R is reachable in 4h with ~6x faster
> capital turnover.

Scored **separately by `entry_source`**, and this split is not optional:

- `vision_rebracket` — day geometry on the LLM's entries. This is the path
  §1.3 supports (+0.262R at TP1.0/SL1.0, 62% win, n=21).
- `m1_trigger` — deterministic entries. **Unvalidated. No backtest exists.**

- **Primary:** mean R per source, against control on the same window.
- **Secondary:** fill-to-trigger ratio, median hold, skip reasons.
- **Falsified if:** either source's mean R is ≤ 0 over 60+ closed positions.

**The `m1_trigger` arm starts from a negative prior and is treated that way.**
The watchdog precedent: 1,416 deterministic shadow fires came in at −0.077R
per trigger with no surviving family, and its one clean result was negative
(`trade_ideas/analysis/WATCHDOG_FINDINGS.md`). Measured on live M1, structure
breaks fire ~1.3x/h/product and FVGs ~4x/h/product, so the conjunction plus
cooldown is what keeps this tradeable at all. A positive result here needs to
clear the placebo, not just zero.

## 3. Fixed before any data

Changing any of these invalidates the experiment and requires a new epoch:

1. **Equal dollar risk.** Every book risks `VARIANT_RISK_USD = $10` per
   position, so a wider stop buys a smaller position. Books are compared in
   **R**; dollar P&L is not comparable because control sizes off a portfolio
   fraction.
2. **Exits resolve on the M5 path**, stop-first on an ambiguous bar, for every
   book. Never on poll-time spot.
3. **Ladder legs collapse by position** before any statistic is computed.
   Un-collapsed, a 3-target winner counts three times.
4. **One open position per book per product per side.** Excess triggers are
   recorded in `variant_skips`, so the denominator stays honest and the fire
   rate is never mistaken for the position rate.
5. **Parameters are frozen at their registered values** (`bot_config.py`):
   `EVA_DAY_TP_R = 1.0`, `EVA_DAY_MAX_HOLD_H = 4.0`,
   `EVA_SWING_TP_RUNGS = (1.5, 3.0, 5.0)`, `EVA_SWING_MAX_HOLD_H = 168`,
   stop clamps as configured. No mid-flight tuning. Any sweep is a *new*
   registration, and a parameter whose R is non-monotonic across the sweep is
   fitting noise and must not ship as a tuned value.

## 4. Promotion bar — all five, or `control` stays live

A leading book on the dashboard is **not** a promotion. To take
`EVA_LIVE_VARIANT`, a variant must:

1. Have **≥ 60 closed positions** in-epoch. Below this nothing is measurable.
2. Beat control on **mean R** with a day-clustered bootstrap 95% CI that
   **excludes zero**. Day-clustered because same-day positions share the tape
   and are not independent observations.
3. Beat its own **random-entry placebo** with identical geometry — otherwise
   the result is the geometry, not the signal. This is the check the watchdog
   families failed.
4. Show its **primary mechanism metric** moved in the predicted direction. A
   mean-R win with no mechanism is an unexplained result, and unexplained
   results at this sample size are usually noise.
5. Survive a **structural review**: bounded risk, honest accounting, no
   dependence on a tuned threshold.

Promotion is the two-line change `EVA_LIVE_VARIANT = "<book>"` plus routing
that book's suggestions into the live path. Nothing about control's code
changes, so the rollback is the same two lines.

## 5. Stated up front: this book is small

Eva closes on the order of tens of positions per month. At n=60 per book and
the observed per-trade R spread, the minimum detectable difference in mean R is
roughly **0.4–0.5R** at 80% power — larger than most of the effects in §1.
Three consequences, accepted in advance:

- **Most single-parameter results here will not reach significance**, and will
  be reported as not significant rather than as trends.
- This is why every primary metric is a **mechanism** metric, not mean R.
  "Did the wider stop stop getting hit?" converges in a handful of trades;
  "did it make more money?" does not.
- `eva_swing_mech` is the strongest arm by design: it shares control's entries,
  so it is a paired test and does not spend power on entry variance.

Prefer changes that are **structurally** correct over changes that backtest
well. Report per-trade winners *and* losers — a net-positive change that flips
two winners into losses needs that said out loud.

## 6. Review

First review at **30 days or 60 closed positions per book**, whichever is
later. Interim dashboard readings are for monitoring that the books are
recording correctly, not for deciding anything.
