# Project state

> Single source of truth for architecture and status of the Telegram trading bot.
> See **Documentation maintenance** below — update this file (and related deploy docs) whenever behaviour changes.

**Last updated:** 2026-07-19

---

## Documentation maintenance

When you implement a change that affects architecture, runtime behaviour, config flags, deployment, or ops workflows, **update the relevant docs in the same PR/commit** — do not leave them stale.

| If you changed… | Update… |
|---|---|
| Agent flow, validation, audit, watchdog, chat, persistence, dashboard | `deploy/PROJECT_STATE.md` — diagrams, status table, config table, changelog |
| VPS setup, systemd, `.env`, subscriber onboarding, dashboard HTTPS, deploy scripts | `deploy/CLOUD.md` |
| New/changed deploy script, service unit, or one-off ops tool | The script **and** a short note in `deploy/CLOUD.md` (and changelog here if architectural) |
| `bot_config.py` tunables | Section 9 below **and** any CLOUD.md mention of that flag |

**Checklist before merging:**

- [ ] Status table and changelog reflect the change
- [ ] Mermaid diagrams still match the code path (hourly, watchdog, chat, persistence)
- [ ] Config defaults in section 9 match `bot_config.py`
- [ ] CLOUD.md updated if deploy or server ops steps changed

Related deploy docs: [`CLOUD.md`](CLOUD.md) · `setup.sh` · `update.sh` · `eth-agent.service` · `eth-dashboard.service`

---

## 1. What this system is

A Telegram bot that runs an hourly dual-asset LLM trading cycle and a sub-hourly dual-asset programmatic watchdog over Coinbase ETH-USD and BTC-USD data. W1 ETH/BTC relative strength biases asset selection and soft-gates watchdog entries. Every suggestion is validated and audited before broadcast, then applied to a **house/agent paper book** (public journal). Subscribers hold **separate personal demo accounts**; trade DMs include a decision chart plus Accept/Reject — only Accept (or a later missed-connection Join) puts that user's cash into the trade. Personal ledgers are on `/me` (Telegram magic link). State is persisted to SQLite and surfaced through a FastAPI dashboard and Telegram read-back.

Four operator-facing paths (hourly, watchdog, Telegram chat/inline UI, dashboard), one shared data/context layer, one house paper book, and per-user personal books.

---

## 2. Top-level architecture

```mermaid
flowchart TD
    subgraph ENTRY["main.py — Telegram bot + job queue"]
        TG[Telegram polling<br/>commands, chat + inline callbacks]
        HJ[hourly_job<br/>ETH-USD + BTC-USD every 3600s]
        WJ[watchdog_job<br/>ETH-USD + BTC-USD every 60–300s<br/>if WATCHDOG_ENABLED]
        MJ[macro_feed_job<br/>every 300s<br/>if MACRO_CONTEXT_ENABLED]
        ZJ[zmove_job<br/>every 300s<br/>if ZMOVE_ENABLED]
    end

    ENTRY --> DATA[research.py + build_market_context<br/>Coinbase ETH/BTC OHLC → per-product MarketContext]
    DATA --> RS[W1 ETH/BTC ratio<br/>relative-strength bias]
    MJ --> MACRO[macro/ RSS + webhook ingest<br/>keyword score → Haiku classify]
    MACRO --> STORE
    ZJ --> ZMOVE[zmove.py ETH H1 price/volume z-score]
    ZMOVE --> TG

    TG --> CHAT[Chat Q&A<br/>bot.py on_text]
    TG --> TGUI[Inline keyboard<br/>Open account · My Metrics · My book · Journal · Research]
    TG --> RESEARCH["/research grounded SFP studies<br/>ohlc vault + sfp_index"]
    HJ --> HOURLY[Hourly cycle<br/>agent.run_cycle]
    WJ --> WATCH[Watchdog<br/>no LLM, sub-hourly]
    DATA --> HOURLY
    DATA --> WATCH
    RS --> HOURLY
    RS --> WATCH

    CHAT --> STORE[(SQLite persistence)]
    TGUI --> STORE
    RESEARCH --> OHLC[(ohlc.db candles + sfp_events)]
    HOURLY --> STORE
    WATCH --> STORE
    STORE --> READ[FastAPI dashboard +<br/>Telegram read-back]
```

