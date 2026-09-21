"""Bot runtime configuration (non-secret tunables)."""

from __future__ import annotations

# Products the hourly cycle and watchdog may trade concurrently.
TRADED_PRODUCTS: tuple[str, ...] = ("ETH-USD", "BTC-USD")
DEFAULT_PRODUCT_ID = "ETH-USD"

# Maximum simultaneous open paper positions. When full, oldest position is
# closed at market (FIFO) to make room for a new trade signal.
MAX_OPEN_TRADES = 20

# When True, hourly DMs go only to subscribers on real trade actions (not no_trade).
BROADCAST_ONLY_TRADES = True

# Pre-broadcast audit refine loop (propose_trade retries after fact-check failures).
MAX_REFINE_PASSES = 1
RUN_LLM_CRITIC_PRE_BROADCAST = False

# Attach Trading Guide/*.png reference examples to Claude vision calls.
# Off by default — live marked charts + guide text are enough and cut ~5 images/call.
INCLUDE_PATTERN_IMAGES = False

# LLM rewrite of trade-card blurbs (else deterministic_setup_blurb).
USE_LLM_DISPLAY_SUMMARY = False

# Annotated close-chart for Eva (HQ) live trades. One Haiku call per close;
# numbers always come from the ledger. Off skips generation entirely.
CASE_STUDY_ENABLED = True
USE_LLM_CASE_STUDY = True

# Fixed-fraction position sizing: each trade deploys this fraction of live paper
# equity as notional (cash + open positions marked to spot). R/R, stop, and
# take-profit logic are unaffected — this only sets trade size.
TRADE_DEPLOY_PCT = 0.25

# M5 OB fib entry band (bullish: from block low; bearish: from block high).
ENTRY_FIB_LOW = 0.25
ENTRY_FIB_HIGH = 0.50
ENTRY_FIB_TRANCHE_1 = 0.25  # 50% of base deploy at this level
ENTRY_FIB_TRANCHE_2 = 0.50  # remaining 50% of base deploy
ADD_FIB_LEVEL = 0.718  # scale-in adds another full TRADE_DEPLOY_PCT
ENTRY_TRANCHE_DEPLOY_PCT = TRADE_DEPLOY_PCT / 2  # 12.5% per tranche
ADD_DEPLOY_PCT = TRADE_DEPLOY_PCT  # +25% at 0.718 → 1.25× base exposure
FIB_LEVEL_TOLERANCE_PCT = 0.008  # looser "near" fib mark for M5 watchdog

# Paper position size guardrails per product, applied after fixed-fraction sizing.
# Legacy aliases MIN_ETH_QTY / MAX_ETH_QTY keep older call sites working.
PRODUCT_QTY_CAPS: dict[str, tuple[float, float]] = {
    "ETH-USD": (0.25, 2.0),
    "BTC-USD": (0.005, 0.05),
}
MIN_ETH_QTY = PRODUCT_QTY_CAPS["ETH-USD"][0]
MAX_ETH_QTY = PRODUCT_QTY_CAPS["ETH-USD"][1]

# Paper scales out on the ladder the live sleeve could actually place, so the
# published journal does not show profit taken at targets a whole-contract clip
# can never reach. This shapes the *weights* only — paper sizing is still
# fixed-fraction on paper equity. False reverts paper to an even split across
# every target, which is what it did before 2026-09-11.
PAPER_LADDER_MATCHES_LIVE = True

# Shared paper book: fake Fund deposit (placeholder for future real funding).
PAPER_CONTRIBUTION_USD = 1000.0
# Telegram user id reserved for the house seed stake in paper_contributions.
HOUSE_CONTRIBUTION_TELEGRAM_ID = 0

# Personal demo accounts (opt-in Accept/Reject). Separate from the house/agent book.
PAPER_ACCOUNT_SIZES: tuple[float, ...] = (500.0, 1000.0, 2500.0)
PAPER_ACCOUNT_DEFAULT_USD = 1000.0  # migration amount for legacy Funders
APPROVAL_WINDOW_MIN = 15
MISSED_CONNECTION_R = 0.5
# Minimum cash required to Accept / late-join a trade.
USER_MIN_DEPLOY_USD = 25.0
# One-time launch notice after personal-books migrate (ops may reset).
LAUNCH_NOTICE_SENT_KEY = "personal_books_launch_v1"

# Minimum OB zone width as % of mid price.
# HTF (H4) keeps the swing-style filter; M5 entry candles are much thinner.
# BTC H4 candles are typically narrower in % terms than ETH, so BTC uses a
# lower HTF floor while ETH keeps the original 1.25% swing filter.
OB_MIN_WIDTH_PCT = 1.25
OB_MIN_WIDTH_PCT_M5 = 0.15
PRODUCT_OB_MIN_WIDTH_PCT: dict[str, float] = {
    "ETH-USD": OB_MIN_WIDTH_PCT,
    "BTC-USD": 0.60,
}

# Label for the current paper epoch (shown on dashboard after reset).
PAPER_EPOCH_LABEL = "5k_usd"

