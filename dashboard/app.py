"""FastAPI application — public read-only dashboard + personal /me ledger."""

from __future__ import annotations

import json
import secrets
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Header, Response
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

import audit
import bot_config
import config
import ledger
import live_ledger
import paper
import user_books
from dashboard import data
from dashboard.brain import get_brain_payload
from dashboard.yield_gen import get_yield_payload
from dashboard.charts import (
    VALID_KINDS,
    VALID_TFS,
    h4_marked_path,
    latest_marked_h4_path,
    resolve_chart_path,
    resolve_trade_chart,
    stance_chart_path,
)
from dashboard.formatting import (
    format_news_age,
    format_news_iso,
    format_news_when,
    format_pct,
    format_trade_date,
    format_trade_time,
    format_usd,
    news_source_label,
    tag_tooltip,
    trade_title,
)
from dashboard.intel_api import router as intel_router
from dashboard.investor import build_investor_payload
from intelligence import store as intel_store
from macro import store as macro_store
from macro.ingest import ingest_headline

_PKG_DIR = Path(__file__).resolve().parent
_ME_COOKIE = "me_session"
_INVESTOR_COOKIE = "investor_access"


class MacroIngestBody(BaseModel):
    title: str = Field(min_length=1)
    url: str | None = None
    summary: str | None = None
    source: str | None = None
    published_at: str | None = None
    force_classify: bool = False


class WatchdogExecuteBody(BaseModel):
    enabled: bool


class IdeaDecisionBody(BaseModel):
    decision: str


def _resolve_telegram_id(request: Request) -> int | None:
    token = request.query_params.get("t")
    if token:
        telegram_id = user_books.verify_me_token(token)
        if telegram_id is not None:
            return telegram_id
    cookie = request.cookies.get(_ME_COOKIE)
    if cookie:
        return user_books.verify_me_token(cookie)
    return None


def _investor_authorized(request: Request) -> bool:
    """Gate for the shareable investor link.

    The link is meant to be forwarded around, so the token travels in the URL
    once and then lives in a cookie. With no token configured the page stays
    reachable but unlisted, matching /volume, so a fresh deploy is not broken
    by a missing env var.
    """
    expected = config.INVESTOR_ACCESS_TOKEN
    if not expected:
        return True
    presented = request.query_params.get("k") or request.cookies.get(_INVESTOR_COOKIE)
    return bool(presented) and secrets.compare_digest(str(presented), expected)


def _set_investor_cookie(response: Response, request: Request) -> None:
    if not config.INVESTOR_ACCESS_TOKEN or not request.query_params.get("k"):
        return
    response.set_cookie(
        key=_INVESTOR_COOKIE,
        value=config.INVESTOR_ACCESS_TOKEN,
        httponly=True,
        max_age=config.INVESTOR_SESSION_TTL_SEC,
        samesite="lax",
    )


def _set_session_cookie(response: Response, request: Request, telegram_id: int) -> None:
    token = request.query_params.get("t")
    if not token or user_books.verify_me_token(token) != telegram_id:
        return
    response.set_cookie(
        key=_ME_COOKIE,
        value=user_books.create_session_token(telegram_id),
        httponly=True,
        max_age=config.ME_SESSION_TTL_SEC,
        samesite="lax",
    )