---

## 3. Data + market context layer

`research.py` pulls ETH-USD and BTC-USD feeds from Coinbase; `patterns/market_context.py` → `build_market_context` assembles one `MarketContext` per product. `patterns/relative_strength.py` aligns weekly ETH and BTC bars into an ETH/BTC ratio, detects nearby W1 zones/SFPs, and infers `eth_strong`, `btc_strong`, or `neutral`. The hourly proposal receives that context as an asset preference; the watchdog rejects only entries that clearly fight it (long weaker asset or short stronger asset), so it remains a soft gate rather than a standalone signal.

Live strategy timeframes: **H4 → H1 → M5**. H12 remains available for research/historical studies only.

```mermaid
flowchart TD
    API[Coinbase OHLC API] --> R1[ETH + BTC H4 / H1 / M5 native]
    API --> R2[H12 resample from paginated H1<br/>research only]
    API --> R3[Daily bars for key levels]
    API --> R4[Live ETH + BTC spots<br/>watchdog, paper MTM + dashboard]

    R1 --> MC[MarketContext]
    R3 --> MC
    API --> RS[ETH/BTC W1 ratio<br/>relative-strength context]

    MC --> MC1[compute_range_24h on H1 + range state]
    MC --> MC2[detect_sfps H4 + M5]
    MC --> MC3[detect_htf_zones + resolve_zones on H4]
    MC --> MC4[find_order_blocks M5]
    MC --> MC5[update_bearish_retest_state]
    MC --> MC6[compute_key_levels / nearest levels]
    MC --> MC7[summary_text + alerts + setup_tags]
```

---

## 4. Hourly vision cycle (`agent.run_cycle`)

The LLM path builds both products plus W1 ETH/BTC context in one proposal call, then validates, refines, persists, and broadcasts each actionable product independently.

```mermaid
flowchart TD
    D1[ETH + BTC timeframes + daily bars] --> CTX[build MarketContext per product]
    CTX --> CH1[render marked ETH + BTC<br/>H4/H1/M5 charts]
    D1 --> RS[build + render W1 ETH/BTC ratio]
    CH1 --> PT[propose_trades_multi — Claude<br/>both assets + ratio + contexts]
    RS --> PT

    PT --> V1{_validate in analyze.py}
    V1 -->|trade| V1a[M5 OB + fib zone match]
    V1a --> V1b[validate_trade_risk — validate.py<br/>stop dist, R/R, sizing]
    V1 -->|no_trade / parse_error| S0[Suggestion]
    V1b -->|pass/fail| S0

    S0 --> REF[refine_suggestion — critic.py<br/>pre-broadcast audit loop]
    REF --> FD[verify_deterministic]
    REF --> FL[verify_llm critic<br/>if RUN_LLM_CRITIC_PRE_BROADCAST]
    FD --> OK{findings require refine?}
    FL --> OK
    OK -->|yes, passes left| RETRY[propose_trade retry<br/>with audit_feedback]
    RETRY[propose_trade single-product retry<br/>with audit_feedback] --> REF
    OK -->|exhausted passes| DOWN[downgrade / sanitize → no_trade]
    OK -->|no| CR[compose_rationale<br/>thesis then Market context]
    DOWN --> CR

    CR --> OC[build_output_charts]
    OC --> LG[ledger.append + paper.update]
    LG --> SNAP[audit.save_snapshot]
    SNAP --> MON[audit_hourly_cycle<br/>monitor verdict, run_llm=False]
    MON --> MRPT[send_hourly_monitor_report]
    LG --> BC{BROADCAST_ONLY_TRADES<br/>and no_trade?}
    BC -->|skip| SKIP[no subscriber DM]
    BC -->|send| OFFER[user_books.create_trade_offer]
    OFFER --> BDM[notify.broadcast<br/>decision chart + Accept/Reject]
```

---

## 5. Watchdog (`watchdog.run_watchdog`) — no LLM, sub-hourly

