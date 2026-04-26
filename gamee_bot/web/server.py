from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse

from gamee_bot.web.runtime import WebRuntime
from gamee_bot.web.schemas import MassCodeRequest, ProxyUpdateRequest
from gamee_bot.web.state import AppStateStore


ROOT_DIR = Path(__file__).resolve().parents[2]
CONFIG_PATH = Path(os.environ.get("GAMEE_BOT_CONFIG", ROOT_DIR / "config.yaml")).resolve()
WEB_DIST = ROOT_DIR / "web" / "dist"


class WebSocketHub:
    def __init__(self) -> None:
        self._clients: set[WebSocket] = set()
        self._loop: asyncio.AbstractEventLoop | None = None

    def set_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self._clients.add(ws)

    def disconnect(self, ws: WebSocket) -> None:
        self._clients.discard(ws)

    def publish_threadsafe(self, event: dict[str, Any]) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        loop.call_soon_threadsafe(lambda: asyncio.create_task(self.broadcast(event)))

    async def broadcast(self, event: dict[str, Any]) -> None:
        dead: list[WebSocket] = []
        for ws in list(self._clients):
            try:
                await ws.send_json(event)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)


store = AppStateStore()
hub = WebSocketHub()
runtime = WebRuntime(CONFIG_PATH, store)


def create_app() -> FastAPI:
    app = FastAPI(title="Gamee Bot Web UI")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://127.0.0.1:5173",
            "http://localhost:5173",
            "http://127.0.0.1:8000",
            "http://localhost:8000",
        ],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.on_event("startup")
    async def _startup() -> None:
        hub.set_loop(asyncio.get_running_loop())
        store.set_event_callback(hub.publish_threadsafe)
        runtime.load()

    @app.get("/api/state")
    async def api_state() -> dict[str, Any]:
        return store.snapshot()

    @app.post("/api/worker/start")
    async def api_worker_start() -> dict[str, Any]:
        return runtime.start_worker()

    @app.post("/api/worker/stop")
    async def api_worker_stop() -> dict[str, Any]:
        return runtime.stop_worker()

    @app.post("/api/accounts/{label}/sync")
    async def api_account_sync(label: str) -> dict[str, Any]:
        return runtime.run_account_action(label, "sync")

    @app.post("/api/accounts/{label}/claim-daily")
    async def api_account_claim_daily(label: str) -> dict[str, Any]:
        return runtime.run_account_action(label, "claim_daily")

    @app.post("/api/accounts/{label}/play-session")
    async def api_account_play_session(label: str) -> dict[str, Any]:
        return runtime.run_account_action(label, "play_session")

    @app.post("/api/accounts/{label}/proxy")
    async def api_account_proxy(
        label: str, body: ProxyUpdateRequest
    ) -> dict[str, Any]:
        return runtime.update_proxy(label, body.proxy_url)

    @app.delete("/api/accounts/{label}")
    async def api_account_delete(label: str) -> dict[str, Any]:
        return runtime.delete_account(label)

    @app.post("/api/code/mass-submit")
    async def api_mass_code(body: MassCodeRequest) -> dict[str, Any]:
        return runtime.submit_mass_code(body.code, body.task_id)

    @app.websocket("/ws/events")
    async def ws_events(ws: WebSocket) -> None:
        await hub.connect(ws)
        try:
            await ws.send_json(store.snapshot_event())
            while True:
                await ws.receive_text()
        except WebSocketDisconnect:
            hub.disconnect(ws)
        except Exception:
            hub.disconnect(ws)

    @app.get("/{path:path}", include_in_schema=False)
    async def spa(path: str) -> Any:
        if path.startswith("api/") or path.startswith("ws/"):
            return HTMLResponse("Not found", status_code=404)
        if WEB_DIST.exists():
            target = (WEB_DIST / path).resolve()
            try:
                target.relative_to(WEB_DIST.resolve())
            except ValueError:
                target = WEB_DIST / "index.html"
            if target.is_file():
                return FileResponse(target)
            index = WEB_DIST / "index.html"
            if index.is_file():
                return FileResponse(index)
        return HTMLResponse(
            "<h1>Gamee Bot Web UI</h1>"
            "<p>React build не найден. Запустите: "
            "<code>cd web && npm install && npm run dev</code>.</p>"
        )

    return app


app = create_app()

