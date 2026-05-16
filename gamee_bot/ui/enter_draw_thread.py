from __future__ import annotations

from PySide6.QtCore import QThread, Signal

from gamee_bot.account_store import load_accounts
from gamee_bot.client import GameeClient
from gamee_bot.config import AppConfig
from gamee_bot.proxy_url import normalize_and_validate_gamee_proxy
from gamee_bot.ui.account_action_thread import _format_micro_amount
from gamee_bot.worker import build_gamee_session_for_account


class EnterDrawThread(QThread):
    """Adds currently available XP to eligible Lucky Draws for every account."""

    log_line = Signal(str)

    def __init__(self, cfg: AppConfig, parent=None) -> None:
        super().__init__(parent)
        self._cfg = cfg

    def run(self) -> None:
        try:
            accounts = load_accounts(self._cfg.accounts_path)
        except Exception as e:
            self.log_line.emit(f"accounts.yaml: {e}")
            return
        if not accounts:
            self.log_line.emit("Нет аккаунтов в accounts.yaml.")
            return
        self.log_line.emit(f"Draw XP: аккаунтов: {len(accounts)}")
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
                res = client.enter_lucky_draw_with_available_xp(session)
                if res.entered:
                    amount = _format_micro_amount(
                        res.entered_micro,
                        self._cfg.gamee.reward_micro_divisor,
                    )
                    title = res.title or f"draw #{res.draw_id}"
                    action_text = (
                        f"добавлено {amount} XP"
                        if res.already_entered
                        else f"начато участие: внесено {amount} XP"
                    )
                    self.log_line.emit(
                        f"[{label}] ✓ Draw XP: {action_text} в {title} (drawId={res.draw_id})."
                    )
                else:
                    reason = res.reason or "нет доступной XP-раздачи"
                    self.log_line.emit(f"[{label}] Draw XP: {reason}.")
            except Exception as e:
                self.log_line.emit(f"[{label}] ✗ Draw XP: {e}")
            finally:
                if client is not None:
                    client.close()
        self.log_line.emit("Draw XP: готово.")