# How often the full LLM trade cycle runs. Slots are wall-clock aligned, so
# 1800 fires on :00 and :30. Halving this doubles idea flow and LLM spend; how
# many of those ideas can be *held* is bounded by LIVE_MAX_OPEN_HQ, so the
# practical effect is that a freed slot refills within 30 minutes instead of an
# hour. Keep at or above IDEA_EXPIRY_MINUTES so a card cannot outlive its cycle.
CYCLE_INTERVAL_SEC = 1800  # 30 minutes

# Backstop only. What actually bounds a pending paper entry is re-evaluation:
# a fresh plan for the product supersedes it and a `no_trade` verdict cancels
# it, which at a 30-minute cadence retires the median plan in ~1.4h. This clock
# exists for the case where a product stops being evaluated at all — a skipped
# cycle, a dead candle feed, a config change to TRADED_PRODUCTS. Do not tune it
# against P&L; the limit-fill sweep is non-monotonic across expiries and any
# level picked off that curve is noise.
PAPER_PENDING_EXPIRY_HOURS: float = 4.0

# Live waits for its entry too, rather than buying the mark the moment a plan
# is minted. Over the 15 HQ plans of the current epoch the plans whose entry
# had not traded were worth +0.317R at Eva's price and -0.352R at the price
# live paid, and price never returned to those levels — so the choice was
# never "fill better", it was "fill worse or pass". See analysis/
# run_fill_study.py in the trade_ideas repo. The effect is not significant on
# 15 trades (p = 0.269); this ships because an entry that has not traded is a
# premise that has not been confirmed, not because of the P&L.
LIVE_PENDING_ENTRIES_ENABLED: bool = True
# Backstop only, same argument as PAPER_PENDING_EXPIRY_HOURS above: what really
# retires a live plan is the next cycle either replacing it or declining the
# product. This clock only matters if a product stops being evaluated at all.
# Do not tune it against P&L — per-trade R falls monotonically as the window
# widens, so any interior optimum is noise.
LIVE_PENDING_EXPIRY_HOURS: float = 4.0

# --------------------------------------------------------------------------
# Eva HQ variant experiment (see deploy/EVA_VARIANTS_PLAN.md).
#
# Four books, one live. `control` IS paper.py and is never written by the
# variant code — that is why the variants live in their own tables rather than
# behind a `variant` column on paper_positions. EVA_LIVE_VARIANT names the only
# book allowed to touch real money; promoting anything off `control` requires
# its pre-registered bar in EVA_VARIANTS_PREREG.md to clear first.
EVA_VARIANTS_ENABLED = True
EVA_LIVE_VARIANT = "control"          # the only book whose PROMOTION bar has run

# --- Live mirrors for the two leading variant books (2026-09-21) -------------
# Operator risk decision, recorded in EVA_VARIANTS_PREREG.md (amendment
# 2026-09-21): eva_swing_llm and eva_day trade live ALONGSIDE control, ahead of
# the §4 bar. This is an addition, not a promotion — the paper books stay the
# measurement instrument, the epoch is unchanged, and live results are
# excluded from the §4 statistics. Each flag is a kill-switch: off stops the
# mirror without touching the paper book or the experiment.
EVA_SWING_LLM_LIVE_ENABLED = True     # mirrors entry_source == "swing_vision"
EVA_DAY_LIVE_ENABLED = True           # mirrors entry_source == "vision_rebracket"
                                      # m1_trigger is EXCLUDED: negative prior
                                      # (prereg §2 H3), zero closed positions.
# Fixed dollar risk per live variant clip, like the paper books' $10 — NOT a
# portfolio fraction, so the live mirror stays recognisable next to its paper
# twin. Venue contract floors can force more risk than the budget on wide
# swing stops; the ceiling below bounds how much more before the clip is
# skipped instead.
LIVE_VARIANT_RISK_USD = 12.0
LIVE_VARIANT_MAX_RISK_USD = 40.0
# eva_day re-brackets control's own entries, so control-live + day-live can
# stack the same idea. Allowed, but combined open risk per (product, side)
# across the hq family (hq + hq_swing + hq_day) is capped here — chosen over
# "skip day when control filled", which would silently censor day's live book
# on exactly the entries it shares with control.
LIVE_VARIANT_STACK_RISK_CAP_USD = 45.0
LIVE_VARIANT_MAX_OPEN = 2             # per variant source
# A live mirror is only honest if it fills near the paper book's entry. If the
# market has already left the plan's entry by more than this, skip the mirror
# (the paper book still opens; the divergence is logged).
LIVE_VARIANT_MAX_CHASE_PCT = 0.003
# Compared as a string against `closed_at`, so it can carry a time. It is set
# to the moment the candle-window fix shipped rather than to midnight: every
# position resolved before it — control's included — was walked against bars
# that predated its own entry, so those results are not measurements of
# anything and must not sit in the same sample as what follows.
# (2026-09-16: vol-conditioned geometry was briefly applied to control/live/
# swing-llm and rolled back the same hour into the eva_geom paper book below.
# Zero positions were booked in the window — every cycle said no_trade — so
# this epoch did not need to move.)
EVA_EXPERIMENT_EPOCH = "2026-09-15T15:10:00Z"

