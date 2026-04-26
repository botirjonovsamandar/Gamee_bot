from __future__ import annotations

import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import HTTPException
from PySide6.QtCore import Qt

from gamee_bot.account_store import remove_account_by_label, set_account_proxy_url
from gamee_bot.config import (
    BACKGROUND_MODE_FULL_AUTO,
    TELETHON_CREDENTIALS_REQUIRED_MSG,
    AppConfig,
    gamee_proxy_table_summary,
    load_config,
    telethon_credentials_ready,
)
from gamee_bot.gamee_transport import gamee_transport_backend_blocker_message
from gamee_bot.telethon_bridge import clear_init_cache
from gamee_bot.ui.account_action_thread import AccountActionThread
from gamee_bot.ui.enter_code_thread import EnterCodeThread
from gamee_bot.web.state import AppStateStore
from gamee_bot.worker import BotWorker


class WebRuntime:
    def __init__(self, config_path: Path, store: AppStateStore) -> None:
        self._config_path = config_path.resolve()
        self._store = store
        self._lock = threading.RLock()
        self._cfg: AppConfig | None = None
        self._worker: BotWorker | None = None
        self._manual_thread: AccountActionThread | None = None
        self._code_thread: EnterCodeThread | None = None
        self._manual_moves_used_today: dict[str, tuple[str, int]] = {}

    def load(self) -> AppConfig:
        cfg = load_config(self._config_path)
        self._cfg = cfg
        self._store.configure_from_app(cfg)
        self._store.load_placeholders(cfg)
        self._refresh_busy_flags()
        return cfg

    def _cfg_ready(self) -> AppConfig:
        cfg = load_config(self._config_path)
        self._cfg = cfg
        self._store.configure_from_app(cfg)
        return cfg

    def _ensure_can_use_transport(self, cfg: AppConfig) -> None:
        if not telethon_credentials_ready(cfg):
            raise HTTPException(status_code=400, detail=TELETHON_CREDENTIALS_REQUIRED_MSG)
        blocker = gamee_transport_backend_blocker_message(cfg.gamee.transport_backend)
        if blocker:
            raise HTTPException(status_code=400, detail=blocker)

    def start_worker(self) -> dict[str, Any]:
        with self._lock:
            if self._worker is not None and self._worker.isRunning():
                return {"ok": True, "message": "Фон уже запущен"}
            if self._manual_thread is not None and self._manual_thread.isRunning():
                raise HTTPException(
                    status_code=409,
                    detail="Дождитесь завершения ручного действия.",
                )
            cfg = self._cfg_ready()
            self._ensure_can_use_transport(cfg)
            cfg.compliance.background_mode = BACKGROUND_MODE_FULL_AUTO
            worker = BotWorker(cfg)
            worker.table_updated.connect(
                self._on_worker_rows, Qt.ConnectionType.DirectConnection
            )
            worker.log_message.connect(
                self._on_log, Qt.ConnectionType.DirectConnection
            )
            worker.fatal_error.connect(
                self._on_fatal, Qt.ConnectionType.DirectConnection
            )
            worker.session_earnings_move.connect(
                self._on_earnings, Qt.ConnectionType.DirectConnection
            )
            worker.finished.connect(
                self._on_worker_finished, Qt.ConnectionType.DirectConnection
            )
            self._worker = worker
            started_at = datetime.now(timezone.utc).isoformat()
            self._store.set_worker_status(
                running=True,
                stopping=False,
                phase="fast_bootstrap" if cfg.compliance.fast_bootstrap_enabled else "steady",
                started_at=started_at,
            )
            self._store.add_log(
                "Запущено всё: синхронизация, награды и ходы для всех аккаунтов."
            )
            worker.start()
            return {"ok": True, "message": "Фон запущен"}

    def stop_worker(self) -> dict[str, Any]:
        with self._lock:
            worker = self._worker
            if worker is None:
                self._store.set_worker_status(
                    running=False, stopping=False, phase="stopped", started_at=None
                )
                return {"ok": True, "message": "Фон уже остановлен"}
            if worker.isRunning():
                worker.stop()
                worker.wake_idle()
                self._store.set_worker_status(stopping=True, phase="stopping")
                self._store.add_log("Остановка фона запрошена.")
                return {"ok": True, "message": "Остановка запрошена"}
            self._worker = None
            self._store.set_worker_status(
                running=False, stopping=False, phase="stopped", started_at=None
            )
            return {"ok": True, "message": "Фон остановлен"}

    def run_account_action(self, label: str, action: str) -> dict[str, Any]:
        label = str(label or "").strip()
        action = str(action or "").strip()
        if not label:
            raise HTTPException(status_code=400, detail="Пустой label.")
        if action not in {"sync", "claim_daily", "play_session"}:
            raise HTTPException(status_code=400, detail="Неизвестное действие.")
        with self._lock:
            if self._worker is not None and self._worker.isRunning():
                raise HTTPException(
                    status_code=409,
                    detail="Сначала остановите фоновый режим.",
                )
            if self._manual_thread is not None and self._manual_thread.isRunning():
                raise HTTPException(
                    status_code=409,
                    detail="Уже выполняется другое ручное действие.",
                )
            cfg = self._cfg_ready()
            self._ensure_can_use_transport(cfg)
            move_limit = 0
            if action == "play_session":
                move_limit = min(
                    int(cfg.compliance.max_moves_per_session),
                    self._remaining_manual_moves(cfg, label),
                )
                if move_limit <= 0:
                    raise HTTPException(
                        status_code=409,
                        detail="Дневной бюджет ходов исчерпан.",
                    )
            thread = AccountActionThread(cfg, label, action, move_limit=move_limit)
            thread.log_line.connect(self._on_log, Qt.ConnectionType.DirectConnection)
            thread.row_ready.connect(
                self._on_manual_row, Qt.ConnectionType.DirectConnection
            )
            thread.move_earned.connect(
                self._on_manual_earnings, Qt.ConnectionType.DirectConnection
            )
            thread.finished.connect(
                self._on_manual_finished, Qt.ConnectionType.DirectConnection
            )
            self._manual_thread = thread
            self._store.set_worker_status(manual_busy=True)
            self._store.add_log(f"[{label}] {action} — старт.")
            thread.start()
            return {"ok": True, "message": "Действие запущено"}

    def update_proxy(self, label: str, proxy_url: str | None) -> dict[str, Any]:
        label = str(label or "").strip()
        if not label:
            raise HTTPException(status_code=400, detail="Пустой label.")
        with self._lock:
            cfg = self._cfg_ready()
            try:
                ok = set_account_proxy_url(
                    cfg.accounts_path, label, proxy_url if proxy_url else None
                )
            except OSError as e:
                raise HTTPException(status_code=500, detail=str(e)) from e
            if not ok:
                raise HTTPException(status_code=404, detail="Аккаунт не найден.")
            if self._worker is not None and self._worker.isRunning():
                self._worker.wake_idle(label)
                px_cell, px_tip = gamee_proxy_table_summary(proxy_url)
                self._store.update_proxy_display(label, px_cell, px_tip)
            else:
                self._store.load_placeholders(cfg)
            self._store.add_log(f"Прокси для «{label}» обновлён.")
            return {"ok": True, "message": "Прокси обновлён"}

    def delete_account(self, label: str) -> dict[str, Any]:
        label = str(label or "").strip()
        if not label:
            raise HTTPException(status_code=400, detail="Пустой label.")
        with self._lock:
            cfg = self._cfg_ready()
            removed, _session_path = remove_account_by_label(cfg.accounts_path, label)
            if not removed:
                raise HTTPException(status_code=404, detail="Аккаунт не найден.")
            clear_init_cache(label)
            if self._worker is not None and self._worker.isRunning():
                self._worker.wake_idle(label)
                self._store.remove_row(label)
            else:
                self._store.load_placeholders(cfg)
            self._store.add_log(f"Аккаунт «{label}» удалён из accounts.yaml.")
            return {"ok": True, "message": "Аккаунт удалён"}

    def submit_mass_code(self, code: str, task_id: int | None) -> dict[str, Any]:
        code = str(code or "").strip()
        if not code:
            raise HTTPException(status_code=400, detail="Код пустой.")
        with self._lock:
            if self._worker is not None and self._worker.isRunning():
                raise HTTPException(
                    status_code=409,
                    detail="Сначала остановите фоновый режим.",
                )
            if self._code_thread is not None and self._code_thread.isRunning():
                raise HTTPException(
                    status_code=409,
                    detail="Промокод уже отправляется.",
                )
            cfg = self._cfg_ready()
            self._ensure_can_use_transport(cfg)
            tid = int(task_id if task_id is not None else cfg.gamee.check_task_id)
            thread = EnterCodeThread(cfg, code, tid)
            thread.log_line.connect(self._on_log, Qt.ConnectionType.DirectConnection)
            thread.finished.connect(
                self._on_code_finished, Qt.ConnectionType.DirectConnection
            )
            self._code_thread = thread
            self._store.set_worker_status(code_busy=True)
            thread.start()
            return {"ok": True, "message": "Промокод отправляется"}

    def _on_worker_rows(self, rows: object) -> None:
        if isinstance(rows, list):
            self._store.update_rows([dict(r) for r in rows if isinstance(r, dict)])

    def _on_manual_row(self, row: object) -> None:
        if isinstance(row, dict):
            self._store.upsert_row(dict(row))

    def _on_log(self, message: str) -> None:
        self._store.add_log(str(message or ""))

    def _on_fatal(self, message: str) -> None:
        self._store.add_log("КРИТИЧНО: " + str(message or ""), kind="fatal")

    def _on_earnings(
        self, label: str, gold_delta: int, tickets_delta: int, xp_delta: int
    ) -> None:
        self._store.add_earnings(label, gold_delta, tickets_delta, xp_delta)

    def _on_manual_earnings(
        self, label: str, gold_delta: int, tickets_delta: int, xp_delta: int
    ) -> None:
        self._consume_manual_moves(label, 1)
        self._on_earnings(label, gold_delta, tickets_delta, xp_delta)

    def _on_worker_finished(self) -> None:
        with self._lock:
            self._worker = None
            self._store.set_worker_status(
                running=False,
                stopping=False,
                phase="stopped",
                started_at=None,
            )
            self._store.add_log("Фон остановлен.")

    def _on_manual_finished(self) -> None:
        with self._lock:
            self._manual_thread = None
            self._store.set_worker_status(manual_busy=False)

    def _on_code_finished(self) -> None:
        with self._lock:
            self._code_thread = None
            self._store.set_worker_status(code_busy=False)

    def _refresh_busy_flags(self) -> None:
        self._store.set_worker_status(
            running=self._worker is not None and self._worker.isRunning(),
            manual_busy=self._manual_thread is not None and self._manual_thread.isRunning(),
            code_busy=self._code_thread is not None and self._code_thread.isRunning(),
        )

    @staticmethod
    def _day_key_local() -> str:
        return datetime.now().strftime("%Y-%m-%d")

    def _moves_used_today(self, label: str) -> int:
        key = self._day_key_local()
        day, used = self._manual_moves_used_today.get(label, (key, 0))
        if day != key:
            self._manual_moves_used_today[label] = (key, 0)
            return 0
        return int(used)

    def _remaining_manual_moves(self, cfg: AppConfig, label: str) -> int:
        daily_budget = int(cfg.compliance.daily_move_budget)
        if daily_budget <= 0:
            return int(cfg.compliance.max_moves_per_session)
        return max(0, daily_budget - self._moves_used_today(label))

    def _consume_manual_moves(self, label: str, delta: int) -> None:
        if delta <= 0:
            return
        cfg = self._cfg
        if cfg is not None and int(cfg.compliance.daily_move_budget) <= 0:
            return
        key = self._day_key_local()
        used = self._moves_used_today(label)
        self._manual_moves_used_today[label] = (key, used + int(delta))