def create_app() -> FastAPI:
    app = FastAPI(title="ETH/BTC Trading Agent Dashboard", docs_url=None, redoc_url=None)

    ledger.init_db()
    paper.init_db()
    audit.init_db()
    macro_store.init_db()
    user_books.init_db()
    intel_store.init_db()
    live_ledger.init_db()
    import vault as hq_vault

    hq_vault.init_db()

    app.include_router(intel_router)

    templates = Jinja2Templates(directory=str(_PKG_DIR / "templates"))
    templates.env.filters["trade_time"] = format_trade_time
    templates.env.filters["trade_date"] = format_trade_date
    templates.env.filters["tag_tip"] = tag_tooltip
    templates.env.filters["news_when"] = format_news_when
    templates.env.filters["news_iso"] = format_news_iso
    templates.env.filters["news_age"] = format_news_age
    templates.env.filters["news_source"] = news_source_label
    templates.env.filters["usd"] = format_usd
    templates.env.filters["pct"] = format_pct
    templates.env.globals["trade_title"] = trade_title
    static_dir = _PKG_DIR / "static"
    if static_dir.is_dir():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request) -> HTMLResponse:
        import trade_ideas_bridge

        mill_paper = trade_ideas_bridge.volume_book_payload(limit=12)
        live_open = data.enrich_live_trades(live_ledger.get_open_trades(source="hq"))
        return templates.TemplateResponse(
            request,
            "index.html",
            {
                "status": data.get_status_payload(),
                "performance": data.get_performance_payload(),
                "positions": data.get_open_positions_payload(),
                "cycles": data.get_cycles(limit=25),
                "closed_trades": data.get_closed_trades_payload(limit=15),
                "archived_trades": data.get_archived_trades_payload(limit=15),
                "archived_performance": data.get_archived_performance_payload(),
                "macro": data.get_macro_payload(),
                "brain": get_brain_payload(),
                "live_open": live_open,
                "live_closed": data.enrich_live_trades(
                    live_ledger.get_closed_trades(limit=15, source="hq"),
                    closed=True,
                ),
                "live_unrealized_usd": data.live_unrealized_usd(live_open),
                "mill_open": live_ledger.get_open_trades(source="mill"),
                "mill_closed": live_ledger.get_closed_trades(limit=20, source="mill"),
                "live_performance": live_ledger.get_live_performance(),
                "mill_policy": {
                    "sleeve_usd": bot_config.LIVE_MILL_SLEEVE_USD,
                    "clip_qty": dict(bot_config.LIVE_PRODUCT_QTY_FLOORS),
                    "max_open": bot_config.LIVE_MILL_MAX_OPEN,
                    "max_fills_per_day": bot_config.LIVE_MILL_MAX_FILLS_PER_DAY,
                    "daily_loss_limit_usd": bot_config.LIVE_MILL_DAILY_LOSS_LIMIT_USD,
                },
                "mill_paper": mill_paper or {"available": False},
                "yield_enabled": bool(config.YIELD_GEN_API_URL),
                "yield_dashboard_url": config.YIELD_GEN_DASHBOARD_URL,
            },
        )

    @app.get("/api/brain")
    async def api_brain() -> dict:
        """Public intelligence hub snapshot (no HQ ideas, no service token)."""
        return get_brain_payload()

    @app.get("/api/yield")
    async def api_yield() -> dict:
        """Yield Generation tab payload (proxied from yield_gen_bot)."""
        return get_yield_payload()

    @app.get("/api/trades/live")
    async def api_live_trades(
        limit: int = 50, offset: int = 0, source: str = "hq"
    ) -> dict:
        return {
            "open": live_ledger.get_open_trades(source=source or None),
            "closed": live_ledger.get_closed_trades(
                limit=min(limit, 100), offset=max(offset, 0), source=source or None
            ),
            "performance": live_ledger.get_live_performance(),
        }

    @app.get("/api/brain/cycle-chart")
    async def api_brain_cycle_chart() -> FileResponse:
        thesis = intel_store.latest_long_thesis()
        path = (thesis or {}).get("chart_path")
        if not path or not Path(str(path)).exists():
            raise HTTPException(status_code=404, detail="No cycle chart")
        resolved = Path(str(path)).resolve()
        charts_root = Path(config.CHARTS_DIR).resolve()
        if charts_root not in resolved.parents and resolved != charts_root:
            raise HTTPException(status_code=404, detail="No cycle chart")
        return FileResponse(str(resolved), media_type="image/png")

    @app.get("/api/brain/structure/{product_id}/{timeframe}")
    async def api_brain_structure(product_id: str, timeframe: str) -> FileResponse:
        """Marked stance-board chart for a product/timeframe (public)."""
        path = stance_chart_path(product_id, timeframe)
        if path is None:
            raise HTTPException(status_code=404, detail="No structure chart")
        return FileResponse(path, media_type="image/png")

    @app.get("/api/brain/cycle-figure")
    async def api_brain_cycle_figure() -> dict:
        """Plotly spec for the interactive 4-year cycle chart (public)."""
        thesis = intel_store.latest_long_thesis() or {}
        path = ((thesis.get("thesis") or {}).get("cycle_figure_path")) or ""
        if not path:
            raise HTTPException(status_code=404, detail="No cycle figure")
        resolved = Path(str(path)).resolve()
        charts_root = Path(config.CHARTS_DIR).resolve()
        if charts_root not in resolved.parents or not resolved.is_file():
            raise HTTPException(status_code=404, detail="No cycle figure")
        return json.loads(resolved.read_text(encoding="utf-8"))

    @app.get("/volume", response_class=HTMLResponse)
    async def volume_book(request: Request) -> HTMLResponse:
        """Hidden paper book for every public-lane idea (no hub link)."""
        import trade_ideas_bridge

        book = trade_ideas_bridge.volume_book_payload(limit=150)
        return templates.TemplateResponse(
            request,
            "volume.html",
            {"book": book or {"available": False}},
        )

    # The two investor handlers are sync on purpose: the payload makes a
    # blocking Coinbase call plus a pile of sqlite reads, so FastAPI runs them
    # in a threadpool rather than stalling the event loop for everyone else.
    @app.get("/investors", response_class=HTMLResponse)
    def investors(request: Request) -> Response:
        """Private investor view — unlisted, and token-gated when configured."""
        if not _investor_authorized(request):
            raise HTTPException(status_code=404, detail="Not Found")
        response = templates.TemplateResponse(
            request,
            "investors.html",
            {"inv": build_investor_payload()},
        )
        _set_investor_cookie(response, request)
        return response

    @app.get("/api/investors/snapshot")
    def api_investor_snapshot(request: Request) -> dict:
        if not _investor_authorized(request):
            raise HTTPException(status_code=404, detail="Not Found")
        return build_investor_payload(include_paper=False)

    @app.get("/feed", response_class=HTMLResponse)
    async def idea_feed(request: Request) -> HTMLResponse:
        """Public mill stream — same cards for every visitor; Accept needs login."""
        telegram_id = _resolve_telegram_id(request)
        response = templates.TemplateResponse(
            request,
            "feed.html",
            {
                "signed_in": telegram_id is not None,
                "telegram_id": telegram_id,
            },
        )
        if telegram_id is not None:
            _set_session_cookie(response, request, telegram_id)
        return response

    @app.get("/api/ideas/stream")
    async def api_idea_stream(
        request: Request, limit: int = 40, after_id: int | None = None
    ) -> dict:
        import trade_ideas_bridge

        telegram_id = _resolve_telegram_id(request)
        payload = trade_ideas_bridge.idea_stream(
            limit=min(max(limit, 1), 100),
            after_id=after_id,
            user_id=telegram_id,
        )
        if payload is None:
            return {
                "available": False,
                "ideas": [],
                "latest_id": 0,
                "signed_in": telegram_id is not None,
            }
        payload["signed_in"] = telegram_id is not None
        return payload

    @app.get("/api/ideas/funnel")
    async def api_idea_funnel() -> dict:
        import trade_ideas_bridge

        payload = trade_ideas_bridge.idea_funnel()
        if payload is None:
            return {"available": False}
        return payload

    @app.post("/api/ideas/{idea_id}/decision")
    async def api_idea_decision(
        request: Request, idea_id: int, body: IdeaDecisionBody
    ) -> dict:
        import access
        import trade_ideas_bridge

        telegram_id = _resolve_telegram_id(request)
        if telegram_id is None:
            raise HTTPException(
                status_code=401,
                detail="Open the feed from Telegram (Idea feed button) to Accept or Reject.",
            )
        if not access.is_allowed(telegram_id):
            raise HTTPException(status_code=403, detail="Not authorized.")
        if body.decision not in ("accept", "reject"):
            raise HTTPException(status_code=422, detail="decision must be accept or reject")
        status = trade_ideas_bridge.record_decision(
            int(idea_id), telegram_id, body.decision
        )
        if status == "unavailable":
            raise HTTPException(status_code=503, detail="Idea book unavailable.")
        if status == "unknown_idea":
            raise HTTPException(status_code=404, detail="Idea not found.")
        return {
            "ok": status == "recorded",
            "status": status,
            "decision": body.decision,
            "idea_id": int(idea_id),
            "message": trade_ideas_bridge.format_decision_reply(
                status, body.decision, int(idea_id)
            ),
        }

    @app.get("/api/vault/snapshot")
    async def api_vault_snapshot() -> dict:
        import vault as hq_vault

        return hq_vault.snapshot()

    @app.get("/api/vault/stream")
    async def api_vault_stream(
        request: Request, limit: int = 20, after_id: int | None = None
    ) -> dict:
        import vault as hq_vault

        telegram_id = _resolve_telegram_id(request)
        payload = hq_vault.stream(
            limit=min(max(limit, 1), 50),
            after_id=after_id,
            user_id=telegram_id,
        )
        payload["signed_in"] = telegram_id is not None
        return payload

    @app.post("/api/vault/{allocation_id}/decision")
    async def api_vault_decision(
        request: Request, allocation_id: int, body: IdeaDecisionBody
    ) -> dict:
        import access
        import vault as hq_vault

        telegram_id = _resolve_telegram_id(request)
        if telegram_id is None:
            raise HTTPException(
                status_code=401,
                detail="Open the feed from Telegram (Idea feed button) to follow vault allocations.",
            )
        if not access.is_allowed(telegram_id):
            raise HTTPException(status_code=403, detail="Not authorized.")
        if body.decision not in ("accept", "reject"):
            raise HTTPException(status_code=422, detail="decision must be accept or reject")
        spots = data.get_live_spots()
        result = hq_vault.follow(
            int(allocation_id),
            telegram_id,
            body.decision,
            spots=spots.get("spots") if isinstance(spots, dict) else spots,
        )
        if result["status"] == "unknown_idea":
            raise HTTPException(status_code=404, detail="Vault allocation not found.")
        return result

    @app.get("/me", response_class=HTMLResponse)
    async def me(request: Request) -> Response:
        telegram_id = _resolve_telegram_id(request)
        if telegram_id is None:
            return templates.TemplateResponse(
                request,
                "me.html",
                {
                    "authorized": False,
                    "me": None,
                    "error": "Open My book from Telegram for a fresh link.",
                },
                status_code=401,
            )

        payload = data.get_me_payload(telegram_id)
        if payload is None:
            return templates.TemplateResponse(
                request,
                "me.html",
                {
                    "authorized": True,
                    "me": None,
                    "error": "No personal paper account yet. Open an account in Telegram first.",
                },
            )

        response = templates.TemplateResponse(
            request,
            "me.html",
            {"authorized": True, "me": payload, "error": None},
        )
        _set_session_cookie(response, request, telegram_id)
        return response

    @app.get("/api/spot")
    async def api_spot() -> dict:
        return data.get_live_spot()

    @app.get("/api/spots")
    async def api_spots() -> dict:
        return data.get_live_spots()

    @app.get("/api/status")
    async def api_status() -> dict:
        return data.get_status_payload()

    @app.get("/api/positions")
    async def api_positions() -> list:
        return data.get_open_positions_payload()

    @app.get("/api/trades/paper")
    async def api_paper_trades(limit: int = 50, offset: int = 0) -> list:
        return data.get_closed_trades_payload(
            limit=min(limit, 100), offset=max(offset, 0)
        )

    @app.get("/api/trades/archived")
    async def api_archived_trades(limit: int = 50, offset: int = 0) -> list:
        return data.get_archived_trades_payload(
            limit=min(limit, 100), offset=max(offset, 0)
        )

    @app.get("/api/cycles")
    async def api_cycles(limit: int = 30, offset: int = 0) -> list:
        return data.get_cycles(limit=min(limit, 100), offset=max(offset, 0))

    @app.get("/api/cycles/{cycle_id}")
    async def api_cycle_detail(cycle_id: str) -> dict:
        detail = data.get_cycle_detail(cycle_id)
        if detail is None:
            raise HTTPException(status_code=404, detail="Cycle not found")
        return detail

    @app.get("/api/performance")
    async def api_performance() -> dict:
        return data.get_performance_payload()

    @app.get("/api/macro")
    async def api_macro() -> dict:
        return data.get_macro_payload()

    @app.get("/api/ops/watchdog-execute")
    async def api_watchdog_execute_status() -> dict:
        return {
            "watchdog_enabled": bot_config.WATCHDOG_ENABLED,
            "execute_enabled": bot_config.watchdog_execute_enabled(),
            "allow_shorts": bot_config.WATCHDOG_ALLOW_SHORTS,
            "config_default": bot_config.WATCHDOG_EXECUTE_ENABLED,
        }

    @app.post("/api/ops/watchdog-execute")
    async def api_watchdog_execute_set(
        body: WatchdogExecuteBody,
        authorization: str | None = Header(default=None),
    ) -> dict:
        secret = config.MACRO_WEBHOOK_SECRET
        if not secret:
            raise HTTPException(
                status_code=503,
                detail="MACRO_WEBHOOK_SECRET not configured (ops auth)",
            )
        expected = f"Bearer {secret}"
        if authorization != expected:
            raise HTTPException(status_code=401, detail="Unauthorized")
        enabled = bot_config.set_watchdog_execute_enabled(bool(body.enabled))
        return {
            "ok": True,
            "execute_enabled": enabled,
            "allow_shorts": bot_config.WATCHDOG_ALLOW_SHORTS,
        }

    @app.post("/api/macro/ingest")
    async def api_macro_ingest(
        body: MacroIngestBody,
        authorization: str | None = Header(default=None),
    ) -> dict:
        secret = config.MACRO_WEBHOOK_SECRET
        if not secret:
            raise HTTPException(status_code=503, detail="MACRO_WEBHOOK_SECRET not configured")
        expected = f"Bearer {secret}"
        if authorization != expected:
            raise HTTPException(status_code=401, detail="Unauthorized")

        event = ingest_headline(
            title=body.title,
            url=body.url,
            summary=body.summary,
            source=body.source or "webhook",
            published_at=body.published_at,
            force_classify=body.force_classify,
        )
        if event is None:
            return {"ok": True, "duplicate": True, "event": None}
        return {"ok": True, "duplicate": False, "event": event}

    @app.get("/api/chart/latest")
    async def api_chart_latest() -> FileResponse:
        snapshot = audit.get_latest_snapshot()
        if snapshot is None:
            raise HTTPException(status_code=404, detail="No snapshot")
        path = h4_marked_path(snapshot.get("marked_chart_paths"))
        if path is None:
            raise HTTPException(status_code=404, detail="H4 chart not found")
        return FileResponse(path, media_type="image/png")

    @app.get("/api/chart/product/{product_id}/h4")
    async def api_chart_product_h4(product_id: str) -> FileResponse:
        """Newest marked H4 PNG for a product (disk fallback when no snapshot)."""
        path = latest_marked_h4_path(product_id)
        if path is None:
            raise HTTPException(status_code=404, detail="H4 chart not found")
        return FileResponse(path, media_type="image/png")

    @app.get("/api/chart/{cycle_id}")
    async def api_chart_cycle(
        cycle_id: str,
        kind: str = "marked",
        tf: str = "H4",
    ) -> FileResponse:
        kind_n = (kind or "marked").lower()
        tf_n = (tf or "H4").upper()
        if kind_n not in VALID_KINDS:
            raise HTTPException(status_code=400, detail=f"Invalid kind={kind!r}")
        if tf_n not in VALID_TFS:
            raise HTTPException(status_code=400, detail=f"Invalid tf={tf!r}")

        snapshot = audit.get_snapshot(cycle_id)
        marked = (snapshot or {}).get("marked_chart_paths") if snapshot else None
        row = ledger.get_suggestion_by_cycle_id(cycle_id)
        ledger_path = (row or {}).get("chart_path") if row else None

        if kind_n == "marked" and tf_n == "H4":
            path = h4_marked_path(marked)
            if path is None and row:
                for part in str(ledger_path or "").split(","):
                    path = resolve_chart_path(part.strip())
                    if path and "H4" in path.name and "marked" in path.name:
                        break
                    path = None
        else:
            path = resolve_trade_chart(
                cycle_id,
                kind=kind_n,
                tf=tf_n,
                ledger_chart_path=ledger_path,
                marked_chart_paths=marked,
            )

        if path is None:
            raise HTTPException(status_code=404, detail="Chart not found")
        return FileResponse(path, media_type="image/png")

    return app
