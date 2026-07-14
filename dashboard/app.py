"""FastAPI application — public read-only dashboard."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Header
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

import audit
import config
import ledger
import paper
from dashboard import data
from dashboard.charts import (
    VALID_KINDS,
    VALID_TFS,
    h4_marked_path,
    resolve_chart_path,
    resolve_trade_chart,
)
from dashboard.formatting import (
    format_trade_date,
    format_trade_time,
    tag_tooltip,
    trade_title,
)
from macro import store as macro_store
from macro.context import macro_payload_for_dashboard
from macro.ingest import ingest_headline

_PKG_DIR = Path(__file__).resolve().parent


class MacroIngestBody(BaseModel):
    title: str = Field(min_length=1)
    url: str | None = None
    summary: str | None = None
    source: str | None = None
    published_at: str | None = None
    force_classify: bool = False


def create_app() -> FastAPI:
    app = FastAPI(title="ETH Trading Agent Dashboard", docs_url=None, redoc_url=None)

    ledger.init_db()
    paper.init_db()
    audit.init_db()
    macro_store.init_db()

    templates = Jinja2Templates(directory=str(_PKG_DIR / "templates"))
    templates.env.filters["trade_time"] = format_trade_time
    templates.env.filters["trade_date"] = format_trade_date
    templates.env.filters["tag_tip"] = tag_tooltip
    templates.env.globals["trade_title"] = trade_title
    static_dir = _PKG_DIR / "static"
    if static_dir.is_dir():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request) -> HTMLResponse:
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
                "macro": data.get_macro_payload(),
            },
        )

    @app.get("/api/spot")
    async def api_spot() -> dict:
        return data.get_live_spot()

    @app.get("/api/status")
    async def api_status() -> dict:
        return data.get_status_payload()

    @app.get("/api/positions")
    async def api_positions() -> list:
        return data.get_open_positions_payload()

    @app.get("/api/trades/paper")
    async def api_paper_trades() -> list:
        return data.get_closed_trades_payload()

    @app.get("/api/cycles")
    async def api_cycles(limit: int = 30, offset: int = 0) -> list:
        return data.get_cycles(limit=min(limit, 100), offset=offset)

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

        # Default (no query / marked+H4): preserve legacy H4-marked behaviour.
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
