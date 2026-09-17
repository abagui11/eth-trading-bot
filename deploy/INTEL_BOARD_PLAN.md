# Intelligence board rebuild plan — the brain's window, made honest

Written 2026-09-17, following the vision-accuracy study
(`trade_ideas/analysis/EVA proof of concept/VISION_ACCURACY.md`) and the
failure localization (`trade_ideas/analysis/VISION_LOCALIZATION.md`).
This is a plan, not an implementation — nothing here is shipped, and each
phase names the evidence that would kill it. Companion prereg thinking
follows `EVA_VARIANTS_PREREG.md`.

---

## 1. Where the board sits in the product

One brain, two LLM surfaces, five products hanging off it:

| Surface | What it is | Measured state |
|---|---|---|
| **Entry engine** (`analyze.py`, 30-min cycle) | Vision call on marked charts → conditional trade objects → critic-verified → paper book, live path, offers, vault, tester pool, cards, case studies | Entries beat placebo 35/35 geometry cells; levels miscalibrated; geometry fixes running as controlled paper variants |
| **Stance board** (`intelligence/stance.py`, hourly) | Feature-vector LLM call → six scalar stances, served two ways: read locally (dashboard Brain tab, public marketing API) and over the authenticated **`/api/v1/intelligence/*` service API** to other services | Coin-flip at its own horizons; trend exposure masquerading as edge; LLM overrides of its own features cost 19–128 bps (CI < 0 in 15/18 cells, both regimes) |

**The board's consumer graph (traced 2026-09-17), broad and money-adjacent:**

| Consumer | How it uses the board | Coupling |
|---|---|---|
| **Trade-ideas mill** (`trade_ideas/mill.py` via `intel_client.py`) | `_trend_of` → `_resolve_direction`: the **H1 stance becomes the long/short direction** of session ideas, zmove framing, funding cards, and the BTC-flip→ETH cascade; "sell-the-news" fades headlines that conflict with it; neutral falls back to the mill's local `structure_bias` | direction-setting for subscriber Accept/Reject cards + the nano live clip |
| **Kalshi 15m bots** | `h1_bias_tag` gate, 82% concordant with the board (derivation untraced — Phase 0) | gate on sized paper books; live candidates |
| **Yield gen bot** | HTF posture panel via `/api/v1` (per `intel_api.py`; internals live in that service) | advisory as far as traced; must be confirmed, not assumed |
| **Public marketing API / site** | deduped stance grid, long-thesis bias | brand surface |
| **Dashboard Brain tab, Telegram/Twitter** | stance + rationale display | brand surface |
| **Future strategies** | the strategy-factory pattern explicitly mints new strategies off persisted stances (the Kalshi case studies were built exactly this way) | every future strat inherits whatever the board is |

So the board is not commentary with a dashboard: it is the shared directional
substrate of the volume lane and the factory pattern. The watchdog and the
variants lab are the only trading components that genuinely do not consume it.

**The product problem this creates.** The larger goal (FalconX roadmap, AI
Vaults) is a strategy factory whose brand is *recorded books and measured
edge* — the front page is designed as a window into the brain, and the
marketing site republishes the stance grid. Right now the window displays the
brain's weakest measured organ, publishing ~48–51%-accurate directional calls
under the same brand as the entry engine's real edge, while a revenue-adjacent
gate (Kalshi) partially keys off it. The board is the most visible and least
defensible component. That inversion is what this plan fixes.

**The design asset we already have.** The deterministic ICT layer is built,
tested, and trusted: H4 OB/BRKR zones (`patterns/htf_structure.py`), M5 OBs,
SFP/sweep detection (`patterns/sfp.py`), key levels, 24h range state, and —
importantly — causal FVG (`patterns/fvg.py`) and BOS/CHoCH
(`patterns/structure_shift.py`) detectors already exist with causality tests.
The stance board simply doesn't use any of it: it runs on a second, cheaper
feature vocabulary (EMA cross, HH/LL flags). The rebuild is mostly assembly,
not new detection. Known gap: no dedicated equal-highs/equal-lows liquidity
pool detector (swing highs/lows + key levels approximate it; a real one is a
small detector with the same causality-test pattern).

---

## 2. The phases

Ordered so every phase produces decision-grade evidence for the next, and the
board's public surface never carries an unmeasured claim.

### Phase 0 — instrument, trace, freeze, mine (no behaviour change; ~2 days)