# eva_geom — control's exact entries, vol-conditioned brackets. PAPER ONLY:
# the rule replay-won on the recorded book (trade_ideas/analysis/
# _q0916_dynamic_geometry.py: mean R +0.128 -> +0.251, placebo delta +0.33,
# cost = truncated tail winners, none flipped negative) but overriding the
# LLM's structural ICT levels is itself an untested hypothesis, so it earns a
# forward paper book before it may touch control. The floor is the recorded
# book's p75 stop/ATR ratio and the cap sits between p25 and p50 of 48h
# favorable excursion — structural anchors on a broad plateau (floor 5-7 x
# cap 6-8 all beat baseline), not swept optima. n=52: calibration, not proof.
EVA_GEOM_ENABLED = True
EVA_GEOM_STOP_FLOOR_ATR = 7.0   # stop >= 7 x trailing-24h mean M5 range %
EVA_GEOM_TP_CAP_ATR = 8.0       # TP rung k <= k x 8 x ATR24 from entry
EVA_GEOM_MAX_STOP_PCT = 0.045   # hard ceiling on the floored stop (4.5%)
EVA_GEOM_MAX_HOLD_H = 48.0      # the replay's horizon; unresolved = unmeasured

# Equal dollar risk per trade across all books, so a wider stop buys a smaller
# position. Without this the swing arms would beat control by betting more.
VARIANT_RISK_USD = 10.0

# eva_day — fast ICT, paper only, zero new LLM calls.
EVA_DAY_SCAN_INTERVAL_SEC = 120       # M1 trigger scan cadence
EVA_DAY_M1_TRIGGERS_ENABLED = True
EVA_DAY_MAX_HOLD_H = 4.0              # hard close; the variant's whole premise
EVA_DAY_TP_R = 1.0                    # single near target, swept in paper
EVA_DAY_MIN_STOP_PCT = 0.0015         # 0.15% — below this the stop is spread
EVA_DAY_MAX_STOP_PCT = 0.0060         # 0.60% — above this it is not a day trade

# eva_swing_mech — control's entries, structural stop, no LLM.
EVA_SWING_MAX_HOLD_H = 168.0          # 7 days
EVA_SWING_TP_RUNGS = (1.5, 3.0, 5.0)  # control's ladder shape, swing distances
EVA_SWING_MIN_STOP_PCT = 0.010        # 1.0% — floor, roughly control's stop
EVA_SWING_MAX_STOP_PCT = 0.045        # 4.5% — ceiling on structural placement
EVA_SWING_FALLBACK_STOP_PCT = 0.025   # used when H4 pivots are unavailable
EVA_SWING_ATR_BUFFER = 0.5            # ATR multiples beyond the swing
# Reach is tested on the furthest rung, not the nearest: with a structural (so
# wide) stop, a real intermediate objective can legitimately sit under 1R.
EVA_SWING_MIN_FIRST_TARGET_R = 0.5
EVA_SWING_MIN_LAST_TARGET_R = 2.0

# eva_swing_llm — its own vision call on a slower cadence. Deliberately NOT
# folded into the 30-min cycle: a single Claude call cannot show H12/D1 to the
# swing mandate while hiding them from control, so sharing the call would
# contaminate the control book. Swing holds for days, so 2h resolution costs
# it nothing and ~12 calls/day keeps the token bill negligible.
EVA_SWING_LLM_ENABLED = True
EVA_SWING_LLM_INTERVAL_SEC = 7200     # 2 hours
EVA_SWING_LLM_TIMEFRAMES = ("D1", "H12", "H4")

# Sub-hourly programmatic entry scanner (charts + no LLM).
WATCHDOG_ENABLED = True
WATCHDOG_INTERVAL_SEC = 60  # 1 minute (valid range: 60–300)
WATCHDOG_COOLDOWN_SEC = 30 * 60  # 30 min — suppress repeat trigger on same M5 OB
# Scan/log always when WATCHDOG_ENABLED; paper fills + subscriber offers only when execute is on.
# Runtime override via user_books meta key WATCHDOG_EXECUTE_META_KEY (dashboard / Telegram).
#
# Do not arm this on the "it fires 60x a day and we're only taking 1" argument.
# The 1,416 shadow fires from 2026-08-06 to 09-10 were replayed on M5 candles
# through the same ladder engine as every other study, filled the way an armed
# watchdog actually fills (market at the mark — `execute._execute` sends a
# market order and never records a live pending), and measured in R because
# `vault.propose` normalises risk. Result: the whole book is **-0.077R per
# trigger**, and no family survives. `m5_ob_fib_long` looked like +0.223R over
# 200 resolved, but 34% of its fires never resolve inside 7 days (its stops
# average 6.85% of price), and forcing those to the worst case flips it to
# -0.187R — so its sign is undetermined, not positive. Day-clustered p was
# 0.090 before Holm and 0.451 after, and 78% of its total R came from 2 of 16
# days. `m5_sfp_sweep_reversal` is the one solid result and it is **negative**:
# -0.443R, 95% CI [-0.780, -0.065], below the placebo's 0th percentile.
# `short_trigger_retest` went 0 for 4 at -1.000R each.
# Under the real sleeve (4 open / 2 per product) only 112 of 1,416 fires are
# takeable at all, and every candidate policy lands between -$45 and +$53 over
# five weeks. The min-k sweep is monotonic unconstrained but **non-monotonic
# once capacity-limited** (+$53 at k>=2.0, -$22 at k>=3.0), which per
# eva-quant-evidence means noise, not a parameter. Read
# trade_ideas/analysis/WATCHDOG_FINDINGS.md before touching this.
WATCHDOG_EXECUTE_ENABLED = False
WATCHDOG_EXECUTE_META_KEY = "watchdog_execute_enabled"
# When execute is on, still block short fires unless this is True (inverted M5 short module).
# Keeping this False has been doing real work: shorts are 1,096 of the 1,416
# shadow fires and ran -0.135R over 1,048 resolved. Note the window is not a
# fair test of the short module — BTC rose 19.4% and ETH 28.8% across it, which
# is why the same-geometry random-entry placebo also lost (-0.204R). Shorts beat
# that placebo by +0.069R, so the module is not worse than chance; it is just
# that being less bad than a losing baseline is still losing money.
WATCHDOG_ALLOW_SHORTS = False
# Scale-in only when unrealized P&L >= this multiple of 1R (entry→stop distance).
SCALE_IN_MIN_R = 0.5

