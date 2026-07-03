"""FastAPI application — public read-only dashboard."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import audit
import ledger
import paper
from dashboard import data
from dashboard.charts import h12_marked_path, resolve_chart_path

_PKG_DIR = Path(__file__).resolve().parent


def create_app() -> FastAPI:
    app = FastAPI(title="ETH Trading Agent Dashboard", docs_url=None, redoc_url=None)

    ledger.init_db()
    paper.init_db()
    audit.init_db()

    templates = Jinja2Templates(directory=str(_PKG_DIR / "templates"))
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

    @app.get("/api/chart/latest")
    async def api_chart_latest() -> FileResponse:
        snapshot = audit.get_latest_snapshot()
        if snapshot is None:
            raise HTTPException(status_code=404, detail="No snapshot")
        path = h12_marked_path(snapshot.get("marked_chart_paths"))
        if path is None:
            raise HTTPException(status_code=404, detail="H12 chart not found")
        return FileResponse(path, media_type="image/png")

    @app.get("/api/chart/{cycle_id}")
    async def api_chart_cycle(cycle_id: str) -> FileResponse:
        snapshot = audit.get_snapshot(cycle_id)
        marked = (snapshot or {}).get("marked_chart_paths") if snapshot else None
        path = h12_marked_path(marked)
        if path is None:
            row = ledger.get_suggestion_by_cycle_id(cycle_id)
            if row:
                for part in str(row.get("chart_path") or "").split(","):
                    path = resolve_chart_path(part.strip())
                    if path and "H12" in path.name and "marked" in path.name:
                        break
                    path = None
        if path is None:
            raise HTTPException(status_code=404, detail="Chart not found")
        return FileResponse(path, media_type="image/png")

    return app
