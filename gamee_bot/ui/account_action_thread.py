from __future__ import annotations

from datetime import datetime
from time import monotonic

from PySide6.QtCore import QThread, Signal

from gamee_bot.account_store import AccountRecord, load_accounts
from gamee_bot.client import AccountGameState, GameeClient
from gamee_bot.config import AppConfig, gamee_proxy_table_summary, local_time_in_quiet_hours
from gamee_bot.preview_loader import _daily_checkin_preview_row, _season_pass_preview_cell
from gamee_bot.proxy_url import normalize_and_validate_gamee_proxy
from gamee_bot.worker import MIN_ENERGY_TO_PLAY, build_gamee_session_for_account


class AccountActionThread(QThread):
    """Ручные действия по одному аккаунту: sync, daily claim, limited play session."""

    log_line = Signal(str)
    row_ready = Signal(object)
    move_earned = Signal(str, int, int, int)

    def __init__(
        self,
        cfg: AppConfig,
        label: str,
        action: str,
        *,
        move_limit: int = 0,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._cfg = cfg
        self._label = label.strip()
        self._action = action.strip()
        self._move_limit = max(0, int(move_limit))

    def _account(self) -> AccountRecord:
        accounts = load_accounts(self._cfg.accounts_path)
        acc = next((a for a in accounts if a.label == self._label), None)
        if acc is None:
            raise ValueError(f"Аккаунт «{self._label}» не найден в accounts.yaml.")
        return acc

    def _emit_row(
        self,
        client: GameeClient,
        session,
        acc: AccountRecord,
        state: AccountGameState,
        *,
        status_override: str | None = None,
        last_move_at: str = "",
    ) -> None:
        px_cell, px_tip = gamee_proxy_table_summary(acc.proxy_url)
        status = status_override or "статистика с сервера"
        dc_note = ""
        dc_iso = None
        dc_streak = 0
        dc_streak_total = 0
        season_txt = "—"
        if state.last_error:
            err = state.last_error.strip() or "ошибка"
            if len(err) > 90:
                err = err[:87] + "..."
            status = f"ошибка: {err}"
        else:
            dc_note, dc_iso, dc_streak, dc_streak_total = _daily_checkin_preview_row(client, session)
            season_txt = _season_pass_preview_cell(client, session, self._cfg)
        self.row_ready.emit(
            {
                "label": acc.label,
                "energy": state.energy,
                "gold": state.gold,
                "gold_estimated_usd": state.gold_estimated_usd,
                "status": status,
                "last_error": state.last_error or "",
                "last_move_at": last_move_at,
                "regen_deadline_iso": state.next_live_at_utc.isoformat()
                if state.next_live_at_utc is not None
                else None,
                "daily_claim_rewards_text": dc_note,
                "daily_checkin_deadline_iso": dc_iso,
                "daily_checkin_streak": dc_streak,
                "daily_checkin_streak_total": dc_streak_total,
                "season_rewards_text": season_txt,
                "proxy_cell": px_cell,
                "proxy_tooltip": px_tip,
            }
        )

    @staticmethod
    def _looks_like_http_guard_error(msg: str) -> bool:
        low = (msg or "").lower()
        return any(x in low for x in ("403", "429", "502", "503", "504"))

    def run(self) -> None:
        client: GameeClient | None = None
        try:
            acc = self._account()
            proxy = normalize_and_validate_gamee_proxy(acc.proxy_url)
            session = build_gamee_session_for_account(self._cfg, acc)
            client = GameeClient(
                self._cfg.gamee,
                proxy_url=proxy,
                http_profile=session.http_profile,
            )
            if self._action == "sync":
                state = client.get_assets_state(session)
                self._emit_row(client, session, acc, state)
                self.log_line.emit(f"[{acc.label}] Sync now завершён.")
                return

            if self._action == "claim_daily":
                ok, msg, _snap = client.claim_daily_checkin(session)
                state = client.get_assets_state(session)
                if ok:
                    note = (msg or "").strip() or "OK"
                    self.log_line.emit(f"[{acc.label}] Ежедневная награда: {note}")
                    self._emit_row(client, session, acc, state, status_override="ежедневная награда выполнена")
                else:
                    self.log_line.emit(f"[{acc.label}] Ежедневная награда не взята: {msg}")
                    self._emit_row(client, session, acc, state)
                return

            if self._action != "play_session":
                raise ValueError(f"Неизвестное действие: {self._action!r}")

            comp = self._cfg.compliance
            if local_time_in_quiet_hours(
                comp.quiet_hours_enabled,
                comp.quiet_hours_start_hour,
                comp.quiet_hours_end_hour,
            ):
                state = client.get_assets_state(session)
                self.log_line.emit(f"[{acc.label}] Ручная серия ходов заблокирована quiet hours.")
                self._emit_row(client, session, acc, state, status_override="quiet hours")
                return
            if self._move_limit <= 0:
                state = client.get_assets_state(session)
                self.log_line.emit(f"[{acc.label}] Дневной бюджет ходов исчерпан.")
                self._emit_row(client, session, acc, state, status_override="лимит ходов на сегодня")
                return

            deadline = monotonic() + comp.session_duration_minutes * 60.0
            state = client.get_assets_state(session)
            self._emit_row(client, session, acc, state, status_override="ручная серия ходов")
            if state.last_error:
                self.log_line.emit(f"[{acc.label}] Sync перед ходами не удался: {state.last_error}")
                return
            move_idx = 0
            error_streak = 0
            while (
                not self.isInterruptionRequested()
                and move_idx < self._move_limit
                and monotonic() < deadline
                and state.energy >= MIN_ENERGY_TO_PLAY
            ):
                outcome = client.play_board(session)
                if not outcome.ok or outcome.after is None:
                    error_streak += 1
                    err = (outcome.error or "ошибка").strip() or "ошибка"
                    self.log_line.emit(f"[{acc.label}] Серия ходов остановлена: {err}")
                    if (
                        self._looks_like_http_guard_error(err)
                        and error_streak >= comp.stop_after_error_streak
                    ):
                        self.log_line.emit(
                            f"[{acc.label}] Guardrail: серия HTTP-ошибок достигла лимита, сессию прерываем."
                        )
                    break
                move_idx += 1
                error_streak = 0
                before = outcome.before
                after = outcome.after
                state = after
                reward_line = outcome.rewards_text if outcome.rewards_text.strip() not in ("", "—") else "ничего"
                dice_s = str(outcome.dice_value) if outcome.dice_value is not None else "?"
                self.log_line.emit(
                    f"[{acc.label}] Ход #{move_idx}: выпало {dice_s}, награда {reward_line}, "
                    f"энергия {before.energy}->{after.energy}, золото {before.gold}->{after.gold}."
                )
                self.move_earned.emit(
                    acc.label,
                    after.gold - before.gold,
                    after.tickets - before.tickets,
                    outcome.xp_gained,
                )
            if move_idx >= self._move_limit:
                self.log_line.emit(
                    f"[{acc.label}] Серия ходов завершена: достигнут лимит {self._move_limit} ходов."
                )
            elif monotonic() >= deadline:
                self.log_line.emit(
                    f"[{acc.label}] Серия ходов завершена: достигнут лимит длительности сессии."
                )
            elif state.energy < MIN_ENERGY_TO_PLAY:
                self.log_line.emit(
                    f"[{acc.label}] Серия ходов завершена: энергии меньше {MIN_ENERGY_TO_PLAY}."
                )
            last_move_at = datetime.now().strftime("%Y.%m.%d %H:%M") if move_idx > 0 else ""
            final_state = client.get_assets_state(session)
            self._emit_row(
                client,
                session,
                acc,
                final_state,
                status_override="ручная серия завершена",
                last_move_at=last_move_at,
            )
            return
        except Exception as e:
            self.log_line.emit(f"[{self._label}] {e}")
        finally:
            if client is not None:
                client.close()