```mermaid
flowchart TD
    WD1[Loop ETH + BTC timeframes + live spot<br/>apply_live_spot_to_bars on M5] --> CTX2[build MarketContext per product<br/>spot_override]
    RS2[Build W1 ETH/BTC bias once] --> RSG{relative-strength<br/>soft gate}
    CTX2 --> TRG[evaluate_triggers]
    TRG --> T1[short_trigger_retest]
    TRG --> T2[m5_sfp_close on latest bar]
    TRG --> T3[m5_ob_fib tranches 0.25 / 0.50 + 0.718 add]
    TRG --> T4[m5_sfp_sweep_reversal]

    T1 --> RSG
    T2 --> RSG
    T3 --> RSG
    RSG --> CD{product-prefixed cooldown?}
    CD -->|active| WSKIP[skip trigger]
    CD -->|ok| BS[build_suggestion programmatic]

    BS --> V2[validate_suggestion<br/>same M5 OB + fib + trade risk]
    V2 --> WCH[render output charts]
    WCH --> WLG[ledger + paper.update]
    WLG --> WBD[notify.broadcast / broadcast_text]
    WLG --> WAL[send_watchdog_monitor_alert]
```

---

## 6. Telegram chat + inline UI (`bot.py`, `telegram_ui.py`)

```mermaid
flowchart TD
    TG[Telegram message or /start] --> KB[Inline keyboard<br/>Open account · My Metrics · My book · Journal · Research · Refresh]
    TG --> QA[chat.answer — Claude<br/>ledger + audit snapshot context]
    QA --> CAR[refine_chat_reply<br/>audit_text + sanitize on critical codes]
    CAR --> CAI[audit.log_chat_audit]
    CAR --> AL{verdict.has_issues?}
    AL -->|yes| MA[send_monitor_alert]
    AL -->|no| REPLY[reply + PnL footer]
    MA --> REPLY
    KB --> OPEN[Open account<br/>$500 / $1000 / $2500 menu]
    KB --> METRICS[My Metrics<br/>personal equity + PnL]
    KB --> ME[My book<br/>magic link to /me]
    KB --> PORT[Agent journal<br/>DASHBOARD_PUBLIC_URL]
    KB --> RESEARCH[Research help/catalog]
    OPEN --> ACCT[(user_accounts)]
    METRICS --> ACCT
    TG --> TRADE[trade:yes / trade:no / trade:join]
    TRADE --> UPOS[(user_positions)]
```

`Open account` creates a one-time personal demo book (not real funding). Legacy Funders are migrated to a **$1,000** personal account via `user_books.migrate_funders_to_personal_accounts` (also on `paper.init_db`). The house/agent book in `paper.py` continues to auto-take every validated trade for the public journal; user cash never mixes into house equity. Trade broadcasts include a decision chart (green TP1 / red SL rectangles) and Accept/Reject (15 min window). Rejected/expired users may get one missed-connection Join invite when the house position is ≥ +0.5R.

---

## 7. Persistence + read consumers

```mermaid
flowchart LR
    subgraph STORE["SQLite persistence"]
        DB1[(ledger)]
        DB2[(paper)]
        DB3[(audit_snapshots)]
        DB4[(audit_verdicts + chart-read score)]
        DB5[(chat_audits)]
        DB6[(paper_contributions — legacy / house seed)]
        DB7[(user_accounts + user_positions + trade_offers + trade_decisions)]
    end

    STORE --> DASH[FastAPI dashboard / + /me]
    STORE --> TG[Telegram read-back]
    PNG[charts/ PNGs<br/>structure entry outcome] --> DASH
    PNG --> TG
```

Writers → stores:

