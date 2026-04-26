from __future__ import annotations

from PySide6.QtCore import QThread, Signal

from gamee_bot.account_store import load_accounts
from gamee_bot.client import GameeClient
from gamee_bot.config import AppConfig
from gamee_bot.proxy_url import normalize_and_validate_gamee_proxy
from gamee_bot.worker import build_gamee_session_for_account


class EnterCodeThread(QThread):
    """Отправляет telegram.checkTask.code для каждого аккаунта (последовательно)."""

    log_line = Signal(str)

    def __init__(self, cfg: AppConfig, code: str, task_id: int, parent=None) -> None:
        super().__init__(parent)
        self._cfg = cfg
        self._code = (code or "").strip()
        self._task_id = int(task_id)

    def run(self) -> None:
        try:
            accounts = load_accounts(self._cfg.accounts_path)
        except Exception as e:
            self.log_line.emit(f"accounts.yaml: {e}")
            return
        if not self._code:
            self.log_line.emit("Код пустой.")
            return
        if not accounts:
            self.log_line.emit("Нет аккаунтов в accounts.yaml.")
            return
        self.log_line.emit(
            f"Промокод: «{self._code}» · taskId={self._task_id} · аккаунтов: {len(accounts)}"
        )
        for acc in accounts:
            label = acc.label
            client: GameeClient | None = None
            try:
                proxy = normalize_and_validate_gamee_proxy(acc.proxy_url)
                session = build_gamee_session_for_account(self._cfg, acc)
                client = GameeClient(
                    self._cfg.gamee,
                    proxy_url=proxy,
                    http_profile=session.http_profile,
                    account_label=acc.label,
                    cookie_base_dir=self._cfg.accounts_path.parent,
                )
                ok, msg = client.submit_check_task_code(
                    session, task_id=self._task_id, code=self._code
                )
                if ok:
                    self.log_line.emit(f"[{label}] ✓ {msg}")
                else:
                    self.log_line.emit(f"[{label}] ✗ {msg}")
            except Exception as e:
                self.log_line.emit(f"[{label}] ✗ {e}")
            finally:
                if client is not None:
                    client.close()
        self.log_line.emit("Готово.")