1. **Log the counterfactual.** Add `det_stance`, `override_reason` columns to
   `intel_stances`; write the deterministic stance alongside the LLM stance
   every cycle. Turns the override study into a standing ledger.
2. **Enumerate and freeze the consumer contract.** `/api/v1` keeps serving
   the scalar shape unchanged for the whole measurement period — conditional
   reads ship *additively* (new fields / a v2 payload), never as an in-place
   change of meaning. Confirm the yield bot's actual use (advisory panel vs
   anything decision-bearing), trace the Kalshi `h1_bias_tag` derivation, and
   pin each consumer to the version it reads today. A substrate consumed by
   direction-resolving strategies cannot change semantics silently: every
   consumer migrates explicitly, one at a time, and each migration gets its
   own before/after measurement on that consumer's book (mill hit rate,
   Kalshi net $/contract).
3. **Mine the 4,272 audit verdicts** for per-primitive LLM-vs-overlay
   disagreement rates (OBs, zones, SFPs, key levels). This is the perception
   bench, from recorded data, at zero model cost. Analysis job in
   `trade_ideas/analysis/`, no bot change.
4. **Baseline the mill's stance-keyed sources.** Segment the mill's recorded
   book by source (session/cascade/zmove/news/funding) so the Phase 1 and
   Phase 2 migrations have per-source before/after numbers. The session and
   cascade lanes take their direction *entirely* from the H1 stance — they
   are the purest downstream measurement of board quality that exists.

*Expected:* a standing override P&L; a yes/no on gate coupling; per-primitive
perception rates. *Prediction to hold ourselves to:* unchanged, the override
channel keeps costing ~20–60 bps per override at 24h. If it doesn't, the
earlier finding was regime-local and Phase 1 loses its justification.

#### Phase 0 status — shipped 2026-09-17

Items 1, 3 and 4 are done; item 2 is partly done (the contract is frozen in
code, the Kalshi gate derivation is still untraced).

- **1. Counterfactual logging — live.** `intel_stances.det_stance` /
  `override_kind` / `override_reason`, `ALTER`-migrated, kind derived in
  `store.classify_override`. `STANCE_LOG_COUNTERFACTUAL=True`. The prompt is
  byte-identical while the Phase 1 flags are off, so the no-behaviour-change
  claim holds by construction (pinned by
  `test_counterfactual_attached_without_changing_stance`).
- **2. Contract frozen, and the Kalshi trace is DONE — it found the opposite
  of what the concordance implied.** `_stance_entry` documents the frozen keys
  and appends the counterfactual fields additively. Traced in
  `kalshi_15m_bot/` on 2026-09-17:

  - **`h1_bias_tag` is NOT the stance board.** It is
    `kalshi_triggers.htf_bias_from_context(ctx)`, read off the Kalshi bot's
    *own* `MarketContext.setup_tags` (`htf_bull` / `htf_bear` / `htf_mixed`)
    computed by its own `patterns/` detectors. The 82% concordance was two
    independent trend reads agreeing on the same tape — expected, not
    coupling. **This field does not move when the board's policy changes.**
  - **But the board *is* consumed, through a path the concordance never
    touched.** `deploy/_vps_eva_wick.py` (the live wick strategy since the
    09-16 replacement) calls `eva_intel.get_stances()`, which reads
    `/api/v1/intelligence/latest`, and it uses the board **as its direction
    source**:
    - `lean = h1["stance"] if h1["stance"] != "neutral" else h4["stance"]`
      picks the side for both `fade_pop` patterns;
    - `m15["stance"]` plus `m15["confidence"] >= EVA_WICK_MIN_M15_CONF` gates
      `buy_overshoot` and the `fade_pop` M15 regime filter;
    - it **fails closed** — stances unavailable or older than
      `EVA_STANCE_MAX_AGE_MIN` returns `None` and the strategy does not trade.
  - **The trace caught a real defect I had shipped.** `apply_override_policy`
    was replacing the published *stance* while leaving the LLM's *confidence*
    attached, so a reverted row published the model's confidence in a
    different call — and `eva_wick` thresholds on exactly that number. Fixed:
    confidence is recomputed as `min(|score|/3, 1)`, the same formula the
    deterministic fallback uses. This is the single strongest argument for
    tracing consumers before flipping a shared substrate.
  - **Consequence for the fundraise:** the Kalshi figures in the evidence
    pack are on the pre-epoch policy, which is already footnoted in
    `EVA proof of concept/README.md`. `eva_wick` is a **paper** book, so no
    real money is exposed to the change; `eva_streak` (the 75% live
    reversal) sets its own `ict_bias` from its run logic and does **not**
    read the board, so the live Kalshi edge estimate is unaffected.
