# Eva HQ variants — day / swing experiment plan

Drafted 2026-09-14. Status: **implemented and deployed 2026-09-14** — all
three variants paper-only, `control` retains the live sleeve. Promotion is
governed by `EVA_VARIANTS_PREREG.md`, registered before the first variant
position existed.

Two things changed during the build, both because reading the code contradicted
the plan:

1. **§2.3's schema approach was dropped.** `paper_state` is
   `CHECK (id = 1)` — a single-row table with one cash balance — and
   `paper._fetch_open_positions` selects every open row unscoped, feeding
   netting, the dashboard, Telegram and the live-execution checks. A `variant`
   column would have required re-scoping all of that inside the module that
   runs the control book. The variants instead live in their own tables
   (`eva_variants.py`); `paper.py` is unmodified.
2. **`eva_swing_llm` got its own vision call** at 2h cadence rather than
   sharing the 30-min cycle. A single Claude call shows every image to the
   model, so H12/D1 could not be given to the swing mandate while being hidden
   from control — sharing the call would have contaminated the control book.
   ~12 calls/day instead of 48; control is byte-identical.

Evidence below comes
from the recorded book through 2026-09-12 (21 closed paper, 7 closed live),
re-walked on Coinbase M5 by `trade_ideas/analysis/eva_variants_study.py`. The
barrier engine reproduces 12 of 14 recorded ladder outcomes exactly, so real
and counterfactual legs are measured identically.

---

## 1. What the recorded book says

### 1.1 Holding time — the claim is right, and it is the bracket, not indecision

| | median hold | IQR | winners | losers |
|---|---|---|---|---|
| Paper (n=21) | **24.0h** | 9.5–34.0h | 25.1h | 7.0h |
| Live (n=7) | 15.1h | 3.4–29.0h | | |

The hold time is a direct consequence of the exit geometry. Median TP1 sits
1.44R away (1.47% of price), TP3 at 3.48R (3.57%), and ETH/BTC simply take a
median **16.5h to travel to TP1** and **55.6h to TP3**. Eva holds for a day
because she is paid for holding for a day; there is no faster version of *these
brackets*.

### 1.2 The swing claim — validated directionally at n=21

Two findings, both extending the 2026-09-02 stop study (`analysis/HANDOFF.md`)
to the fuller book:

- **10 of 21** trades traded through their stop level before touching TP1.
  **7 of those 10 later reached TP1 anyway** — three by an overshoot of less
  than 0.05R. Eva keeps being stopped out of directionally correct trades.
- Of the 18 trades that eventually touched TP1, median heat first taken was
  −0.48R and **7 of 18 dipped below −1.00R** before paying. The stop sits
  inside the range her own winners routinely travel.

The wider-stop × further-target sweep (constant dollar risk, 7-day horizon,
random-entry placebo with identical geometry as control):

| stop × | targets × | real mean R | placebo | edge |
|---|---|---|---|---|
| 1.00 | 1.00 (current) | +0.422 | +0.076 | +0.345 |
| 1.25 | 1.50 | +0.733 | +0.072 | +0.661 |
| **1.50** | **1.50** | **+0.789** | +0.083 | **+0.706** |
| 1.50 | 2.00 | +0.893 | +0.062 | +0.831 |
| 2.00 | 2.00 | +0.678 | −0.002 | +0.680 |

Real R rises with both dimensions while the placebo stays flat — the
improvement is not the mechanical win-rate effect of wider stops. **Caveats
that must survive into any implementation:** n=21, nothing is statistically
significant; the stop dimension peaks at 1.5× and declines at 2× (non-monotone
→ do not ship a tuned multiplier); the pre-existing guidance stands — derive
the stop from structure (beyond the swing/OB that invalidates the thesis, plus
a volatility buffer), not from a hardcoded 1.5×.

### 1.3 The day claim — real short-horizon alpha exists on Eva's entries

Within 4 hours of entry, using current-stop R units:

- 76% of entries reached +0.25R, **67% reached +0.50R**, 43% reached +1.00R.
- Time-boxed brackets on the same entries (close at market at 4h if unresolved):

| bracket | mean R | win rate |
|---|---|---|
| TP +0.50 / SL −0.50 | +0.224 | 67% |
| TP +0.75 / SL −1.00 | +0.194 | 62% |
| TP +1.00 / SL −1.00 | +0.262 | 62% |

