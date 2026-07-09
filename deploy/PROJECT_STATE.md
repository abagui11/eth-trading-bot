# Project state

> Single source of truth for architecture and status of the Telegram trading bot.
> See **Documentation maintenance** below — update this file (and related deploy docs) whenever behaviour changes.

**Last updated:** 2026-07-07

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

A Telegram bot that runs an hourly LLM-driven trading cycle and a sub-hourly programmatic watchdog over Coinbase OHLC data, validates and audits every suggestion before broadcast, paper-trades the results, and answers chat questions with ledger/audit context. All state is persisted to SQLite and surfaced through a FastAPI dashboard and Telegram read-back.

Three runtime paths (hourly, watchdog, chat), one shared data/context layer, shared persistence.

---

## 2. Top-level architecture

```mermaid
flowchart TD
    subgraph ENTRY["main.py — Telegram bot + job queue"]
        TG[Telegram polling<br/>bot.py handlers]
        HJ[hourly_job<br/>every 3600s]
        WJ[watchdog_job<br/>every 60–300s<br/>if WATCHDOG_ENABLED]
        MJ[macro_feed_job<br/>every 300s<br/>if MACRO_CONTEXT_ENABLED]
    end

    ENTRY --> DATA[research.py + build_market_context<br/>Coinbase OHLC → MarketContext]
    MJ --> MACRO[macro/ RSS + webhook ingest<br/>keyword score → Haiku classify]
    MACRO --> STORE

    TG --> CHAT[Chat Q&A<br/>bot.py on_text]
    HJ --> HOURLY[Hourly cycle<br/>agent.run_cycle]
    WJ --> WATCH[Watchdog<br/>no LLM, sub-hourly]
    DATA --> HOURLY
    DATA --> WATCH

    CHAT --> STORE[(SQLite persistence)]
    HOURLY --> STORE
    WATCH --> STORE
    STORE --> READ[FastAPI dashboard +<br/>Telegram read-back]
```

---

## 3. Data + market context layer

`research.py` pulls feeds from Coinbase; `patterns/market_context.py` → `build_market_context` assembles a `MarketContext` consumed by both the hourly cycle and the watchdog.

```mermaid
flowchart TD
    API[Coinbase OHLC API] --> R1[H12 resample from paginated H1]
    API --> R2[H4 / H1 native]
    API --> R3[Daily bars for key levels]
    API --> R4[Live spot ticker<br/>watchdog only]

    R1 --> MC[MarketContext]
    R2 --> MC
    R3 --> MC

    MC --> MC1[compute_range_24h + range state]
    MC --> MC2[detect_sfps H12 + H1]
    MC --> MC3[detect_htf_zones + resolve_zones]
    MC --> MC4[find_order_blocks H1]
    MC --> MC5[update_bearish_retest_state]
    MC --> MC6[compute_key_levels / nearest levels]
    MC --> MC7[summary_text + alerts + setup_tags]
```

---

## 4. Hourly vision cycle (`agent.run_cycle`)

The LLM path: render charts → propose → validate → refine loop → compose → persist → broadcast → monitor audit.

```mermaid
flowchart TD
    D1[get_all_timeframes + daily bars] --> CTX[build_market_context]
    CTX --> CH1[render_marked_charts<br/>H12/H4/H1 PNGs with overlays]
    CH1 --> PT[propose_trade — Claude<br/>charts + Trading Guide + market_context]

    PT --> V1{_validate in analyze.py}
    V1 -->|trade| V1a[H1 OB + fib zone match]
    V1a --> V1b[validate_trade_risk — validate.py<br/>stop dist, R/R, sizing]
    V1 -->|no_trade / parse_error| S0[Suggestion]
    V1b -->|pass/fail| S0

    S0 --> REF[refine_suggestion — critic.py<br/>pre-broadcast audit loop]
    REF --> FD[verify_deterministic]
    REF --> FL[verify_llm critic<br/>if RUN_LLM_CRITIC_PRE_BROADCAST]
    FD --> OK{findings require refine?}
    FL --> OK
    OK -->|yes, passes left| RETRY[propose_trade retry<br/>with audit_feedback]
    RETRY --> REF
    OK -->|exhausted passes| DOWN[downgrade / sanitize → no_trade]
    OK -->|no| CR[compose_rationale<br/>llm_body + Signals block]
    DOWN --> CR

    CR --> OC[build_output_charts]
    OC --> LG[ledger.append + paper.update]
    LG --> SNAP[audit.save_snapshot]
    SNAP --> MON[audit_hourly_cycle<br/>monitor verdict, run_llm=False]
    MON --> MRPT[send_hourly_monitor_report]
    LG --> BC{BROADCAST_ONLY_TRADES<br/>and no_trade?}
    BC -->|skip| SKIP[no subscriber DM]
    BC -->|send| BDM[notify.broadcast]
```

