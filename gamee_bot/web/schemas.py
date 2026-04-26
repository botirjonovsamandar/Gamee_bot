from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class ProxyUpdateRequest(BaseModel):
    proxy_url: str | None = None


class MassCodeRequest(BaseModel):
    code: str = Field(min_length=1)
    task_id: int | None = None


class ApiMessage(BaseModel):
    ok: bool = True
    message: str = "ok"


class WorkerStatus(BaseModel):
    running: bool
    stopping: bool = False
    phase: str = "stopped"
    started_at: str | None = None
    account_count: int = 0
    active_count: int = 0
    manual_busy: bool = False
    code_busy: bool = False


class LogEvent(BaseModel):
    ts: str
    account_label: str | None = None
    message: str
    kind: Literal["info", "move", "daily", "season", "error", "fatal"] = "info"


class WsEvent(BaseModel):
    type: str
    seq: int
    payload: dict[str, Any]