So roughly **half to two-thirds of Eva's per-trade R is available inside 4h**,
with capital freed ~6× faster. Not placebo-controlled, n=21, and — critically —
these are the LLM's entries. The day variant as specced will enter on
*deterministic M1/M5 triggers*, which this book cannot backtest.

**The watchdog precedent is the caution here.** 1,416 deterministic shadow
fires were measured on 2026-09-10: −0.077R per trigger, no family cleared the
pre-registered bar. That study used Eva-sized stops (avg 6.85% of price) and no
time exit, so it did not test day-trade geometry — but it is prior evidence
that raw deterministic ICT fires are not free money. The day variant is a
hypothesis to be paper-tested, not a validated edge.

---

## 2. Design: four books, one live  *(decided 2026-09-14)*

| | control | eva_swing_mech | eva_swing_llm | eva_day |
|---|---|---|---|---|
| What | Current bot, untouched | Control's entries, swing geometry | Swing mandate | Fast ICT scalps |
| Entries | LLM vision, 30-min cycle | **Mirrors every control entry** | LLM vision, 30-min cycle, sees H12/D1 | Vision suggestions re-bracketed **and** deterministic M1/M5 triggers, tagged by source |
| Stop | LLM plan (~1% of price) | Beyond the H4 structure that invalidates the thesis + vol buffer (code-derived, no LLM) | Beyond H4/H12 structure per the prompt | M1/M5 structure, ≈1R tight |
| Targets | 3-rung ladder, TP1 ≈1.4R | Same ladder shape, price distances ≈1.5–2× | LLM plan at H12/D1 objectives | Single TP +0.75R to +1.0R (sweep in paper) |
| Time exit | none | 7-day max hold | 7-day max hold | **4h hard close** |
| Mode | **LIVE** + paper | paper only | paper only | paper only |
| New LLM cost | — | **zero** | +4 images per existing call | **zero** (deterministic between cycles) |

`eva_swing_mech` is the cleanest test of §1.2 — same signal, only the exit
geometry changes, so any divergence from control is attributable to geometry
alone. `eva_swing_llm` tests the further question of whether Eva plans
differently when shown H12/D1. `eva_day` tags every position with its entry
source (`vision_rebracket` | `m1_trigger`) so the validated and unvalidated
entry paths are scored separately.

**Trigger budget (decided):** cooldown-style — at most one open eva_day
position per product per side; a new trigger on an occupied slot is recorded
as a skip (with reason) rather than a position, mirroring the watchdog's
dedupe.

Shared rules:

- All three size to the same $1,000 nominal account and record in R against the
  opening stop, so books are directly comparable.
- All resolve exits on the M5 path (the honest engine `paper.py` already uses),
  never on poll-time spot.
- `EVA_LIVE_VARIANT=control` (env) marks which book trades real money —
  exactly one, mirroring `KALSHI_LIVE_BOTS`. `EVA_EXPERIMENT_EPOCH` is the
  common start line.
- A pre-registration (same format as `analysis/PREREGISTRATION.md`) freezes
  hypotheses, metrics, and the promotion rule **before** the epoch starts.

### 2.1 The two swing arms

**eva_swing_mech** — pure geometry arm. Whenever control opens a paper
position, a mirror opens in this book with the same entry but: stop moved
beyond the H4 swing/OB that invalidates the thesis plus a volatility buffer
(code-derived from `patterns/`, clamped to sane % bounds), target ladder price
distances scaled ~1.5–2× toward the next structural objective, 7-day max hold.
No prompt changes, no tokens. Any divergence from control is attributable to
exit geometry alone — this is the direct forward test of §1.2.

**eva_swing_llm** — mandate arm. The 30-min vision call gains H12 and D1
charts per product (both already supported by `research.py`; H12 resamples
from paginated H1). The prompt instructs: place the stop beyond the structure
that invalidates the thesis on H4/H12 — not a fixed % — and set targets at
the next H12/D1 liquidity objectives; expect multi-day holds.
`_validate_order_block_entry` gains swing-specific clamps (min/max stop % so
the LLM cannot mint a reckless or degenerate plan).

### 2.2 eva_day