# --- Live execution sleeves (Coinbase US futures — CDE nano contracts) --------
# All LIVE_* values are live-only. Paper sizing (TRADE_DEPLOY_PCT=0.25,
# PRODUCT_QTY_CAPS) is untouched — never reuse paper equity for live size.
LIVE_HQ_EQUITY_USD = 2000.0          # HQ ICT margin sleeve
LIVE_TRADE_DEPLOY_PCT = 0.50         # fallback notional when no clip is set
LIVE_MAX_OPEN_HQ = 4                 # skip new ideas when full (no FIFO kill)
LIVE_MAX_PER_PRODUCT_HQ = 2          # concurrent positions in one product
# A position that has banked a rung and trailed its stop to breakeven cannot
# lose money, so charging it a slot taxes idea flow for risk that is no longer
# there — and Eva's median TP1 takes 20.4h, so a runner can hold a slot for
# days. This many such positions are excused from LIVE_MAX_OPEN_HQ and
# LIVE_MAX_PER_PRODUCT_HQ. Their notional still counts against
# LIVE_MAX_LEVERAGE, which is the cap that actually bounds the book. 0 disables.
LIVE_DERISKED_SLOT_EXEMPT_HQ = 2
LIVE_DAILY_LOSS_LIMIT_USD = 160.0    # 8% of sleeve → halt until next UTC day
# Notional ceiling, not a risk ceiling — per-trade risk is bounded by
# LIVE_HQ_RISK_PCT. Four concurrent clips at their widest (two tight-stop ETH
# at 4 nanos, ~$960 each, plus two BTC at ~$800) is ~$3,520, which needs 1.76x.
# Below that the clip is trimmed to fit rather than refused, so too low a value
# shows up as undersized clips rather than skips. 2x stays the hard cap.
LIVE_MAX_LEVERAGE = 1.8              # notional ≤ sleeve × this (hard cap 2x)
LIVE_SCALE_IN_ENABLED = False        # 0.718 adds are paper-only on live
# Live qty floors per product = one CDE nano contract (orders are whole
# contracts; anything smaller cannot execute). ETH 0.1 ≈ $250, BTC 0.01 ≈ $800.
LIVE_PRODUCT_QTY_FLOORS: dict[str, float] = {
    "ETH-USD": 0.1,
    "BTC-USD": 0.01,
}
# An HQ clip is however many whole nano contracts fit this much risk, measured
# against the price the market order actually fills at rather than the planned
# entry. Eva's entries are pullbacks into an M5 order block, so the fill
# routinely sits away from an untouched stop; sizing off the plan let the clip
# risk 1.39x what was intended (2.20x worst) across the first 15 HQ positions.
#
# Risk-based sizing is also what makes the stop study's numbers transferable.
# At constant dollar risk a 1.5x stop is a 0.67x position, so widening a stop
# costs upside rather than adding exposure. Size the clip any other way and a
# wider stop is simply more risk, and the measured R-multiples do not carry
# over. Levels are never moved to fit the budget; only the clip changes.
#
# 0.7% of the sleeve ($14) sizes an ETH clip at roughly 2-6 nanos depending on
# stop distance. Raised from 0.5% on 2026-09-02 because one BTC nano — its
# smallest tradeable size — risks $5.33-$15.43 across the recorded book, so a
# $10 budget refused BTC as `risk_cap` whenever its stop ran wider than 1,000
# points. At $14 only the 1,543-point outlier is still refused.
#
# This treats a symptom. The cause is that one BTC nano is ~39% of sleeve
# notional and cannot ladder, so BTC's risk granularity is coarser than a
# $2,000 sleeve can express. See section 10 of PROJECT_STATE.
LIVE_HQ_RISK_PCT: float = 0.007
# Watchdog live execution is gated separately from paper execute.
WATCHDOG_LIVE_ENABLED = False
WATCHDOG_LIVE_META_KEY = "watchdog_live_enabled"

# Volume-mill live sleeve (same Coinbase account, internal partition).
# Every mill clip is exactly one CDE nano contract (LIVE_PRODUCT_QTY_FLOORS):
# 0.1 ETH or 0.01 BTC. Notional is then qty × mark; a contract that no longer
# fits the sleeve is rejected by the exposure check. Capital (sleeve + open
# count + daily loss) is the limiter — not a daily fill count. A closed clip
# frees its capital.
LIVE_MILL_SLEEVE_USD = 1400.0
LIVE_MILL_MAX_OPEN = 3
LIVE_MILL_MAX_FILLS_PER_DAY = 0      # 0 = no daily fill cap
LIVE_MILL_DAILY_LOSS_LIMIT_USD = 112.0  # 8% of sleeve, same ratio as HQ

