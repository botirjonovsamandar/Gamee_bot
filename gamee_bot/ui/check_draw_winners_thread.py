from __future__ import annotations

from PySide6.QtCore import QThread, Signal

from gamee_bot.account_store import load_accounts
from gamee_bot.client import AccountGameState, GameeClient
from gamee_bot.config import AppConfig
from gamee_bot.proxy_url import normalize_and_validate_gamee_proxy
from gamee_bot.worker import build_gamee_session_for_account


def _balance_text(state: AccountGameState | None) -> str:
    if state is None:
        return "unknown"
    return f"gold {state.gold}, tickets {state.tickets}, energy {state.energy}"


def _balance_delta_text(before: AccountGameState | None, after: AccountGameState | None) -> str:
    if before is None or after is None:
        return "unknown"
    parts: list[str] = []
    for label, delta in (
        ("gold", after.gold - before.gold),
        ("tickets", after.tickets - before.tickets),
        ("energy", after.energy - before.energy),
    ):
        if delta:
            sign = "+" if delta > 0 else ""
            parts.append(f"{label} {sign}{delta}")
    return ", ".join(parts) if parts else "no change"


class CheckDrawWinnersThread(QThread):
    """Checks draw.getWinners(drawId) for every account and logs own ranking/reward."""

    log_line = Signal(str)

    def __init__(self, cfg: AppConfig, draw_id: int, parent=None) -> None:
        super().__init__(parent)
        self._cfg = cfg
        self._draw_id = int(draw_id)

    def run(self) -> None:
        try:
            accounts = load_accounts(self._cfg.accounts_path)
        except Exception as e:
            self.log_line.emit(f"accounts.yaml: {e}")
            return
        if self._draw_id <= 0:
            self.log_line.emit("Draw winners: drawId must be positive.")
            return
        if not accounts:
            self.log_line.emit("Нет аккаунтов в accounts.yaml.")
            return
        self.log_line.emit(
            f"Draw winners: drawId={self._draw_id}, аккаунтов: {len(accounts)}"
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
                before = client.get_assets_state(session)
                if before.last_error:
                    self.log_line.emit(
                        f"[{label}] Draw winners: баланс до проверки не прочитан: {before.last_error}"
                    )
                    before_state: AccountGameState | None = None
                else:
                    before_state = before

                res = client.check_lucky_draw_winner(session, draw_id=self._draw_id)

                after = client.get_assets_state(session)
                after_state: AccountGameState | None = None if after.last_error else after
                balance_note = (
                    f"баланс до: {_balance_text(before_state)}; "
                    f"после: {_balance_text(after_state)}; "
                    f"изменение: {_balance_delta_text(before_state, after_state)}"
                )
                who = f" ({res.user_display})" if res.user_display else ""
                if res.won:
                    rank = f"#{res.rank}" if res.rank is not None else "?"
                    line = (
                        f"[{label}] ✓ Draw {res.draw_id} {res.title}: выиграл, "
                        f"rank {rank}{who}, reward {res.reward_text}; {balance_note}"
                    )
                    if _balance_delta_text(before_state, after_state) == "no change":
                        line += "; баланс не изменился во время проверки"
                    self.log_line.emit(line)
                else:
                    self.log_line.emit(
                        f"[{label}] Draw {res.draw_id} {res.title}: не выиграл{who}; "
                        f"winners в ответе: {res.winners_count}; {balance_note}"
                    )
            except Exception as e:
                self.log_line.emit(f"[{label}] ✗ Draw winners: {e}")
            finally:
                if client is not None:
                    client.close()
        self.log_line.emit("Draw winners: готово.")