| Store | Written by | Read by |
|---|---|---|
| `ledger` | hourly cycle, watchdog | dashboard, Telegram |
| `paper` | hourly cycle, watchdog | dashboard, Telegram |
| `paper_contributions` | House seed; legacy Fund rows (migration source) | Migrate script; house seed |
| `user_accounts` / `user_positions` / `user_trades` | Open account; Accept / late-join | Telegram My Metrics; `/me` |
| `trade_offers` / `trade_decisions` | Hourly + watchdog after house `paper.update` | Accept/Reject; participation strip; missed-connection |
| `audit_snapshots` | hourly cycle | dashboard, chat, monitor |
| `audit_verdicts` | hourly monitor, chat audit | dashboard |
| `chat_audits` | chat Q&A | — |
| `charts/` PNGs | hourly/watchdog output charts; `paper` close → `{cycle}_H4|M5_outcome.png` | dashboard `/api/chart`, Telegram |

---

## 8. Component status

Legend: ✅ done · 🟡 in progress · 🔧 needs work · ⬜ planned · ⚠️ known issue

| Component | File(s) | Status | Notes |
|---|---|---|---|
| Coinbase OHLC ingest | `research.py` | ✅ | H4/H1/M5 live; H12 resample research-only; daily; live spot |
| Market context | `patterns/market_context.py`, `patterns/relative_strength.py` | ✅ | per-product alerts/tags/summary plus W1 ETH/BTC bias |
| SFP detection | `patterns/sfp.py` | ✅ | H4 + M5 (live); H12 still used in research |
| HTF zones | `patterns/htf_structure.py`, `patterns/zone_resolver.py` | 🟡 | detect_zones on H4; resolve_zones tuning |
| Order blocks | `patterns/order_block.py` | 🟡 | M5 OB + fib matching |
| 24h range | `patterns/range_24h.py` | ✅ | computed on H1 bars |
| Bearish retest state | `patterns/setup_state.py` | ✅ | |
| Hourly cycle | `agent.py` | ✅ | dual ETH/BTC contexts, charts, per-product persistence/broadcast |
| Trade proposal (LLM) | `analyze.py` | ✅ | one 0–2 trade multi-asset call; single-product critic retries |
| Trade risk validation | `validate.py` | ✅ | stop dist, R/R, USD-notional sizing |
| Refine / critic loop | `critic.py` | ✅ | pre-broadcast retries; context-conflict ack; thesis + Market context compose; post-cycle monitor |
| Watchdog | `watchdog.py` | ✅ | loops ETH/BTC; one fire/product/tick; product cooldown; macro + ETH/BTC soft gates |
| Macro context | `macro/` | ✅ | RSS poll, webhook ingest, keyword→Haiku classify, pulse, dashboard |
| OHLC history vault | `ohlc_cache.py`, `backfill.py` | ✅ | ETH+BTC H1/D1 cache; W1/H12 derived; `--product` CLI |
| SFP pattern index | `patterns/sfp_index.py` | ✅ | deterministic `sfp_events` in ohlc.db; rebuild on backfill/study |
| Chat Q&A | `bot.py`, `chat.py` | ✅ | snapshot-grounded + chat audit |
| Telegram research | `research_reports/`, `metrics/`, `analytics.py` | ✅ | `/research` catalog; d1/w1/h12 SFP + invalidations; ETH/BTC |
| Z-Move alerts | `zmove.py` | ✅ | ETH H1 \|z\|≥2 price/volume → subscriber broadcast + cooldown |
| Persistence | `ledger.py`, `audit.py`, `paper.py`, `user_books.py` | ✅ | SQLite |
| Paper trading | `paper.py` | ✅ | house multi-asset book; fixed 25% deploy; qty caps; FIFO; staged TP scale-out; outcome charts |
| Personal books | `user_books.py` | ✅ | open-account sizes; offers; Accept/Reject/expire; late-join; user SL/TP; `/me` tokens |
| Telegram beta UI | `bot.py`, `telegram_ui.py` | ✅ | Open account / My Metrics / My book / Journal / Research; trade Yes/No/Join |
| Decision chart | `charts.build_decision_chart` | ✅ | clean candles + red SL / green TP1 bands; first broadcast image |
| Dashboard | `dashboard/` | ✅ | public agent journal + participation aggregates; `/me` personal ledger |
| Live execution | `execute.py` | ⬜ | shadow/live path not built |
| OHLC history cache | `ohlc_cache.py` | ✅ | research/backfill; ETH+BTC H1/D1; not hot path |
| Legacy scheduler | `scheduler.py` | ⚠️ | deprecated; use `main.py` |