# Temporary testing priority: before an HQ live entry, flatten any open mill
# clip on the same product that is the *opposite* direction. HQ and mill share
# one CDE contract, and mill's resting brackets reserve its whole size, so an
# HQ short into a mill long (or the reverse) is rejected
# PREVIEW_ORDER_SIZE_EXCEEDS_BRACKETED_POSITION — that is what killed the
# 2026-09-03 HQ BTC short at 81,010.97. Same-direction mill is left alone.
# Mill refill is skipped on these closes so it cannot immediately re-open into
# the same conflict. Turn this off once HQ testing no longer needs the lane.
LIVE_HQ_CLEARS_MILL: bool = True

# Objective: keep a mill clip open at all times. When the sleeve is EMPTY the
# next sized idea at or above this confidence self-fills (FIFO — the first
# qualifying mint wins the slot). Once one clip is open the remaining slots
# are reserved for manual Accepts, so the auto path can never crowd them out.
LIVE_MILL_AUTO_FILL_ENABLED = True
LIVE_MILL_AUTO_MIN_CONFIDENCE = 0.5

# Telegram ids whose Accept fills a real clip, bypassing the conviction gate.
# Everyone else's Accept stays paper-only (user_paper_trades).
LIVE_MILL_FILL_TELEGRAM_IDS: tuple[int, ...] = (8282981740, 2037245798)
# Widen that to any approved, funded tester. Without it a tester's Accept only
# reserves a claim against a house fill, and the house filled 62 of 544 recent
# ideas — so the common outcome was "you're in if it fills" followed by nothing.
# The cost of turning it on: a tester's tap deploys house money at house size,
# and trade selection moves from "auto-fill takes the first qualifying mint" to
# "whatever someone taps". Sleeve caps still bound exposure; the selection
# change is **unmeasured** and was shipped as a deliberate product call.
LIVE_MILL_ANY_ACCEPT_FILLS: bool = True

# --- Accept-time revalidation -------------------------------------------
# An idea is priced when it is minted and filled whenever someone taps Accept,
# which can be minutes later. The levels are re-checked against the live mark
# before any money moves: targets the market has already taken are dropped, and
# a setup the drift has ruined is refused rather than filled at a worse price.
LIVE_REVALIDATE_ON_FILL: bool = True
# How far price may run past the entry, in units of the planned risk, before an
# Accept counts as chasing. Holding the stop still while the entry drifts would
# quietly turn a 1R trade into a 2R one on a fixed-notional clip.
LIVE_MAX_CHASE_R: float = 0.5
# Reward:risk floor for the re-anchored plan, measured against the average of
# the targets still ahead — not TP1, which a scale-out ladder puts close in on
# purpose. This is a backstop against ideas that were poor to begin with.
LIVE_MIN_FILL_RR: float = 1.0
# A target closer than this to the mark is not worth resting an order against.
LIVE_TP_MIN_EDGE_PCT: float = 0.1

# --- Idea lifecycle -----------------------------------------------------
# How long a posted card stays acceptable. Past this it is marked expired, so
# silence becomes an explicit pass instead of an offer that never closes, and a
# late Accept is refused rather than filling a stale setup. 0 disables.
IDEA_EXPIRY_MINUTES: int = 15
# When a mill clip closes, replay the recent backlog to refill the sleeve.
# Auto-fill otherwise only ever fires at the moment an idea is minted, so a
# closed clip left the sleeve idle until the next mint happened to land.
LIVE_MILL_REOFFER_ENABLED: bool = True
# How far back the sweep looks. Deliberately longer than IDEA_EXPIRY_MINUTES:
# expiry governs what a person may still tap Accept on, where a stale card is
# judged by eye, whereas the sweep re-prices every candidate against the live
# mark first — revalidation, not the clock, is what keeps it honest. At the
# mill's bursty ~20-30 fillable ideas a day, a 15-minute lookback would leave
# the sweep with nothing to replay in most windows.
LIVE_MILL_REOFFER_MAX_AGE_MIN: int = 120

# Every live open/close/halt is pushed to these chats on top of
# TELEGRAM_ADMIN_CHAT_ID. Both sleeves now fill without a human in the loop, so
# a real fill must never be discoverable only by reading the journal.
LIVE_ALERT_TELEGRAM_IDS: tuple[int, ...] = LIVE_MILL_FILL_TELEGRAM_IDS
LIVE_FILL_ALERTS_ENABLED = True