---

## 5. Watchdog (`watchdog.run_watchdog`) — no LLM, sub-hourly

```mermaid
flowchart TD
    WD1[get_all_timeframes + live spot<br/>apply_live_spot_to_h1] --> CTX2[build_market_context<br/>spot_override]
    CTX2 --> TRG[evaluate_triggers]
    TRG --> T1[short_trigger_retest]
    TRG --> T2[h1_sfp_close on latest bar]
    TRG --> T3[h1_ob_fib tranches 0.25 / 0.50 + 0.718 add]
    TRG --> T4[h1_sfp_sweep_reversal]

    T1 --> CD{cooldown?}
    T2 --> CD
    T3 --> CD
    CD -->|active| WSKIP[skip trigger]
    CD -->|ok| BS[build_suggestion programmatic]

    BS --> V2[validate_suggestion<br/>same H1 OB + fib + trade risk]
    V2 --> WCH[render output charts]
    WCH --> WLG[ledger + paper.update]
    WLG --> WBD[notify.broadcast / broadcast_text]
    WLG --> WAL[send_watchdog_monitor_alert]
```

---

## 6. Chat Q&A (`bot.py on_text`)

```mermaid
flowchart TD
    TG[Telegram message] --> QA[chat.answer — Claude<br/>ledger + audit snapshot context]
    QA --> CAR[refine_chat_reply<br/>audit_text + sanitize on critical codes]
    CAR --> CAI[audit.log_chat_audit]
    CAR --> AL{verdict.has_issues?}
    AL -->|yes| MA[send_monitor_alert]
    AL -->|no| REPLY[reply + PnL footer]
    MA --> REPLY
```

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
    end

    STORE --> DASH[FastAPI dashboard]
    STORE --> TG[Telegram read-back]