- **3. Perception bench — first read is a warning, not a clearance.**
  `_q0917_perception_bench.py` over 4,244 hourly verdicts:
  - an **M5 order block cited but not matchable by the detector on ~20% of
    cycles** (20.4% of abstentions, 18.2% of trades) — a real per-primitive
    disagreement rate, and a lower bound, since these verdicts are written
    *after* the refine pass.
  - **already-invalidated primitives cited on 4.0% of abstentions but 0.0% of
    trades.** That asymmetry is the critic doing its job:
    `INVALIDATED_SFP_CITED` is a critical code, so it downgrades the cycle
    rather than shipping it. The trades look clean *because* the dirty ones
    were blocked — raw model perception is materially worse than the trade
    column shows, and the deterministic verification layer is carrying it.
  - only **44 of 4,244 cycles produced a trade** (abstention-first by design),
    so the trade column settles nothing on its own.
  - **The gap this bench cannot close:** a NOT_FOUND may be a hallucinated
    level or a real level priced just outside the matcher's window —
    perception vs localization, which imply different fixes. Cheapest next
    step is to have `critic.verify_deterministic` record the
    nearest-detected-zone distance alongside the finding. Until then, Phase 4
    cannot be decided.
- **4. Mill baseline — recorded.** `_q0917_mill_baseline.py`, per lane on the
  0916 snapshot: session (direction *is* the H1 stance) n=178, 46.6% win,
  +0.082% mean; cascade n=62, 48.4%, +0.023%; pooled board-keyed n=240,
  47.1%, +0.067%, t=0.73. Everything else n=629, 42.1%, −0.137%, t=−2.84.
  Note the direction of that: the board-keyed lanes are the **least bad** in
  the mill, and the purely local `spike` scan is the worst (40.0%, −0.187%,
  t=−2.55). Consistent with the board carrying a weak momentum signal that
  still beats the mill's local alternative. None of it is significant, and it
  is one epoch — this is the comparison baseline, not a defence of the board.

### Phase 1 — stop the measured bleeding (built 2026-09-17, both flags OFF)

Deterministic stance passes through as the published direction; the LLM's
role becomes synthesis and narrative (the rationale, the medium summary, the
BTC→ETH note). Overrides either removed or gated: only with a cited array and
price, logged via Phase 0 columns.

**Implemented and tested; not enabled.** `stance.apply_override_policy` plus
two flags, defaulting off because enabling either changes what every `/api/v1`
consumer reads:

- `STANCE_OVERRIDE_REQUIRE_EVIDENCE` — an override must cite a price
  (`_cites_price`, threshold ≥100 so a bare `0.618` does not qualify);
  unevidenced ones revert. Appends `_OVERRIDE_EVIDENCE_RULE` to the prompt,
  which tells the model plainly that keeping the programmatic stance is the
  correct answer when it has no level, so the rule cannot be satisfied by
  inventing one.
- `STANCE_PUBLISH_DETERMINISTIC` — reverts every override, evidenced or not.

A revert makes `stance == det_stance`, which would erase the attempt from the
row, so the attempted stance is preserved in `override_reason` as
`[reverted:<stance>] <why>`. Note the consequence for analysis: the clean
*structural* record of override behaviour only exists while the flags are off
(Phase 0), which is the other reason to run Phase 0 first.

**Enablement order, when we take it:** `STANCE_OVERRIDE_REQUIRE_EVIDENCE`
first (it lets evidenced overrides survive, so it is the smaller
intervention), measured for ≥2 weeks against the Phase 0 override ledger and
the per-lane mill baseline; `STANCE_PUBLISH_DETERMINISTIC` only if the
evidence gate proves insufficient.

*Expected:* the board stops being *harmful* — it will **not** become
*accurate*. The deterministic score is momentum; own-horizon accuracy should
stay ~coin-flip. Anyone reviewing this plan should expect the Phase 1 board
to still fail the directional metric. That is why Phase 2 exists.
*Falsifier:* on the forward window, det-published stances underperform the
counterfactual LLM stances (the Phase 0 ledger makes this checkable weekly).