# --- Tester pool — pooled live allocations with per-user accounting ----------
# 10-20 approved testers share the one Coinbase account. Their Accepts pool
# into the house order (one aggregate fill, virtual pro-rata shares); exits
# ride the house ladder and credit each stake pro-rata. Money movement stays
# manual (admin credits a deposit after it lands); the pool ledger in
# ledger.db attributes it. Everything below is inert until POOL_ENABLED.
#
# ON makes the product approval-gated: unknown users get the pending-approval
# flow instead of the open beta, so run deploy/pool_bootstrap.py alongside the
# flip to grandfather the existing list in as unfunded accounts. With no funded
# account the sizing path is a no-op and house orders are unchanged.
POOL_ENABLED: bool = True
# Risk budget per Accept = this fraction of the tester's cash equity. Matches
# LIVE_HQ_RISK_PCT by design: a tester's slice is sized by exactly the same
# rule as the house clip, just against their own equity.
POOL_RISK_PCT: float = LIVE_HQ_RISK_PCT
# Cash floor to Accept into the pool. Below this a 0.7% risk budget is under
# $3.50 and the share becomes dust that only complicates the audit trail.
POOL_MIN_EQUITY_USD: float = 500.0
# Hard ceiling on how long an Accept may sit reserved before the sweep returns
# the money and tells the tester. The normal release is ref-based — the reserve
# comes back as soon as the order path is gone — and this only catches the case
# where that bookkeeping is wrong, which has happened. Deliberately above
# LIVE_MILL_REOFFER_MAX_AGE_MIN, the longest window in which a mill ref can
# still legitimately fill, so the backstop can never race a real fill and drop
# a tester out of a trade they were promised. 0 disables.
POOL_INTENT_TTL_MIN: int = 150
# Smallest deposit worth the manual ops round-trip.
POOL_MIN_DEPOSIT_USD: float = 500.0
# Reconcile drift beyond this alerts ops and freezes NEW pool intents (house
# trading continues; open stakes keep booking their exits).
POOL_RECON_TOLERANCE_USD: float = 25.0
# Telegram ids allowed to Admit users, credit deposits, and run /credit //debit.
# Merged with POOL_ADMIN_TELEGRAM_IDS from .env (set it there so an operator can
# be added without a deploy). If both are empty this falls back to
# config.INTERNAL_TELEGRAM_IDS, then the admin chat.
POOL_ADMIN_TELEGRAM_IDS: tuple[int, ...] = ()
# How long payouts are held after an admin approves a change of payout
# address. Whoever holds the Telegram account can ask to be paid somewhere
# new, so re-pointing the address is a takeover's first move; the delay is the
# window in which the real owner can notice and say so before funds leave.
# Costs an honest tester a day, once, and only if they move wallets.
POOL_WALLET_COOLDOWN_HOURS: float = 24.0

# -- Withdrawals ------------------------------------------------------------
# A payout costs more than it sends: the network fee rides on top, measured at
# $0.148 on a $2.00 Ethereum USDC send. It is flat, not proportional, so it is
# a large share of a small withdrawal. The tester is debited amount + fee so
# the pool's books stay exactly level with the venue; the floor keeps the fee
# under ~0.3% of anything we actually send.
POOL_MIN_WITHDRAWAL_USD: float = 50.0
# What we assume the fee will be when quoting a maximum, before the real one
# is known. Deliberately above the measured $0.148: quoting a max that the
# balance then cannot cover is worse than quoting slightly low, because it
# fails at the moment a tester is trying to take their money out. Gas spikes,
# so this is headroom rather than an estimate.
POOL_WITHDRAWAL_FEE_RESERVE_USD: float = 3.00

# Caps are blast-radius limits, not product policy. They exist because a bug
# or a stolen Telegram account drains at most this much before a human sees
# it, so they are set by what we could stand to lose in an hour, not by what a
# tester might reasonably want.
POOL_MAX_WITHDRAWAL_USD: float = 2_500.0        # per request
POOL_MAX_USER_DAILY_WITHDRAWAL_USD: float = 2_500.0
POOL_MAX_GLOBAL_DAILY_WITHDRAWAL_USD: float = 5_000.0

# Coinbase offers no idempotency on sends (it rejects `idem` outright), so a
# resend is a second real payment. One payout may be in flight at a time and
# an ambiguous failure halts the queue until a human clears it: with no way to
# ask the venue "did this already go?", continuing would be guessing with
# someone else's money.
POOL_PAYOUTS_ENABLED: bool = True
# Withdrawals to a wallet we have not seen funds arrive from are refused.
# Turning this off pays out to an address whose owner is unproven, which is
# the failure that loses a tester's money to whoever took their account.
POOL_REQUIRE_VERIFIED_WALLET: bool = True
# Send a withdrawal as soon as it clears the coded limits, rather than holding
# it for an admin tap. The approval step was never making a decision — halt,
# caps, a chain-verified destination and the balance check all run at request
# time — so all it added was however long it took someone to see the message.
# A tester watching their own money sit still learns something about us that
# no amount of copy undoes. Turning this off restores the Approve/Deny card,
# which is the thing to do if a payout ever needs a human look.
POOL_AUTO_APPROVE_WITHDRAWALS: bool = True

# Macro headline context (RSS + webhook advisory layer).
MACRO_CONTEXT_ENABLED = True
MACRO_POLL_INTERVAL_SEC = 300  # 5 minutes
MACRO_MIN_SEVERITY_INJECT = 3
MACRO_PULSE_MIN_SEVERITY = 4
MACRO_WATCHDOG_GATE_MIN_SEVERITY = 4
MACRO_DEFAULT_TTL_HOURS = 24
MACRO_LLM_PROMOTE_THRESHOLD = 40  # keyword_score 0-100 before Haiku classify

