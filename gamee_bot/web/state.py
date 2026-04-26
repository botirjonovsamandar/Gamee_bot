from __future__ import annotations

import re
import threading
from collections import defaultdict, deque
from datetime import datetime, timezone
from typing import Any, Callable

from gamee_bot.account_store import load_accounts
from gamee_bot.config import (
    AppConfig,
    background_mode_label,
    gamee_proxy_table_summary,
)
from gamee_bot.gamee_transport import normalize_gamee_transport_backend


EventCallback = Callable[[dict[str, Any]], None]


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _account_from_message(message: str) -> str | None:
    m = re.match(r"^\[([^\]]+)\]", message.strip())
    return m.group(1).strip() if m else None


def _kind_from_message(message: str) -> str:
    low = message.lower()
    if "критично" in low:
        return "fatal"
    if any(x in low for x in ("cloudflare", "429", "ошибка", "exception", "traceback")):
        return "error"
    if "ход #" in low or "бросок" in low:
        return "move"
    if "ежеднев" in low or "daily" in low:
        return "daily"
    if "сезон" in low or "season" in low:
        return "season"
    return "info"


class AppStateStore:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._rows: list[dict[str, Any]] = []
        self._logs: deque[dict[str, Any]] = deque(maxlen=2000)
        self._earnings: dict[str, dict[str, int]] = defaultdict(
            lambda: {"gold": 0, "tickets": 0, "xp": 0}
        )
        self._worker_status: dict[str, Any] = {
            "running": False,
            "stopping": False,
            "phase": "stopped",
            "started_at": None,
            "account_count": 0,
            "active_count": 0,
            "manual_busy": False,
            "code_busy": False,
        }
        self._ui_config: dict[str, Any] = {}
        self._seq = 0
        self._on_event: EventCallback | None = None

    def set_event_callback(self, callback: EventCallback | None) -> None:
        with self._lock:
            self._on_event = callback

    def _publish_locked(self, event_type: str, payload: dict[str, Any]) -> None:
        self._seq += 1
        event = {"type": event_type, "seq": self._seq, "payload": payload}
        cb = self._on_event
        if cb is not None:
            cb(event)

    def _with_earnings_locked(self, row: dict[str, Any]) -> dict[str, Any]:
        out = dict(row)
        label = str(out.get("label", "") or "")
        earned = self._earnings.get(label, {"gold": 0, "tickets": 0, "xp": 0})
        out["session_earned"] = dict(earned)
        return out

    def configure_from_app(self, cfg: AppConfig) -> None:
        c = cfg.compliance
        ui_config = {
            "window_title": cfg.window_title,
            "background_mode": c.background_mode,
            "background_mode_label": background_mode_label(c.background_mode),
            "fast_bootstrap_enabled": bool(c.fast_bootstrap_enabled),
            "steady_energy_targets": list(c.steady_energy_targets),
            "transport_backend": normalize_gamee_transport_backend(
                cfg.gamee.transport_backend
            ),
            "quiet_hours_enabled": bool(c.quiet_hours_enabled),
            "daily_move_budget": int(c.daily_move_budget),
            "max_moves_per_session": int(c.max_moves_per_session),
            "gold_micro_divisor": int(cfg.gamee.gold_micro_divisor),
            "gold_estimate_usd_micro_divisor": int(
                cfg.gamee.gold_estimate_usd_micro_divisor
            ),
            "check_task_id": int(cfg.gamee.check_task_id),
        }
        with self._lock:
            self._ui_config = ui_config
            self._publish_locked("config.updated", {"config": dict(ui_config)})

    def load_placeholders(self, cfg: AppConfig) -> None:
        rows: list[dict[str, Any]] = []
        accounts = load_accounts(cfg.accounts_path)
        for acc in accounts:
            px_cell, px_tip = gamee_proxy_table_summary(acc.proxy_url)
            rows.append(
                {
                    "label": acc.label,
                    "energy": 0,
                    "gold": 0,
                    "gold_estimated_usd": None,
                    "status": "—",
                    "last_error": "",
                    "last_move_at": "",
                    "regen_deadline_iso": None,
                    "daily_claim_rewards_text": "",
                    "daily_checkin_deadline_iso": None,
                    "daily_checkin_streak": 0,
                    "daily_checkin_streak_total": 0,
                    "season_rewards_text": "—",
                    "proxy_cell": px_cell,
                    "proxy_tooltip": px_tip,
                }
            )
        self.update_rows(rows)

    def update_rows(self, rows: list[dict[str, Any]]) -> None:
        with self._lock:
            self._rows = [self._with_earnings_locked(r) for r in rows]
            self._worker_status["account_count"] = len(self._rows)
            self._worker_status["active_count"] = self._active_count_locked()
            self._publish_locked("rows.updated", {"rows": list(self._rows)})
            self._publish_locked(
                "worker.updated", {"worker": dict(self._worker_status)}
            )

    def upsert_row(self, row: dict[str, Any]) -> None:
        label = str(row.get("label", "") or "").strip()
        if not label:
            return
        with self._lock:
            next_rows: list[dict[str, Any]] = []
            replaced = False
            for cur in self._rows:
                if str(cur.get("label", "") or "") == label:
                    next_rows.append(self._with_earnings_locked(row))
                    replaced = True
                else:
                    next_rows.append(cur)
            if not replaced:
                next_rows.append(self._with_earnings_locked(row))
            self._rows = next_rows
            self._worker_status["account_count"] = len(self._rows)
            self._worker_status["active_count"] = self._active_count_locked()
            self._publish_locked("rows.updated", {"rows": list(self._rows)})

    def update_proxy_display(self, label: str, proxy_cell: str, proxy_tooltip: str) -> None:
        key = str(label or "").strip()
        if not key:
            return
        with self._lock:
            next_rows: list[dict[str, Any]] = []
            for cur in self._rows:
                if str(cur.get("label", "") or "") == key:
                    updated = dict(cur)
                    updated["proxy_cell"] = proxy_cell
                    updated["proxy_tooltip"] = proxy_tooltip
                    next_rows.append(updated)
                else:
                    next_rows.append(cur)
            self._rows = next_rows
            self._publish_locked("rows.updated", {"rows": list(self._rows)})

    def remove_row(self, label: str) -> None:
        key = str(label or "").strip()
        if not key:
            return
        with self._lock:
            self._rows = [
                row for row in self._rows if str(row.get("label", "") or "") != key
            ]
            self._worker_status["account_count"] = len(self._rows)
            self._worker_status["active_count"] = self._active_count_locked()
            self._publish_locked("rows.updated", {"rows": list(self._rows)})
            self._publish_locked(
                "worker.updated", {"worker": dict(self._worker_status)}
            )

    def add_log(self, message: str, *, kind: str | None = None) -> None:
        text = str(message or "").strip()
        if not text:
            return
        event = {
            "ts": _utc_now_iso(),
            "account_label": _account_from_message(text),
            "message": text,
            "kind": kind or _kind_from_message(text),
        }
        with self._lock:
            self._logs.append(event)
            self._publish_locked("log.added", {"log": dict(event)})

    def add_earnings(
        self, label: str, gold_delta: int, tickets_delta: int, xp_delta: int
    ) -> None:
        key = str(label or "").strip()
        if not key:
            return
        with self._lock:
            cur = self._earnings[key]
            cur["gold"] += int(gold_delta)
            cur["tickets"] += int(tickets_delta)
            cur["xp"] += int(xp_delta)
            self._rows = [self._with_earnings_locked(r) for r in self._rows]
            self._publish_locked(
                "earnings.updated",
                {"label": key, "session_earned": dict(cur)},
            )
            self._publish_locked("rows.updated", {"rows": list(self._rows)})

    def set_worker_status(self, **patch: Any) -> None:
        with self._lock:
            self._worker_status.update(patch)
            self._worker_status["active_count"] = self._active_count_locked()
            self._publish_locked(
                "worker.updated", {"worker": dict(self._worker_status)}
            )

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "worker": dict(self._worker_status),
                "rows": [dict(r) for r in self._rows],
                "logs": [dict(x) for x in self._logs],
                "session_earnings": {
                    k: dict(v) for k, v in self._earnings.items()
                },
                "config": dict(self._ui_config),
            }

    def snapshot_event(self) -> dict[str, Any]:
        with self._lock:
            self._seq += 1
            return {
                "type": "snapshot",
                "seq": self._seq,
                "payload": self.snapshot(),
            }

    def _active_count_locked(self) -> int:
        active_markers = (
            "синх",
            "награда",
            "бросок",
            "ход",
            "bootstrap",
            "первый",
            "cooldown",
            "сервер занят",
        )
        count = 0
        for row in self._rows:
            status = str(row.get("status", "") or "").lower()
            if any(x in status for x in active_markers):
                count += 1
        return count