### Phase 2 — the conditional read (schema + scoring; ~3–5 days build)

Feed the stance job the primitives the brain already detects, and change the
output contract per (product, timeframe):

```json
{
  "attracting": {"kind": "pool|fvg", "lo": 0, "hi": 0},
  "repelling":  {"kind": "ob|breaker|mitigation", "lo": 0, "hi": 0,
                 "side": "bullish|bearish", "state": "holding|traded_through|untested"},
  "location": "premium|discount|equilibrium",
  "invalidation": {"price": 0, "trigger": "m5_close_through"},
  "bias": "bullish|bearish|null"
}
```

`bias` only when an array is holding **and** a draw on liquidity is named;
null otherwise. Conventions named as ours, not doctrine: M5 close-through
invalidation (wick-through as sensitivity), three-candle FVG per `fvg.py`,
displacement per `structure_shift.py` thresholds. New table (`intel_reads`),
old table keeps writing — the scalar metric **is the control**.

Pre-registered scoring, nightly job, same M5 barrier engine as the evidence
pack:

| Metric | Definition | Success bar (set before looking) |
|---|---|---|
| Conditional accuracy | attracting level prints before invalidation, among reads whose invalidation didn't strike first | beats the scalar control on matched anchors over ≥3 forward weeks |
| Stale-invalidation rate | "holding" claimed while price already closed through the array | < 5% (else the fault is conditional logic, not vision) |
| Null calibration | realized range on null hours vs directional hours | null hours measurably quieter |
| Scalar control | the existing directional metric, same rows | keeps running, published next to the rest |

*Expected:* genuinely uncertain, and that is the point — the entry engine's
placebo edge says chart-anchored conditional reads *may* carry signal; this
is the first artifact that will test it directly. *Falsifier:* conditional
accuracy ≤ scalar control after 3–4 weeks → the board's reads carry no
structure information even on their own terms → Phase 4 decision.

### Phase 3 — front page and aggregation (after Phase 2 has ≥2 weeks of scores)

The window into the brain starts showing **predictions with their scored
outcomes**, which is the only version of transparency consistent with the
vault-marketplace goal (every vault card is a recorded book + measured edge;
the brain page should meet its own bar):

- Stance chip becomes the conditional read: "bullish while 63.4k OB holds →
  drawing 65.1k", with live state (holding / invalidated / target printed).
- A rolling scorecard per timeframe: conditional accuracy, stale rate, null
  calibration, and the scalar control — including when they're bad. The
  losing epochs stay on the page the way the mill's re-based epochs do.
- Cross-timeframe posture as a categorical state, not an average: **aligned /
  nested pullback / conflict→stand-down**, from the DOL state machine.
  (Nothing averages today; this creates aggregation rather than fixing it.)
- Public API republishes states + scorecard, not a raw scalar grid.

*Expected:* no P&L claim — this phase is product honesty. *Falsifier:* n/a
(display), but the scorecard makes every later claim self-falsifying.

### Phase 4 — the structural decision (evidence-gated, not scheduled)

Decided by Phase 0.3 + Phase 2, not by preference:

- **If** per-primitive disagreement is high or conditional accuracy fails →
  detection stays fully programmatic everywhere; the LLM's remit shrinks to
  selection + narrative on top of detected levels. For the bias layer the
  evidence already leans this way; for chart vision it is open until the
  audit mining lands.
- **If** disagreement is low and conditional accuracy clears its bar → the
  vision layer keeps selection authority and the same schema extends to the
  entry engine's published theses.

Throughout: **the entry engine's direction logic is not touched.** Its edge
is the one measured positive; its geometry question is already running as
controlled paper experiments (`eva_geom`, `eva_swing_*`) with a promotion bar
in `EVA_VARIANTS_PREREG.md` §4. This plan neither accelerates nor bypasses
that bar.

---

## 3. What we expect to see, stated before we look

1. **Phase 1:** override drag disappears from the standing ledger; board
   accuracy stays ~50% at own horizons. If accuracy jumps, we were wrong
   about the mechanism and should say so. Downstream, the mill's
   stance-keyed sources (session, cascade) should improve *slightly* — they
   inherit the removed override drag — but stay unproven, because the
   deterministic stance is still momentum.
1b. **Phase 2 at the mill:** `bias: null` must map to an explicit mill
   behaviour (skip the mint, or fall back to its local `structure_bias`) —
   decided and named in the migration, not left to the current implicit
   neutral-fallback path.