# Hourly ETH price/volume z-score spike broadcasts.
ZMOVE_ENABLED = True
ZMOVE_INTERVAL_SEC = 300  # 5 minutes
ZMOVE_THRESHOLD = 2.0
ZMOVE_LOOKBACK_H = 168  # 1 week of hourly bars
ZMOVE_COOLDOWN_SEC = 2 * 60 * 60  # 2 hours per metric
ZMOVE_PRODUCT_ID = "ETH-USD"

# W1 ETH/BTC relative-strength bias injected into prompts and watchdog soft-gates.
RELATIVE_STRENGTH_ENABLED = True

# --- Republic Intelligence layer ---------------------------------------------
# Hourly BTC/ETH stance batch (H4/H1/M15) persisted + served on /api/v1.
INTELLIGENCE_ENABLED = True

# Phase 0 of deploy/INTEL_BOARD_PLAN.md. Every stance row records all three
# stances — det_stance (mechanical score), llm_stance (what the model wanted)
# and stance (what was published) — so the counterfactual ledger keeps
# accumulating no matter how the Phase 1 flags below are set. That is what
# lets Phase 0 and Phase 1 run at the same time rather than in sequence.
STANCE_LOG_COUNTERFACTUAL = True

# Phase 1. Publish the deterministic score and demote the LLM to narrative
# (rationale, medium summary, BTC->ETH note), discarding its overrides.
#
# The evidence: on the 523 recorded calls where the LLM departed from the
# deterministic score pasted into its own prompt, it was worse by 19-128 bps
# at 24h with the day-clustered CI excluding zero in 15 of 18 cells, in *both*
# regime halves — so not a trending-tape artifact. All three override kinds
# measured negative (muted n=310, invented n=205, flipped n=8), so there is no
# subset we have evidence is worth keeping. See
# trade_ideas/analysis/EVA proof of concept/VISION_ACCURACY.md.
#
# This does NOT make the board accurate. The deterministic score is momentum;
# own-horizon accuracy should stay near a coin flip. It removes a measured
# cost. Accuracy is what Phase 2's conditional read exists to attempt.
#
# Blast radius: ~9% of H1 rows change (LLM/deterministic agreement on H1 is
# 91%), and H1 is the row the mill resolves idea *direction* from and the
# Kalshi gate is 82% concordant with. Baselines to judge against:
# trade_ideas/analysis/mill_baseline.json.
STANCE_PUBLISH_DETERMINISTIC = True

# Weaker alternative, kept for the case where the board's overrides are later
# shown to carry value on some subset: let an override stand if it cites a
# price. Redundant while the flag above is on (that one reverts everything),
# and it adds an untested dependency on the model complying with a new prompt
# rule, which is why it is not the shipped choice.
STANCE_OVERRIDE_REQUIRE_EVIDENCE = False

# Policy epoch, same role as EVA_EXPERIMENT_EPOCH: the moment the published
# stance stopped being the LLM's. Any analysis of the board must segment on
# this, because rows either side of it were produced by different policies.
# Reverting means setting STANCE_PUBLISH_DETERMINISTIC=False and stamping a
# new epoch — never silently, or the book becomes unreadable.
STANCE_POLICY_EPOCH = "2026-09-17T16:30:00Z"

# Phase 2: the conditional ICT read (intelligence/conditional.py).
# A SHADOW artifact — written to `intel_reads` every cycle and consumed by
# nobody. The scalar board keeps running untouched as the control, so this
# carries no risk to the mill or the Kalshi gate and needs no migration.
# Costs one extra fast-model call per cycle. Detection is done by the existing
# detectors; the model only selects among candidates by index, so it cannot
# emit a price no detector found.
INTEL_CONDITIONAL_READS_ENABLED = True
# H4 and H1 only. M15 measured worst on every horizon in the accuracy study
# (<= null everywhere, negative at 2-4h) and 92% identical to the sign of the
# trailing 4h return, so it would add cost and noise without a hypothesis.
INTEL_CONDITIONAL_TIMEFRAMES: tuple[str, ...] = ("H4", "H1")
# When True, gate high-quality (abstention-first ICT) trade cards to the
# internal allowlist (config.INTERNAL_TELEGRAM_IDS). False = HQ cards go to
# all public subscribers with Accept/Reject and a "High Quality" label.
HQ_IDEAS_INTERNAL_ONLY = False

# Perp funding regime tracker (Binance public funding prints for BTC/ETH).
FUNDING_ENABLED = True
FUNDING_INTERVAL_SEC = 3600  # refresh once per hour (prints land every 8h)
FUNDING_PRODUCTS: dict[str, str] = {
    "BTC-USD": "BTCUSDT",
    "ETH-USD": "ETHUSDT",
}
# A persistence regime requires this many consecutive same-sign prints (8h
# prints -> 9 periods = 3 days).
FUNDING_PERSIST_PERIODS = 9
# A switch only counts once the new sign holds for this many prints; anything
# flippier than that is chop/noise.
FUNDING_SWITCH_CONFIRM_PERIODS = 3

# Long-horizon (4-year cycle) thesis: refreshed daily.
LONG_THESIS_ENABLED = True
LONG_THESIS_INTERVAL_SEC = 24 * 3600