---

## 9. Feature flags / config

Defaults from `bot_config.py` (non-secret tunables). Secrets and portfolio size live in `.env` — see `CLOUD.md`.

| Flag / setting | Default | Effect |
|---|---|---|
| `WATCHDOG_ENABLED` | `True` | enables sub-hourly watchdog job |
| `WATCHDOG_INTERVAL_SEC` | `60` | scan cadence (clamped 60–300s in `main.py`) |
| `WATCHDOG_COOLDOWN_SEC` | `1800` (30m) | suppress repeat fire on same M5 OB |
| `BROADCAST_ONLY_TRADES` | `True` | suppress `no_trade` subscriber DMs |
| `RUN_LLM_CRITIC_PRE_BROADCAST` | `True` | run LLM critic in refine loop |
| `MAX_REFINE_PASSES` | `3` | audit retry budget before downgrade |
| `MAX_OPEN_TRADES` | `20` | paper FIFO cap |
| `ENTRY_FIB_LOW` / `ENTRY_FIB_HIGH` | `0.25` / `0.50` | M5 OB entry band; watchdog tranches at each level |
| `ADD_FIB_LEVEL` | `0.718` | scale-in adds another `TRADE_DEPLOY_PCT` (1.25× max base exposure) |
| `ENTRY_TRANCHE_DEPLOY_PCT` | `0.125` | per-tranche deploy (half of `TRADE_DEPLOY_PCT`) |
| `TRADE_DEPLOY_PCT` | `0.25` | fixed fraction of **live paper equity** deployed as notional per full idea (R/R unaffected) |
| `FIB_LEVEL_TOLERANCE_PCT` | `0.008` | looser "near" fib mark for M5 watchdog |
| `TRADED_PRODUCTS` | `("ETH-USD", "BTC-USD")` | products the hourly cycle and watchdog may trade concurrently |
| `PRODUCT_QTY_CAPS` | `{"ETH-USD": (0.25, 2.0), "BTC-USD": (0.005, 0.05)}` | per-product paper size guardrails used by `qty_caps(product_id)` |
| `MIN_ETH_QTY` / `MAX_ETH_QTY` | `0.25` / `2.0` | legacy aliases for the ETH entries in `PRODUCT_QTY_CAPS` |
| `RELATIVE_STRENGTH_ENABLED` | `True` | adds W1 ETH/BTC proposal bias and watchdog soft gate |
| `PAPER_CONTRIBUTION_USD` | `1000.0` | legacy default / migrate amount alias |
| `PAPER_ACCOUNT_SIZES` | `(500, 1000, 2500)` | Open account menu sizes |
| `PAPER_ACCOUNT_DEFAULT_USD` | `1000.0` | migrate amount for legacy Funders |
| `APPROVAL_WINDOW_MIN` | `15` | Accept window before pending → expired |
| `MISSED_CONNECTION_R` | `0.5` | house unrealized R to trigger late-join DM |
| `USER_MIN_DEPLOY_USD` | `25.0` | minimum notional to Accept / late-join |
| `HOUSE_CONTRIBUTION_TELEGRAM_ID` | `0` | reserved Telegram ID for the house seed row in `paper_contributions` |
| `OB_MIN_WIDTH_PCT` | `1.25` | default / ETH HTF (H4) OB zone width floor (% of mid price) |
| `PRODUCT_OB_MIN_WIDTH_PCT` | `{"ETH-USD": 1.25, "BTC-USD": 0.60}` | per-product HTF OB/breaker width via `ob_min_width_pct(product_id)` |
| `OB_MIN_WIDTH_PCT_M5` | `0.15` | minimum M5 entry OB width (M5 candles are ~10× thinner than H1) |
| `PAPER_EPOCH_LABEL` | `"5k_usd"` | dashboard epoch label |
| `MACRO_CONTEXT_ENABLED` | `True` | RSS poll + macro advisory injection |
| `MACRO_POLL_INTERVAL_SEC` | `300` | RSS poll cadence |
| `MACRO_MIN_SEVERITY_INJECT` | `3` | min LLM severity for prompt injection |
| `MACRO_PULSE_MIN_SEVERITY` | `4` | position-aware pulse + monitor alert |
| `MACRO_WATCHDOG_GATE_MIN_SEVERITY` | `4` | soft gate conflicting watchdog entries |
| `MACRO_LLM_PROMOTE_THRESHOLD` | `40` | min keyword_score before Haiku classify |
| `MACRO_DEFAULT_TTL_HOURS` | `24` | fallback TTL for classified events |
| `ZMOVE_ENABLED` | `True` | ETH H1 price/volume z-score subscriber alerts |
| `ZMOVE_INTERVAL_SEC` | `300` | z-move scan cadence |
| `ZMOVE_THRESHOLD` | `2.0` | \|z\| fire threshold |
| `ZMOVE_LOOKBACK_H` | `168` | hourly lookback for mean/std |
| `ZMOVE_COOLDOWN_SEC` | `7200` | per-metric suppress window after fire |
| `ZMOVE_PRODUCT_ID` | `"ETH-USD"` | product scanned for z-moves |
| hourly interval | `3600s` | `hourly_job` cadence in `main.py` |