2. **Phase 2:** stale-invalidation rate low (the features are read
   correctly); conditional accuracy is the genuine unknown. Prior: weakly
   positive, by analogy to the entry engine's placebo edge — but the analogy
   is exactly what's untested.
3. **Phase 0.3 mining:** either outcome is decision-grade. High agreement
   with overlays → perception fine, selection is the frontier. Low →
   detection-in-code is settled and Phase 4 resolves without debate.
4. **Kalshi:** if the gate proves to be the board, expect its measured edge
   to shift when the board changes — which is why the gate version freezes
   before anything else moves.
5. **The regime caveat on everything:** Aug–Sep contained one regime change.
   Any Phase 2 success inside a single regime is provisional by the pack's
   own rules; the promotion bar requires surviving a second.

## 3a. Consumer counterfactuals — shadow-wired everywhere (2026-09-17)

Staging one consumer at a time was the wrong call: each consumer keeps its own
book, so attribution was never the blocker. The blockers were that the read
emits `null` by design and no consumer has null semantics, and that we do not
yet know it is better. Both are solved by recording rather than acting, and
that pattern generalises — so **every** consumer now logs what it *would*
have done, in parallel, at zero risk.

| Consumer | Records | Where |
|---|---|---|
| HQ | H4 read's bias beside the suggestion's action | `intel_read_counterfactuals` (hub) |
| Mill | read's direction beside the direction it minted on | `ideas.meta_json.cond_read` |
| `eva_wick` | what `lean` / side would have been, incl. abstentions | `kalshi_decisions.setup_tags` + log |

Three stores because three hosts; analysis joins them. Each site is wrapped
and fail-soft, and none of them can reach a direction: the property pinned by
`test_direction_is_never_changed_by_the_read` and
`test_conditional_block_does_not_affect_get_stances`.

**`null` is an abstention, not a neutral — everywhere.** The mill's existing
fallback turns `neutral` into a direction via local `structure_bias`; mapping
a withheld bias to neutral would let it invent the call the read declined to
make. Recorded as `abstain` with `agreed = None`, because there is nothing to
agree or disagree with.

**HQ is recorded but deliberately not informed.** The read is not in the
proposal prompt. The 52-trade placebo result is measured on the current
prompt, so feeding it in would forfeit the one validated comparison HQ has,
in exchange for the slowest test surface we own (~1 trade/day against the
mill's 240 closed trades in five weeks).

### Barrier-implied baseline

`read_scorer.barrier_implied_hit_prob` nets the geometry off the accuracy.
"Target before invalidation" is not interpretable alone — a draw 0.5% away
against an invalidation 2% away wins that race ~80% of the time by geometry,
exactly as a 0.3R target beats a 1R stop. For a driftless walk between two
absorbing barriers the chance of reaching the target first, *given* one was
reached, is the opposite barrier's share of the distance (gambler's ruin).
`skill_over_geometry = conditional_accuracy - mean(implied)` is the number to
read; the raw rate is not. Conditioning on "one barrier was reached" is why
it needs no horizon term and why it is averaged over decided reads only.

## 3b. Reverting (one flag, plus a stamp)

The whole point of the epoch marker is that this is cheap to undo:

1. `bot_config.STANCE_PUBLISH_DETERMINISTIC = False`
2. **Stamp a new `STANCE_POLICY_EPOCH`** — do not silently reuse the old one,
   or the book becomes three policies deep with no way to segment them.
3. `sudo bash /opt/eth-trading-agent/deploy/update.sh`

Nothing needs migrating back: `intel_stances` keeps all three stance columns
under either policy, `intel_reads` is a separate table nobody consumes, and
`/api/v1`'s scalar contract never changed shape. Phase 2 can be left running
through a revert — it is independent of which stance gets published.

To stop Phase 2 as well: `INTEL_CONDITIONAL_READS_ENABLED = False`. The table
and its rows stay; only the writes stop.

## 4. Doc obligations when implemented

Per `deploy-docs.mdc`: Phase 0/1 touch `intelligence/stance.py`, `store.py`
schema → update `PROJECT_STATE.md` §7 (persistence), §9 (flags), changelog.
Phase 2 adds a table + nightly scorer → same, plus `CLOUD.md` if a new
systemd timer. Phase 3 touches dashboard + public API → §8 rows for
dashboard/marketing API. Config values must match `bot_config.py` at ship
time.