Runs on the existing 60s watchdog scheduler slot — no new process, no new LLM
calls. Loop:

1. Fetch M1 (new `research.py` timeframe config; Coinbase `ONE_MINUTE`, 350-bar
   cap = 5.8h per page, well inside limits at 2 products × 60s).
2. Detect setups with the deterministic layer: existing `patterns/`
   (displacement OBs + fib zones, SFP sweeps, HTF zones) **plus two new
   detectors** — FVG and BOS/CHoCH (§3).
3. **Stance gate:** only take triggers aligned with the direction of the most
   recent vision cycle's stance; skip if the last cycle is older than 45 min or
   neutral. This is the "deterministic between vision updates" constraint made
   concrete.
4. Bracket: stop at the M1/M5 structure that invalidates the setup (≈1R),
   single target +0.75R–1.0R, **hard time exit at 4h** (new capability: the
   paper engine needs a max-hold close; today nothing time-boxes an open
   position).
5. Write to the eva_day paper book.

### 2.3 Schema and engine changes

- `paper_positions` and `suggestions` gain a `variant` column
  (default `'control'`, additive migration, no backfill).
- Paper engine: add optional `max_hold_hours` per position, closing at the
  first M5 close past the deadline.
- `research.py`: add `M1` timeframe config (one line in `_TIMEFRAME_CONFIG` —
  `ONE_MINUTE` is already in `_GRANULARITY_SECONDS`).

---

## 3. smart-money-concepts (github.com/joshyattridge/smart-money-concepts)

Coverage against the in-house `patterns/` package:

