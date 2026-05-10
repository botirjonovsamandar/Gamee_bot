from __future__ import annotations

import gc
from typing import Any

from PySide6.QtCore import QThread, Signal

from gamee_bot.account_store import load_accounts
from gamee_bot.client import GameeClient, GameeSession
from gamee_bot.daily_schedule import daily_available_by_schedule, next_daily_reset_utc
from gamee_bot.http_profile import gamee_http_profile_for_label
from gamee_bot.config import (
    AppConfig,
    gamee_proxy_table_summary,
    resolve_account_telegram_referral_ref,
)
from gamee_bot.proxy_url import normalize_and_validate_gamee_proxy
from gamee_bot.telethon_bridge import resolve_init_data


def _season_pass_preview_cell(client: GameeClient, session: GameeSession, cfg) -> str:
    """Только просмотр прогресса Season Pass (без клейма), как при открытии софта."""
    try:
        prog = client.get_season_pass_progress(session)
    except Exception:
        return "—"
    if prog is None:
        return "—"
    return prog.to_cell(cfg.gamee, "")


def _daily_checkin_preview_row(
    client: GameeClient, session: GameeSession
) -> tuple[str, str | None, int, int]:
    """Без клейма: подсказка, ISO таймера, streak и длина цикла (напр. 1/14)."""
    if daily_available_by_schedule():
        return "проверка при ходах", None, 0, 0
    return "ожидание 17:00 UZ", next_daily_reset_utc().isoformat(), 0, 0


class AccountsPreviewLoader(QThread):
    """Проход по accounts.yaml — только getAssets, без ходов (не блокирует UI).

    Если задан only_labels — запросы к API только для этих аккаунтов (остальные строки
    таблицы подставляются из кэша в MainWindow).

    После каждого обработанного аккаунта испускается row_ready — чтобы таблица
    обновлялась по мере готовности данных.
    """

    finished_ok = Signal(list)
    failed = Signal(str)
    row_ready = Signal(object)

    def __init__(
        self,
        cfg: AppConfig,
        parent=None,
        *,
        only_labels: frozenset[str] | None = None,
    ) -> None:
        super().__init__(parent)
        self._cfg = cfg
        self._only_labels = only_labels

    def run(self) -> None:
        gc.disable()
        try:
            self._run_inner()
        finally:
            gc.enable()

    def _run_inner(self) -> None:
        rows: list[dict[str, Any]] = []

        def add_row(row: dict[str, Any]) -> None:
            rows.append(row)
            self.row_ready.emit(dict(row))

        try:
            accounts = load_accounts(self._cfg.accounts_path)
        except Exception as e:
            self.failed.emit(str(e))
            return
        if self._only_labels is not None:
            accounts = [a for a in accounts if a.label in self._only_labels]
        if not accounts:
            self.finished_ok.emit(rows)
            return
        for acc in accounts:
            px_cell, px_tip = gamee_proxy_table_summary(acc.proxy_url)
            try:
                pxy = normalize_and_validate_gamee_proxy(acc.proxy_url)
            except ValueError as e:
                err = str(e).strip() or "ошибка прокси"
                if len(err) > 120:
                    err = err[:117] + "…"
                add_row(
                    {
                        "label": acc.label,
                        "energy": 0,
                        "gold": 0,
                        "gold_estimated_usd": None,
                        "status": f"прокси: {err}",
                        "last_error": err,
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
                continue
            try:
                prof = gamee_http_profile_for_label(acc.label)
                client = GameeClient(
                    self._cfg.gamee, proxy_url=pxy, http_profile=prof,
                    account_label=acc.label,
                    cookie_base_dir=self._cfg.accounts_path.parent,
                )
            except Exception as e:
                err = str(e).strip() or "ошибка инициализации клиента"
                if len(err) > 120:
                    err = err[:117] + "…"
                add_row(
                    {
                        "label": acc.label,
                        "energy": 0,
                        "gold": 0,
                        "gold_estimated_usd": None,
                        "status": f"клиент: {err}",
                        "last_error": err,
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
                continue
            try:
                try:
                    init = resolve_init_data(
                        acc.label,
                        acc.init_data or "",
                        acc.telethon_session,
                        self._cfg,
                        account_gamee_ref=acc.gamee_ref
                        if not acc.gamee_preexisting
                        else None,
                    )
                except Exception as e:
                    msg = str(e).strip() or "ошибка"
                    if len(msg) > 90:
                        msg = msg[:87] + "..."
                    add_row(
                        {
                            "label": acc.label,
                            "energy": 0,
                            "gold": 0,
                            "gold_estimated_usd": None,
                            "status": f"ошибка: {msg}",
                            "last_error": msg,
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
                    continue
                s = GameeSession(
                    init_data=init,
                    install_uuid=acc.install_uuid,
                    http_profile=prof,
                    telegram_referral_ref=resolve_account_telegram_referral_ref(
                        self._cfg,
                        acc.telegram_referral_ref
                        if not acc.gamee_preexisting
                        else None,
                    ),
                    referral_linked=False,
                    accounts_yaml_path=self._cfg.accounts_path,
                    account_label=acc.label,
                )
                try:
                    st = client.get_assets_state(s)
                    if st.last_error:
                        err = st.last_error.strip() or "ошибка"
                        if len(err) > 90:
                            err = err[:87] + "..."
                        status = f"ошибка: {err}"
                    else:
                        status = "статистика с сервера"
                    dc_note, dc_iso, dc_streak, dc_streak_tot = (
                        _daily_checkin_preview_row(client, s)
                        if not st.last_error
                        else ("—", None, 0, 0)
                    )
                    season_txt = (
                        _season_pass_preview_cell(client, s, self._cfg)
                        if not st.last_error
                        else "—"
                    )
                    add_row(
                        {
                            "label": acc.label,
                            "energy": st.energy,
                            "gold": st.gold,
                            "gold_estimated_usd": st.gold_estimated_usd,
                            "status": status,
                            "last_error": st.last_error or "",
                            "last_move_at": "",
                            "regen_deadline_iso": st.next_live_at_utc.isoformat()
                            if st.next_live_at_utc is not None
                            else None,
                            "daily_claim_rewards_text": dc_note,
                            "daily_checkin_deadline_iso": dc_iso,
                            "daily_checkin_streak": dc_streak,
                            "daily_checkin_streak_total": dc_streak_tot,
                            "season_rewards_text": season_txt,
                            "proxy_cell": px_cell,
                            "proxy_tooltip": px_tip,
                        }
                    )
                except Exception as e:
                    msg = str(e).strip() or "ошибка"
                    if len(msg) > 90:
                        msg = msg[:87] + "..."
                    add_row(
                        {
                            "label": acc.label,
                            "energy": 0,
                            "gold": 0,
                            "gold_estimated_usd": None,
                            "status": f"ошибка: {msg}",
                            "last_error": msg,
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
            finally:
                client.close()
        self.finished_ok.emit(rows)