---

## 10. Known issues / open questions

- [ ] Live execution path (`execute.py`, `EXECUTION_MODE=shadow|live`) not implemented — paper only
- [ ] HTF zone / M5 OB resolver edge cases under active tuning

---

## 11. Changelog

| Date | Change |
|---|---|
| 2026-07-19 | History vault + grounded SFP Q&A: multi-product `ohlc_cache` (ETH/BTC H1/D1), `sfp_events` index, `/research d1_sfps` + `w1_invalidations`, clarify/refuse for unindexed patterns; ETH Z-Move broadcasts (`\|z\|≥2` price/volume, 168h lookback, 2h cooldown). |
| 2026-07-19 | Opt-in personal books: Open account menu ($500/$1k/$2.5k); house book stays public journal; Accept/Reject (15m) + decision chart; `/me` magic-link ledger; missed-connection Join at +0.5R; migrate legacy Funders to $1k accounts. |
| 2026-07-16 | Per-product HTF OB min-width: BTC uses 0.60% (ETH stays 1.25%) so BTC H4 OB/breaker boxes are not over-filtered by ETH-tuned volatility. |
| 2026-07-16 | Dashboard H4 structure section shows ETH and BTC marked charts side by side; hourly cycle always persists a per-product decision so both charts stay available. |
| 2026-07-16 | Trading Guide sizing section aligned to USD-notional contract; added `tests/test_relative_strength.py` (W1 ratio/soft-gate) and `tests/test_contributions.py` (Fund/My Metrics). |
| 2026-07-16 | Sizing contract switched to USD notional: `Suggestion.size` now stores deployed dollars, paper converts to ETH/BTC qty for P&L, and dashboard/Telegram show dollar size first with quantity secondary. |
| 2026-07-16 | Beta operator surfaces completed for dual-asset paper contributions: Telegram inline Fund/My Metrics/Portfolio/Research UX; dashboard ETH+BTC spots, asset labels, API pagination, and chart-read score tooltips; deployment/onboarding docs updated for public dashboard links and open beta access. |
| 2026-07-16 | Dual-asset runtime path: hourly Claude call analyzes ETH + BTC with W1 ETH/BTC preference, then refines/persists each product separately; watchdog loops both products with relative-strength soft gates and product-specific cooldowns. |
| 2026-07-16 | Paper multi-asset + contributions: `product_id`/`qty` on positions/trades (with `eth_qty` backcompat), spots-dict MTM, `qty_caps(product_id)`, `paper_contributions` + `fund_user` / `get_user_metrics`. |
| 2026-07-16 | Broadcast UX: thesis first (“Why this trade”), programmatic alerts relabeled **Market context** below. Hourly refine requires `CONTEXT_CONFLICT_UNACKNOWLEDGED` acknowledgment when action opposes context (opposite M5 OB / opposite-only primary H4); watchdog skipped. |
| 2026-07-14 | Dashboard chart lightbox: click thumbs / H4 / M5 charts to enlarge (Esc / backdrop / × to close). |
| 2026-07-14 | Dashboard tag tooltips filled from Trading Guide (ranging, H4/M5 SFP, M5 OB fib, macro gates); macro feed widened to 640px. |
| 2026-07-14 | Dashboard journal layout fix: trade summary button is the flex row (avoids nested-flex-in-button bugs), fixed `.trade-thumb-wrap` frames, full-width cards, cache-busted CSS. |
| 2026-07-14 | Dashboard UX polish: macro feed is a ~480px square with internal scroll; trade thumbs/expanded charts use fixed frames; journal headers left-aligned; expand keeps one continuous card background; more gap between trade cards. |
| 2026-07-14 | Dashboard **trade journal**: expandable open/closed/archived cards with dual H4 structure + M5 execution charts, levels (Entry/SL/TP/OB), P&L, and rationale. `/api/chart/{cycle}?kind=&tf=` serves structure/entry/outcome/marked; paper closes best-effort write `{cycle}_H4|M5_outcome.png` (Entry+Exit+P&L windowed to open→close). |
| 2026-07-14 | Fixed paper/watchdog scale-in bug that stacked many same-side M5 OB fills into one position (cash→0, ~2.6 ETH) and **reset SL to the latest fill**. Adds now only merge on matching `order_block_ref`, never widen SL, cap qty at `MAX_ETH_QTY`; watchdog blocks competing OB fib entries while one same-side OB position is open. |
| 2026-07-13 | M5 entry OB min width lowered via `OB_MIN_WIDTH_PCT_M5=0.15` (HTF stays `OB_MIN_WIDTH_PCT=1.25`). Live probe showed 59/59 M5 OB candidates rejected at 1.25% (widths ~0.05–0.47%). |
| 2026-07-13 | Removed HTF alignment hard-gate from watchdog (`_htf_allows_long/short`). Entries fire on M5 OB fib / SFP triggers; H4 zones remain context only. Softened market_context / Trading Guide / analyze prompts so HTF conflict no longer defaults to no_trade. |
| 2026-07-13 | Live stack **H4→H1→M5** wired through agent/analyze/charts/watchdog/critic/audit/dashboard/chat. Watchdog tags `m5_ob_*_in_fib`, triggers `m5_ob_fib_*` / `m5_sfp_*`; critic codes `M5_OB_MISLABEL` / `JSON_H4_AS_M5_OB`. Fib band 0.25–0.50 unchanged; `WATCHDOG_INTERVAL_SEC=60`, cooldown 30m, `FIB_LEVEL_TOLERANCE_PCT=0.008`. H12 research topics unchanged. |
| 2026-07-09 | `/research h12_invalidations` — last N H12 SFP invalidations with post-invalidation continuation vs mean-reversion stats + chart |
| 2026-07-09 | Expanded `/research`: topic catalog, standardized reports, market snapshot topics (digest, macro, funding, volume, dominance, miner), SFP studies via shared `ResearchReport` format |
| 2026-07-09 | Watchdog staged fib entries (12.5% @ 0.25 + 12.5% @ 0.50), 0.718 scale-in (+25%), and `h1_sfp_sweep_reversal` with stop at swept level. Entry band changed from 0.618–0.786 to 0.25–0.50 across guide, validation, and charts. |
| 2026-07-08 | Position sizing switched from 1% risk-based to fixed-fraction deployment (`TRADE_DEPLOY_PCT=0.25` of live paper equity); removed risk-capacity feasibility gate; `MAX_ETH_QTY` raised to `2.0`. R/R, stop, and TP logic unchanged. |
| 2026-07-07 | OB minimum width filter (`OB_MIN_WIDTH_PCT=1.25`): H1 + H12 detection and analyze validation |
| 2026-07-07 | Macro headline layer: RSS poll, webhook ingest, keyword→Haiku classify, pulse advisories, watchdog soft gates, dashboard macro monitor |
| 2026-07-07 | Added documentation maintenance section; filled config defaults; aligned diagrams with audit loops, watchdog, and chat path |
| 2026-07-07 | Initial project state document created |