```

Writers → stores:

| Store | Written by | Read by |
|---|---|---|
| `ledger` | hourly cycle, watchdog | dashboard, Telegram |
| `paper` | hourly cycle, watchdog | dashboard, Telegram |
| `audit_snapshots` | hourly cycle | dashboard, chat, monitor |
| `audit_verdicts` | hourly monitor, chat audit | dashboard |
| `chat_audits` | chat Q&A | — |

---

## 8. Component status

Legend: ✅ done · 🟡 in progress · 🔧 needs work · ⬜ planned · ⚠️ known issue

| Component | File(s) | Status | Notes |
|---|---|---|---|
| Coinbase OHLC ingest | `research.py` | ✅ | H12 resample, H4/H1 native, daily, live spot |
| Market context | `patterns/market_context.py` | ✅ | alerts, setup_tags, summary_text |
| SFP detection | `patterns/sfp.py` | ✅ | H12 + H1 |
| HTF zones | `patterns/htf_structure.py`, `patterns/zone_resolver.py` | 🟡 | resolve_zones tuning |
| Order blocks | `patterns/order_block.py` | 🟡 | H1 OB + fib matching |
| 24h range | `patterns/range_24h.py` | ✅ | |
| Bearish retest state | `patterns/setup_state.py` | ✅ | |
| Hourly cycle | `agent.py` | ✅ | |
| Trade proposal (LLM) | `analyze.py` | ✅ | JSON retry + `_validate` |
| Trade risk validation | `validate.py` | ✅ | stop dist, R/R, sizing |
| Refine / critic loop | `critic.py` | ✅ | pre-broadcast retries; post-cycle monitor |
| Watchdog | `watchdog.py` | ✅ | 3 triggers, cooldown, no LLM; macro soft gates |
| Macro context | `macro/` | ✅ | RSS poll, webhook ingest, keyword→Haiku classify, pulse, dashboard |
| Chat Q&A | `bot.py`, `chat.py` | ✅ | snapshot-grounded + chat audit |
| Telegram research | `research_reports/`, `metrics/`, `analytics.py` | ✅ | `/research` catalog; snapshot digests + SFP studies |
| Persistence | `ledger.py`, `audit.py`, `paper.py` | ✅ | SQLite |
| Dashboard | `dashboard/` | 🟡 | FastAPI read-only + macro news monitor |
| Paper trading | `paper.py` | ✅ | fixed 25% deploy sizing, FIFO cap, epoch archives |
| Live execution | `execute.py` | ⬜ | shadow/live path not built |
| OHLC history cache | `ohlc_cache.py` | ✅ | research/backfill only, not hot path |
| Legacy scheduler | `scheduler.py` | ⚠️ | deprecated; use `main.py` |

---

## 9. Feature flags / config

Defaults from `bot_config.py` (non-secret tunables). Secrets and portfolio size live in `.env` — see `CLOUD.md`.

| Flag / setting | Default | Effect |
|---|---|---|
| `WATCHDOG_ENABLED` | `True` | enables sub-hourly watchdog job |
| `WATCHDOG_INTERVAL_SEC` | `180` | scan cadence (clamped 60–300s in `main.py`) |
| `WATCHDOG_COOLDOWN_SEC` | `21600` (6h) | suppress repeat fire on same H1 OB |
| `BROADCAST_ONLY_TRADES` | `True` | suppress `no_trade` subscriber DMs |
| `RUN_LLM_CRITIC_PRE_BROADCAST` | `True` | run LLM critic in refine loop |
| `MAX_REFINE_PASSES` | `3` | audit retry budget before downgrade |
| `MAX_OPEN_TRADES` | `20` | paper FIFO cap |
| `ENTRY_FIB_LOW` / `ENTRY_FIB_HIGH` | `0.25` / `0.50` | H1 OB entry band; watchdog tranches at each level |
| `ADD_FIB_LEVEL` | `0.718` | scale-in adds another `TRADE_DEPLOY_PCT` (1.25× max base exposure) |
| `ENTRY_TRANCHE_DEPLOY_PCT` | `0.125` | per-tranche deploy (half of `TRADE_DEPLOY_PCT`) |
| `TRADE_DEPLOY_PCT` | `0.25` | fixed fraction of **live paper equity** deployed as notional per full idea (R/R unaffected) |
| `MIN_ETH_QTY` / `MAX_ETH_QTY` | `0.25` / `2.0` | paper size guardrails after fixed-fraction sizing |
| `OB_MIN_WIDTH_PCT` | `1.25` | minimum OB zone width (% of mid price; H1 rule, all TFs) |
| `PAPER_EPOCH_LABEL` | `"5k_usd"` | dashboard epoch label |
| `MACRO_CONTEXT_ENABLED` | `True` | RSS poll + macro advisory injection |
| `MACRO_POLL_INTERVAL_SEC` | `300` | RSS poll cadence |
| `MACRO_MIN_SEVERITY_INJECT` | `3` | min LLM severity for prompt injection |
| `MACRO_PULSE_MIN_SEVERITY` | `4` | position-aware pulse + monitor alert |
| `MACRO_WATCHDOG_GATE_MIN_SEVERITY` | `4` | soft gate conflicting watchdog entries |
| `MACRO_LLM_PROMOTE_THRESHOLD` | `40` | min keyword_score before Haiku classify |
| `MACRO_DEFAULT_TTL_HOURS` | `24` | fallback TTL for classified events |
| hourly interval | `3600s` | `hourly_job` cadence in `main.py` |

---

## 10. Known issues / open questions

- [ ] Live execution path (`execute.py`, `EXECUTION_MODE=shadow|live`) not implemented — paper only
- [ ] Inline approve/reject on Telegram broadcasts not implemented (`notify.py` TODO)
- [ ] HTF zone / H1 OB resolver edge cases under active tuning

---

## 11. Changelog

| Date | Change |
|---|---|
| 2026-07-09 | `/research h12_invalidations` — last N H12 SFP invalidations with post-invalidation continuation vs mean-reversion stats + chart |
| 2026-07-09 | Expanded `/research`: topic catalog, standardized reports, market snapshot topics (digest, macro, funding, volume, dominance, miner), SFP studies via shared `ResearchReport` format |
| 2026-07-09 | Watchdog staged fib entries (12.5% @ 0.25 + 12.5% @ 0.50), 0.718 scale-in (+25%), and `h1_sfp_sweep_reversal` with stop at swept level. Entry band changed from 0.618–0.786 to 0.25–0.50 across guide, validation, and charts. |
| 2026-07-08 | Position sizing switched from 1% risk-based to fixed-fraction deployment (`TRADE_DEPLOY_PCT=0.25` of live paper equity); removed risk-capacity feasibility gate; `MAX_ETH_QTY` raised to `2.0`. R/R, stop, and TP logic unchanged. |
| 2026-07-07 | OB minimum width filter (`OB_MIN_WIDTH_PCT=1.25`): H1 + H12 detection and analyze validation |
| 2026-07-07 | Macro headline layer: RSS poll, webhook ingest, keyword→Haiku classify, pulse advisories, watchdog soft gates, dashboard macro monitor |
| 2026-07-07 | Added documentation maintenance section; filled config defaults; aligned diagrams with audit loops, watchdog, and chat path |
| 2026-07-07 | Initial project state document created |