# Once-daily performance digest ("you'd be up X%" + winner breakdown),
# posted as an X thread and mirrored to Telegram subscribers. X posting
# additionally requires TWITTER_ENABLED + keys in .env.
# Source is the mill volume paper book (ideas subscribers actually follow),
# not Eva HQ paper. MILL_PAPER_EPOCH_START cuts off the pre-restart mill.
DAILY_PERFORMANCE_POST_ENABLED = True
DAILY_DIGEST_HOUR_UTC = 21  # 21:00 UTC ≈ 5pm ET
DAILY_DIGEST_SOURCE = "mill"
# Re-based 2026-09-12 with the mill's bracket change (stop 1.5x -> 3.0x ATR,
# TP1 1.5R -> 0.375R). The two geometries have completely different hit rates
# (37.4% vs a measured 71.3%), so averaging them produces a digest number that
# describes neither. Move this whenever the bracket moves.
# Cut at the restart itself, not at midnight: ten ideas were minted on the old
# bracket earlier the same day. The filter is a lexicographic >= on an ISO
# opened_at, so a full timestamp works wherever a date does.
MILL_PAPER_EPOCH_START = "2026-09-12T17:36:00Z"  # UTC; volume paper opened_at >= this
# The dashboard's live-clip P&L window starts at the same bracket re-base
# (git 0343548). Trades opened under the old 1.5R bracket stay in the ledger
# and in the Trading Log list; they just no longer set the headline number,
# because a book that changed its geometry mid-flight has two track records.
MILL_LIVE_EPOCH_START = MILL_PAPER_EPOCH_START

# Auto-fill loss cooldown (2026-09-16). On 09-11 the sleeve refilled 19 times
# into one trending day (−$44 of the live book's −$76 total): every close freed
# the slot and the sweep immediately bought the next idea in the same hostile
# tape. After N consecutive losing AUTO clips on the same (product, side), the
# auto path sits out that (product, side) for COOLDOWN minutes; other products
# and the other side stay eligible, so the sweep "thinks about other ideas"
# rather than going quiet. Manual Accepts are never blocked.
# Backtest on the recorded live book (analysis/_q0916_cooldown_bt.py): the
# 3×3 sweep over N∈{2,3,4} × cooldown∈{2h,4h,6h} avoids net losses in every
# cell and monotonically; N=3 / 4h would have avoided −$23.47 of the −$76.40
# (net of the 2 winners it also blocks). n=76, so treat as structural damage
# control, not tuned alpha.
LIVE_MILL_LOSS_COOLDOWN_ENABLED = True
LIVE_MILL_LOSS_COOLDOWN_N = 3          # consecutive losses on one (product, side)
LIVE_MILL_LOSS_COOLDOWN_MIN = 240      # minutes the auto path sits out


def qty_caps(product_id: str) -> tuple[float, float]:
    """Return (min_qty, max_qty) for a product; fall back to ETH caps."""
    return PRODUCT_QTY_CAPS.get(product_id, PRODUCT_QTY_CAPS["ETH-USD"])


def ladder_unit(product_id: str) -> float | None:
    """Indivisible trade unit for a product — one CDE nano contract.

    Shapes the scale-out ladder in both books, so it is not a live-only number
    despite living beside the live floors. ``None`` for a product the venue
    does not list, which means a ladder over it can split freely.
    """
    return LIVE_PRODUCT_QTY_FLOORS.get(product_id)


def ob_min_width_pct(product_id: str | None = None) -> float:
    """HTF OB/breaker minimum width (% of mid) for a product."""
    if not product_id:
        return OB_MIN_WIDTH_PCT
    return float(
        PRODUCT_OB_MIN_WIDTH_PCT.get(product_id, OB_MIN_WIDTH_PCT)
    )


def product_label(product_id: str) -> str:
    """Short asset label for UI copy (ETH, BTC, …)."""
    if product_id.endswith("-USD"):
        return product_id[: -len("-USD")]
    if "/" in product_id:
        return product_id
    return product_id


def watchdog_execute_enabled() -> bool:
    """Effective watchdog paper-execution flag (config default + runtime meta override)."""
    try:
        import user_books

        raw = user_books.get_meta(WATCHDOG_EXECUTE_META_KEY)
    except Exception:
        raw = None
    if raw is None or str(raw).strip() == "":
        return bool(WATCHDOG_EXECUTE_ENABLED)
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def set_watchdog_execute_enabled(enabled: bool) -> bool:
    """Persist runtime override for watchdog paper execution. Returns new value."""
    import user_books

    user_books.set_meta(WATCHDOG_EXECUTE_META_KEY, "1" if enabled else "0")
    return enabled


def watchdog_live_enabled() -> bool:
    """Watchdog LIVE execution gate — separate from paper execute, and only
    meaningful when config.EXECUTION_MODE is shadow|live."""
    try:
        import user_books

        raw = user_books.get_meta(WATCHDOG_LIVE_META_KEY)
    except Exception:
        raw = None
    if raw is None or str(raw).strip() == "":
        return bool(WATCHDOG_LIVE_ENABLED)
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def set_watchdog_live_enabled(enabled: bool) -> bool:
    """Persist runtime override for watchdog LIVE execution."""
    import user_books

    user_books.set_meta(WATCHDOG_LIVE_META_KEY, "1" if enabled else "0")
    return enabled