| smc function | In-house equivalent | Verdict |
|---|---|---|
| `ob` (order blocks) | `patterns/order_block.py` — displacement-based, volume-aware, fib/OTE zones | **Keep ours** (richer, already Eva's dialect) |
| `swing_highs_lows` | `patterns/swing.find_pivots` | Keep ours |
| `liquidity` (equal highs/lows) | `patterns/sfp.py` — sweep events w/ volume spike + 4-year index | Keep ours (theirs detects resting pools, ours detects the sweep; complementary but not needed for v1) |
| `previous_high_low` | `patterns/key_levels.py` | Keep ours |
| `sessions` | mill has session logic; killzones trivial | Keep ours |
| `retracements` | `fib_zone_bounds` / `near_fib_level` | Keep ours |
| **`fvg`** | **none** | **Add** — port the definition in-house (`patterns/fvg.py`) |
| **`bos_choch`** | none explicit (HTF zones imply structure but no BOS/CHoCH events) | **Add** — `patterns/structure_shift.py` |

Recommendation: **do not take the dependency into the live path.** Two
reasons beyond redundancy: (1) `smc.swing_highs_lows` looks `swing_length`
candles *forward*, so its outputs repaint until confirmation — safe in
backtests, a footgun in a 60s live loop unless lagged carefully; (2) it is
pandas-heavy per call. Port the two missing detectors in-house in the existing
`patterns/` style, and add `smartmoneyconcepts` as a **dev/test dependency
only**, cross-checking our FVG/BOS/CHoCH outputs against theirs on fixture
candles in CI. MIT license, ~2k stars, fine for that role. Control stays
untouched either way.

---

## 4. Dashboard

Mirror the Kalshi multi-bot pattern (`kalshi_bridge.performance_payload` →
hub tab):

- New `eva_variants` payload: per variant — label, blurb, `mode`
  (`live`/`paper` from `EVA_LIVE_VARIANT`), equity curve from the epoch,
  n / win rate / mean R / median hold, open positions.
- Hub tab "Eva Lab" beside the Kalshi tab, same card + table macros
  (`dashboard/templates/_macros.html`), LIVE badge on exactly one variant.
- `/api/eva/variants` endpoint; internal hub only for v1 (no public `/feed`
  exposure until a variant is promoted).

---

## 5. Costs

| Item | Cost |
|---|---|
| eva_swing: +2 charts/product (H12, D1) × 2 products on the existing 30-min call | ≈ +5–6k input tokens/call → +250–300k/day → **≈ $0.75–0.90/day** at Sonnet input pricing |
| eva_day LLM | **$0** — deterministic; reuses the last cycle's stance |
| eva_day data | M1 REST poll, 2 products × 60s = 2,880 req/day (public endpoint, no auth burn) |

---

## 6. Evaluation — what can actually be concluded, and when

Per-trade R has SD ≈ 1.385R on this book. A head-to-head mean-R comparison
detecting +0.35R at 80% power needs **~245 trades per arm** — the swing bot at
control's ~4 trades/week would take a year. So the pre-registration must lean
on faster-converging **mechanism metrics**, with mean R as the slow
confirmatory layer:

- **eva_swing primary metric:** the stopped-then-paid rate. Control runs at
  7/21 (33%) — a third of its book is correct theses killed by geometry. The
  swing variant succeeds structurally if that rate collapses (<10%) without
  mean R degrading vs its own placebo. Interim read at ~6 weeks.
- **eva_day primary metric:** mean R vs its own random-entry placebo at the
  same geometry (the pre-registered pattern), plus hit rate ≥ the placebo's.
  At several triggers/day, n≈100+ inside a month — this arm reaches a real
  verdict fastest.
- **Promotion rule (freeze in pre-reg):** a paper variant may take
  `EVA_LIVE_VARIANT` only after its pre-registered bar clears; only one live
  at a time; control keeps running paper either way so the comparison never
  loses its baseline.

---

## 7. What shipped

| # | Step | Where |
|---|---|---|
| 1 | Variant books + M5 resolution engine (ladder, trailing stop, time exit, stop-first ties) | `eva_variants.py`, own tables in `ledger.db` |
| 2 | M1 timeframe | `research.py` `_TIMEFRAME_CONFIG` |
| 3 | Causal FVG and BOS/CHoCH detectors | `patterns/fvg.py`, `patterns/structure_shift.py` |
| 4 | Day variant: stance gate, break+FVG conjunction, cooldown, 4h cap | `eva_day.py`, `eva_day_scan` job |
| 5 | Mechanical swing arm: structural H4 stop + scaled ladder | `eva_swing.py`, mirrored from the cycle |
| 6 | Swing mandate arm: own 2h vision call, H12/D1, plan clamps | `eva_swing_llm.py`, `eva_swing_llm_cycle` job |
| 7 | Eva Lab tab, R-normalized four-book comparison | `eva_variants_bridge.py`, `/api/eva/variants` |
| 8 | Pre-registration and promotion bar | `EVA_VARIANTS_PREREG.md` |

Detector note: the `smartmoneyconcepts` definitions were **ported, not
imported**, and the library is not a dependency. Its `fvg()` reads
`.shift(-1)` (a gap flagged on a bar using the *next* bar's data), its
`swing_highs_lows()` uses a forward-looking rolling window plus a `while True`
loop that retroactively deletes swings it already flagged, and `bos_choch()`
back-dates its flag to `last_positions[-2]`. All three are fine for offline
charting and unusable for a live trigger. Our ports are covered by explicit
causality tests asserting that appending bars never changes a signal already
reported.

Control safety: the only edit to the live path is one guarded block in
`agent.py` that calls the two paper mirrors and swallows their exceptions. No
change to `paper.py`, `execute.py`, `vault.py`, or the vision prompt.

## 8. Decisions log

Resolved 2026-09-14 (all reflected in §2):

1. **Day-bot entries:** both sources, tagged — day-bracketed mirrors of vision
   suggestions (the path the §1.3 evidence actually supports) plus
   deterministic M1 triggers (the hypothesis), scored separately.
2. **Swing arms:** both — `eva_swing_mech` (mechanical re-bracket, cleanest
   attribution, zero token cost) and `eva_swing_llm` (H12/D1 mandate).
3. **Trigger budget:** cooldown — one open eva_day position per product per
   side; excess triggers recorded as skips.

Resolved during the build (both forced by the code, see the header):

4. **Variant storage:** separate tables, not a `variant` column — `paper.py`
   cannot host four books and must not be refactored for an experiment.
5. **Swing LLM cadence:** dedicated 2h call, because a shared call cannot show
   H12/D1 to swing while hiding it from control.

Measured while building, and it shaped the day trigger: on live M1, structure
breaks fire **~1.3x/hour/product** and FVGs **~4x/hour/product**. Either alone
would open dozens of positions a day, which is why the trigger requires a
break *in the stance direction* followed by a retrace into an unmitigated gap,
and why the cooldown is load-bearing rather than a formality.
