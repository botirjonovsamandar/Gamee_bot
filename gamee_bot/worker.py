from __future__ import annotations

import gc
import random
import threading
import traceback
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from time import monotonic, sleep
from typing import Any

from PySide6.QtCore import QThread, Signal

from gamee_bot.account_store import AccountRecord, load_accounts
from gamee_bot.client import AccountGameState, GameeClient, GameeSession
from gamee_bot.http_profile import gamee_http_profile_for_label
from gamee_bot.config import (
    BACKGROUND_MODE_FULL_AUTO,
    BACKGROUND_MODE_MANUAL_ONLY,
    BACKGROUND_MODE_READ_ONLY,
    AppConfig,
    gamee_proxy_table_summary,
    local_time_in_quiet_hours,
    resolve_account_telegram_referral_ref,
)
from gamee_bot.proxy_url import normalize_and_validate_gamee_proxy
from gamee_bot.notify import TelegramNotifier
from gamee_bot.telegram_messages import (
    format_daily_claim_message,
    format_season_claim_message,
    format_summary_message,
)
from gamee_bot.telethon_bridge import resolve_init_data


def build_gamee_session_for_account(cfg: AppConfig, acc: AccountRecord) -> GameeSession:
    """Новая сессия без кеша токена — для разовых действий (промокод и т.д.)."""
    yaml_path = cfg.accounts_path
    gr = None if acc.gamee_preexisting else acc.gamee_ref
    tr = None if acc.gamee_preexisting else acc.telegram_referral_ref
    resolved = resolve_init_data(
        acc.label,
        acc.init_data or "",
        acc.telethon_session,
        cfg,
        account_gamee_ref=gr,
    )
    ref_id = resolve_account_telegram_referral_ref(cfg, tr)
    prof = gamee_http_profile_for_label(acc.label)
    return GameeSession(
        init_data=resolved,
        install_uuid=acc.install_uuid,
        http_profile=prof,
        auth_token=None,
        money_usd_cents=0,
        telegram_referral_ref=ref_id,
        referral_linked=False,
        accounts_yaml_path=yaml_path,
        account_label=acc.label,
    )


# Ходим, пока энергия ≥ этого порога; ожидание между циклами — по next_live с сервера + запас.
MIN_ENERGY_TO_PLAY = 5
POST_NEXT_LIVE_POLL_SLACK_SEC = 5
BACKGROUND_READ_IDLE_SEC = 90.0
# После сбоя не ждать «до регена» часами — быстрый повтор (смена прокси подхватывается в этом же цикле).
_ERROR_RETRY_IDLE_SEC = 5.0
SUPERVISOR_POLL_SEC = 2.0
POST_DAILY_DONE_IDLE_SEC = 8.0
# Пауза между стартом потоков аккаунтов (порядок как в accounts.yaml), чтобы не вшмыть API/UI разом.
# Conservative stagger: official-grade pacing between account thread starts.
ACCOUNT_THREAD_START_STAGGER_SEC = 2.0
ACCOUNT_THREAD_START_STAGGER_JITTER_SEC = 3.0
# Одновременно не более N потоков аккаунтов в rewardedProgress (иначе прокси/API «задыхаются», все в SSL read).
_SEASON_API_MAX_PARALLEL = 5
_MAX_ERROR_BACKOFF_SEC = 90.0
_SEASON_SYNC_MIN_INTERVAL_SEC = 45.0
_IDLE_JITTER_SEC = 3.0

STATUS_IDLE = "ожидание"
STATUS_SYNCING = "синхронизация…"
STATUS_DAILY_IN_PROGRESS = "ежедневная награда…"
STATUS_DAILY_DONE = "ежедневная награда выполнена"


@dataclass
class RowState:
    label: str
    energy: int
    gold: int
    usd_cents: int
    gold_estimated_usd: float | None = None
    status: str = ""
    last_move_at: str = ""
    last_error: str = ""
    regen_deadline_utc: datetime | None = None
    daily_claim_rewards_text: str = ""
    daily_bot_claim_day_key: str = ""
    daily_checkin_deadline_iso: str | None = None
    daily_checkin_streak: int = 0
    daily_checkin_streak_total: int = 0
    season_rewards_text: str = ""
    proxy_cell: str = "—"
    proxy_tooltip: str = ""


def _local_time_last_move() -> str:
    """Время компьютера без указания часового пояса: ГГГГ.MM.DD ЧЧ:ММ"""
    return datetime.now().strftime("%Y.%m.%d %H:%M")


class BotWorker(QThread):
    """Фоновый цикл: каждый аккаунт в своём потоке, ходы не блокируют друг друга."""

    table_updated = Signal(list)
    log_message = Signal(str)
    fatal_error = Signal(str)
    session_earnings_move = Signal(str, int, int, int)

    def __init__(self, cfg: AppConfig, parent=None) -> None:
        super().__init__(parent)
        self._cfg = cfg
        self._running = True
        self._sessions: dict[str, GameeSession] = {}
        self._rows: dict[str, RowState] = {}
        self._table_label_order: list[str] = []
        self._wake_nonce = 0
        self._wake_labels: dict[str, int] = {}
        self._wake_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._notifier_lock = threading.Lock()
        self._account_threads: dict[str, threading.Thread] = {}
        self._notifier: TelegramNotifier | None = None
        self._season_api_semaphore = threading.Semaphore(_SEASON_API_MAX_PARALLEL)
        self._telegram_notify_lock = threading.Lock()
        self._telegram_notify_enabled = True
        self._error_streaks: dict[str, int] = {}
        self._error_cooldown_until: dict[str, float] = {}
        self._season_sync_due_at: dict[str, float] = {}
        self._started_at_monotonic = 0.0

    def set_telegram_notify_enabled(self, enabled: bool) -> None:
        """Вкл/выкл отправку уведомлений бота в Telegram (ходы, ежедневка, сезон, сводка)."""
        with self._telegram_notify_lock:
            self._telegram_notify_enabled = bool(enabled)

    def _telegram_notify_ok(self) -> bool:
        with self._telegram_notify_lock:
            return self._telegram_notify_enabled

    def stop(self) -> None:
        self._running = False

    def wake_idle(self, label: str | None = None) -> None:
        """Разбудить ожидания всех или одного аккаунта."""
        with self._wake_lock:
            if label is not None and label.strip():
                key = label.strip()
                self._wake_labels[key] = self._wake_labels.get(key, 0) + 1
                return
            self._wake_nonce += 1

    def _sleep_interruptible(
        self, total_sec: float, step: float = 0.2, *, label: str | None = None
    ) -> None:
        if total_sec <= 0:
            return
        start_nonce = self._get_wake_nonce()
        start_label_nonce = self._get_label_wake_nonce(label) if label else 0
        deadline = monotonic() + total_sec
        while self._running:
            if self._get_wake_nonce() != start_nonce:
                return
            if label and self._get_label_wake_nonce(label) != start_label_nonce:
                return
            remain = deadline - monotonic()
            if remain <= 0:
                break
            sleep(min(step, remain))

    def _get_wake_nonce(self) -> int:
        with self._wake_lock:
            return self._wake_nonce

    def _get_label_wake_nonce(self, label: str | None) -> int:
        if not label:
            return 0
        with self._wake_lock:
            return self._wake_labels.get(label, 0)

    def _note_account_error(self, label: str) -> None:
        cur = self._error_streaks.get(label, 0)
        nxt = min(cur + 1, 8)
        self._error_streaks[label] = nxt
        if nxt >= self._cfg.compliance.stop_after_error_streak:
            self._error_cooldown_until[label] = monotonic() + float(
                self._cfg.compliance.error_cooldown_seconds
            )

    def _note_account_success(self, label: str) -> None:
        self._error_streaks.pop(label, None)
        self._error_cooldown_until.pop(label, None)

    def _background_mode(self) -> str:
        return self._cfg.compliance.background_mode

    def _background_mode_allows_claims(self) -> bool:
        return self._background_mode() == BACKGROUND_MODE_FULL_AUTO

    def _background_mode_allows_sync(self) -> bool:
        return self._background_mode() in (BACKGROUND_MODE_READ_ONLY, BACKGROUND_MODE_FULL_AUTO)

    def _background_guard_reason(self, label: str) -> str | None:
        if not self._background_mode_allows_sync():
            return "manual-first: фон отключён"
        c = self._cfg.compliance
        if local_time_in_quiet_hours(
            c.quiet_hours_enabled,
            c.quiet_hours_start_hour,
            c.quiet_hours_end_hour,
        ):
            return "quiet hours"
        until = self._error_cooldown_until.get(label, 0.0)
        now = monotonic()
        if now < until:
            remain = max(1, int(round(until - now)))
            return f"cooldown после ошибок ({remain} c)"
        return None

    def _reserve_season_sync(self, label: str, *, force: bool = False) -> bool:
        now = monotonic()
        due_at = self._season_sync_due_at.get(label, 0.0)
        if not force and now < due_at:
            return False
        self._season_sync_due_at[label] = (
            now
            + _SEASON_SYNC_MIN_INTERVAL_SEC
            + random.uniform(0.0, 8.0)
        )
        return True

    def _account_record_for_label(self, label: str) -> AccountRecord | None:
        try:
            accounts = load_accounts(self._cfg.accounts_path)
        except Exception:
            return None
        return next((a for a in accounts if a.label == label), None)

    @staticmethod
    def _row_needs_quick_retry_after_error(row: RowState) -> bool:
        """Ошибка/исключение: не использовать длинное ожидание по regen (иначе новый прокси «висит» до часа)."""
        st = (row.status or "").strip()
        if st in (
            "исключение",
            "ошибка входа",
            "сбой цикла",
            "ход не удался",
        ):
            return True
        if st.startswith("ошибка:"):
            return True
        return False

    def _idle_sleep_seconds_for_row(self, label: str, row: RowState) -> float:
        """Пауза одного аккаунта до следующего опроса."""
        if self._row_needs_quick_retry_after_error(row):
            streak = max(1, self._error_streaks.get(label, 1))
            base = min(_ERROR_RETRY_IDLE_SEC * (2 ** (streak - 1)), _MAX_ERROR_BACKOFF_SEC)
            return float(base + random.uniform(0.0, min(4.0, max(1.0, base * 0.2))))
        now = datetime.now(timezone.utc)
        if row.energy >= MIN_ENERGY_TO_PLAY:
            return float(BACKGROUND_READ_IDLE_SEC + random.uniform(0.0, 12.0))
        if row.regen_deadline_utc is None:
            return 60.0 + random.uniform(0.0, 5.0)
        at = row.regen_deadline_utc
        if at.tzinfo is None:
            at = at.replace(tzinfo=timezone.utc)
        sec = (at + timedelta(seconds=POST_NEXT_LIVE_POLL_SLACK_SEC) - now).total_seconds()
        return max(float(POST_NEXT_LIVE_POLL_SLACK_SEC), sec) + random.uniform(0.0, _IDLE_JITTER_SEC)

    def _join_finished_threads(self, alive_labels: set[str]) -> None:
        for dead in list(self._account_threads.keys()):
            if dead not in alive_labels:
                t = self._account_threads.pop(dead)
                t.join(timeout=120.0)

    def _stop_all_account_threads(self) -> None:
        for _label, t in list(self._account_threads.items()):
            t.join(timeout=120.0)
        self._account_threads.clear()

    def run(self) -> None:
        notifier = TelegramNotifier(self._cfg.telegram.bot_token, self._cfg.telegram.chat_id)
        self._notifier = notifier
        last_summary = monotonic()
        self._started_at_monotonic = monotonic()
        try:
            while self._running:
                if (
                    self._cfg.compliance.session_duration_minutes > 0
                    and monotonic() - self._started_at_monotonic
                    >= self._cfg.compliance.session_duration_minutes * 60.0
                ):
                    self.log_message.emit(
                        "Фоновая сессия остановлена: достигнут лимит длительности из настроек."
                    )
                    self._running = False
                    break
                try:
                    accounts = load_accounts(self._cfg.accounts_path)
                except Exception as e:
                    self.fatal_error.emit(f"accounts.yaml: {e}")
                    self._sleep_interruptible(5)
                    continue

                if not accounts:
                    self._stop_all_account_threads()
                    with self._state_lock:
                        self._table_label_order.clear()
                        self._rows.clear()
                        self._sessions.clear()
                        self._error_streaks.clear()
                        self._error_cooldown_until.clear()
                        self._season_sync_due_at.clear()
                        self._wake_labels.clear()
                    self._emit_table()
                    self._sleep_interruptible(10.0)
                    continue

                alive_labels = {a.label for a in accounts}
                self._join_finished_threads(alive_labels)

                with self._state_lock:
                    self._table_label_order = [a.label for a in accounts]
                    for dead in list(self._rows.keys()):
                        if dead not in alive_labels:
                            del self._rows[dead]
                            self._error_streaks.pop(dead, None)
                            self._error_cooldown_until.pop(dead, None)
                            self._season_sync_due_at.pop(dead, None)
                            self._wake_labels.pop(dead, None)
                    for dead in list(self._sessions.keys()):
                        if dead not in alive_labels:
                            del self._sessions[dead]
                            self._error_streaks.pop(dead, None)
                            self._error_cooldown_until.pop(dead, None)
                            self._season_sync_due_at.pop(dead, None)
                            self._wake_labels.pop(dead, None)
                    for acc in accounts:
                        if acc.label not in self._rows:
                            self._rows[acc.label] = RowState(
                                label=acc.label,
                                energy=0,
                                gold=0,
                                usd_cents=0,
                                status=STATUS_SYNCING,
                            )

                pending_start = [acc for acc in accounts if acc.label not in self._account_threads]
                for i, acc in enumerate(pending_start):
                    t = threading.Thread(
                        target=self._account_thread_main,
                        args=(acc.label,),
                        name=f"gamee-{acc.label}",
                        daemon=True,
                    )
                    self._account_threads[acc.label] = t
                    t.start()
                    if i + 1 < len(pending_start):
                        self._sleep_interruptible(
                            ACCOUNT_THREAD_START_STAGGER_SEC
                            + random.uniform(0.0, ACCOUNT_THREAD_START_STAGGER_JITTER_SEC)
                        )

                self._emit_table()

                now_m = monotonic()
                interval = self._cfg.telegram.summary_interval_seconds
                if (
                    notifier.enabled()
                    and self._telegram_notify_ok()
                    and interval > 0
                    and now_m - last_summary >= interval
                ):
                    with self._state_lock:
                        any_syncing = any(
                            r.status == STATUS_SYNCING for r in self._rows.values()
                        )
                    if not any_syncing:
                        with self._notifier_lock:
                            self._send_summary(notifier)
                        last_summary = now_m

                self._sleep_interruptible(SUPERVISOR_POLL_SEC)
        finally:
            self._running = False
            self._stop_all_account_threads()
            self._notifier = None
            notifier.close()

    def _account_thread_main(self, label: str) -> None:
        gc.disable()
        try:
            self._account_thread_inner(label)
        finally:
            gc.enable()

    def _account_thread_inner(self, label: str) -> None:
        client: GameeClient | None = None
        bound_proxy: object | None = None
        notifier = self._notifier
        if notifier is None:
            return
        try:
            while self._running:
                try:
                    accounts = load_accounts(self._cfg.accounts_path)
                except Exception:
                    self._sleep_interruptible(5.0, label=label)
                    continue
                acc = next((a for a in accounts if a.label == label), None)
                if acc is None:
                    break
                try:
                    want_proxy = normalize_and_validate_gamee_proxy(acc.proxy_url)
                except ValueError as e:
                    self.log_message.emit(f"[{label}] Прокси: {e}")
                    self._note_account_error(label)
                    self._sleep_interruptible(25.0, label=label)
                    continue
                if client is None or bound_proxy != want_proxy:
                    if client is not None:
                        client.close()
                    try:
                        client = GameeClient(
                            self._cfg.gamee,
                            proxy_url=want_proxy,
                            http_profile=gamee_http_profile_for_label(label),
                        )
                    except Exception as e:
                        msg = str(e).strip() or repr(e)
                        self.log_message.emit(f"[{label}] Инициализация клиента: {msg}")
                        client = None
                        bound_proxy = None
                        self._note_account_error(label)
                        self._sleep_interruptible(25.0, label=label)
                        continue
                    bound_proxy = want_proxy
                try:
                    self._sync_account(client, notifier, acc)
                except Exception as e:
                    self.log_message.emit(
                        f"[{label}] Сбой цикла аккаунта (продолжаем попытки): "
                        f"{e}\n{traceback.format_exc()}"
                    )
                    with self._state_lock:
                        row = self._rows.get(label) or RowState(
                            label=label, energy=0, gold=0, usd_cents=0
                        )
                        row.status = "сбой цикла"
                        row.last_error = str(e).strip() or repr(e)
                        self._rows[label] = row
                    self._note_account_error(label)
                    self._emit_table()
                    self._sleep_interruptible(15.0, label=label)
                    continue
                with self._state_lock:
                    row = self._rows.get(label)
                idle = self._idle_sleep_seconds_for_row(label, row) if row is not None else 10.0
                self._sleep_interruptible(idle, label=label)
        finally:
            if client is not None:
                client.close()

    def _session_for(self, acc: AccountRecord) -> GameeSession:
        yaml_path = self._cfg.accounts_path
        fresh = build_gamee_session_for_account(self._cfg, acc)
        resolved = fresh.init_data
        ref_id = fresh.telegram_referral_ref
        s = self._sessions.get(acc.label)
        if s is None or s.init_data != resolved:
            self._sessions[acc.label] = fresh
            return fresh
        s = self._sessions[acc.label]
        if s.telegram_referral_ref != ref_id:
            s.telegram_referral_ref = ref_id
            s.referral_linked = False
        s.install_uuid = acc.install_uuid
        s.http_profile = fresh.http_profile
        s.accounts_yaml_path = yaml_path
        s.account_label = acc.label
        return s

    def _apply_daily_checkin(
        self,
        client: GameeClient,
        session: GameeSession,
        label: str,
        *,
        allow_claim: bool,
    ) -> None:
        snap = client.get_daily_checkin_snapshot(session)
        day_key_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        with self._state_lock:
            row = self._rows.get(label)
            persisted_rw = (row.daily_claim_rewards_text if row else "") or ""
            persisted_day = (row.daily_bot_claim_day_key if row else "") or ""

        iso: str | None = None
        last_rw = ""
        bot_day = ""

        if snap.api_error:
            self._note_account_error(label)
            last_rw = "ежедн. — ошибка"
        elif snap.claimed_today:
            if persisted_day == day_key_utc and persisted_rw:
                last_rw = persisted_rw
                bot_day = persisted_day
        else:
            last_rw = ""

        will_try_claim = (
            not snap.api_error and allow_claim and not snap.claimed_today
        )
        if will_try_claim:
            with self._state_lock:
                row = self._rows.get(label)
                if row is not None:
                    row.status = STATUS_DAILY_IN_PROGRESS
                    self._rows[label] = row
            self._emit_table()

        if not snap.api_error and allow_claim and not snap.claimed_today:
            ok, rw, snap2 = client.claim_daily_checkin(session)
            if ok:
                last_rw = rw if rw.strip() not in ("", "—") else "OK"
                bot_day = day_key_utc
                self.log_message.emit(f"[{label}] Ежедневная награда: {last_rw}")
                snap = snap2 or client.get_daily_checkin_snapshot(session)
                streak_n, streak_tot = 0, 0
                if snap is not None and not snap.api_error:
                    streak_n, streak_tot = snap.streak, snap.streak_total
                n = self._notifier
                if (
                    n is not None
                    and n.enabled()
                    and self._telegram_notify_ok()
                    and self._cfg.telegram.notify_on_daily_claim
                ):
                    t_daily = format_daily_claim_message(
                        label=label,
                        rewards_line=last_rw,
                        streak=streak_n,
                        streak_total=streak_tot,
                    )
                    with self._notifier_lock:
                        n.send(t_daily)
                with self._state_lock:
                    row = self._rows.get(label)
                    if row is not None:
                        row.status = STATUS_DAILY_DONE
                        self._rows[label] = row
                self._emit_table()
                self._sleep_interruptible(POST_DAILY_DONE_IDLE_SEC, label=label)
                with self._state_lock:
                    row = self._rows.get(label)
                    if row is not None:
                        row.status = STATUS_IDLE
                        self._rows[label] = row
                self._emit_table()
            else:
                err_hint = (rw or "").strip() or "клейм не удался"
                self.log_message.emit(f"[{label}] Ежедневная награда не взята: {err_hint}")
                snap = client.get_daily_checkin_snapshot(session)
                if snap.claimed_today and persisted_day == day_key_utc and persisted_rw:
                    last_rw = persisted_rw
                    bot_day = persisted_day
                elif snap.claimed_today:
                    last_rw = ""
                    bot_day = ""
                with self._state_lock:
                    row = self._rows.get(label)
                    if row is not None:
                        row.status = STATUS_IDLE
                        self._rows[label] = row
                self._emit_table()

        if not snap.api_error and snap.next_available_utc is not None:
            now = datetime.now(timezone.utc)
            na = snap.next_available_utc
            if na.tzinfo is None:
                na = na.replace(tzinfo=timezone.utc)
            if now < na:
                iso = na.isoformat()

        if snap.api_error:
            bot_day = ""

        if not snap.api_error:
            streak_n, streak_tot = snap.streak, snap.streak_total
        else:
            streak_n, streak_tot = 0, 0

        with self._state_lock:
            row = self._rows.get(label)
            if row is not None:
                row.daily_claim_rewards_text = last_rw
                row.daily_bot_claim_day_key = bot_day
                row.daily_checkin_deadline_iso = iso
                row.daily_checkin_streak = streak_n
                row.daily_checkin_streak_total = streak_tot
                self._rows[label] = row

    def _apply_season_pass(
        self,
        client: GameeClient,
        session: GameeSession,
        label: str,
        *,
        claim: bool,
        force: bool = False,
        notifier: TelegramNotifier | None = None,
    ) -> None:
        """Прогресс Season Pass в строке; при claim=True — сначала бесплатные вехи, затем премиум."""
        if not self._reserve_season_sync(label, force=force):
            return
        rewards_note = ""
        prog = None
        self._season_api_semaphore.acquire()
        try:
            try:
                if claim:
                    free_note, prog = client.claim_season_pass_free_all(session)
                    if free_note:
                        self.log_message.emit(
                            f"[{label}] Сезон (беспл.): получено — {free_note}"
                        )
                    prem_note, prog = client.claim_season_pass_premium_all(session)
                    if prem_note:
                        self.log_message.emit(
                            f"[{label}] Сезон (прем.): получено — {prem_note}"
                        )
                    parts = [p for p in (free_note, prem_note) if p]
                    rewards_note = "; ".join(parts)
                else:
                    prog = client.get_season_pass_progress(session)
            except Exception as e:
                self._note_account_error(label)
                with self._state_lock:
                    row = self._rows.get(label)
                    if row is not None:
                        row.season_rewards_text = "сезон: ошибка"
                        self._rows[label] = row
                self.log_message.emit(f"[{label}] Сезон: {e}")
                return
        finally:
            self._season_api_semaphore.release()
        cell = "—"
        if prog is not None:
            cell = prog.to_cell(self._cfg.gamee, rewards_note if claim else "")
        elif claim and rewards_note:
            cell = rewards_note if len(rewards_note) <= 96 else rewards_note[:93] + "..."
        with self._state_lock:
            row = self._rows.get(label)
            if row is not None:
                row.season_rewards_text = cell
                self._rows[label] = row

        rw = (rewards_note or "").strip()
        if (
            claim
            and rw
            and notifier is not None
            and notifier.enabled()
            and self._telegram_notify_ok()
            and self._cfg.telegram.notify_on_season_claim
        ):
            msg = format_season_claim_message(label=label, rewards_line=rw)
            with self._notifier_lock:
                notifier.send(msg)

    def _sync_account(
        self,
        client: GameeClient,
        notifier: TelegramNotifier,
        acc: AccountRecord,
    ) -> None:
        label = acc.label
        px_cell, px_tip = gamee_proxy_table_summary(acc.proxy_url)
        row = self._rows.get(label) or RowState(
            label=label, energy=0, gold=0, usd_cents=0
        )
        row.proxy_cell = px_cell
        row.proxy_tooltip = px_tip
        guard_reason = self._background_guard_reason(label)
        if guard_reason is not None:
            with self._state_lock:
                row = self._rows.get(label) or RowState(
                    label=label, energy=0, gold=0, usd_cents=0
                )
                row.proxy_cell = px_cell
                row.proxy_tooltip = px_tip
                row.status = guard_reason
                row.last_error = ""
                self._rows[label] = row
            self._emit_table()
            return
        try:
            with self._state_lock:
                session = self._session_for(acc)
                self._rows[label] = row
        except Exception as e:
            err = (str(e).strip() or repr(e))[:800]
            tb = traceback.format_exc()
            with self._state_lock:
                row = self._rows.get(label) or RowState(
                    label=label, energy=0, gold=0, usd_cents=0
                )
                row.proxy_cell = px_cell
                row.proxy_tooltip = px_tip
                row.status = "ошибка входа"
                row.last_error = err
                self._rows[label] = row
            self._note_account_error(label)
            self.log_message.emit(
                f"[{label}] Сессия / init_data (Telethon или строка входа): {err}\n{tb}"
            )
            self._emit_table()
            return
        try:
            state = client.get_assets_state(session)
            if state.last_error:
                self._note_account_error(label)
            else:
                self._note_account_success(label)
            with self._state_lock:
                row = self._rows.get(label) or RowState(
                    label=label, energy=0, gold=0, usd_cents=0
                )
                row.energy = state.energy
                row.gold = state.gold
                row.usd_cents = state.usd_cents
                row.gold_estimated_usd = state.gold_estimated_usd
                row.last_error = state.last_error or ""
                row.regen_deadline_utc = state.next_live_at_utc
                if state.last_error:
                    err = (state.last_error or "").strip() or "ошибка"
                    if len(err) > 120:
                        err = err[:117] + "..."
                    row.status = f"ошибка: {err}"
                else:
                    row.status = STATUS_IDLE
                self._rows[label] = row

            if not state.last_error:
                self._apply_season_pass(
                    client,
                    session,
                    label,
                    claim=self._background_mode_allows_claims(),
                    force=self._background_mode_allows_claims(),
                    notifier=notifier if self._background_mode_allows_claims() else None,
                )
                self._emit_table()

            self._apply_daily_checkin(
                client,
                session,
                label,
                allow_claim=self._background_mode_allows_claims(),
            )

            if state.last_error:
                self._emit_table()
                return

            if self._background_mode_allows_claims():
                state2 = client.get_assets_state(session)
                if not state2.last_error:
                    state = state2
                    self._note_account_success(label)
                    with self._state_lock:
                        row = self._rows.get(label) or RowState(
                            label=label, energy=0, gold=0, usd_cents=0
                        )
                        row.energy = state.energy
                        row.gold = state.gold
                        row.usd_cents = state.usd_cents
                        row.gold_estimated_usd = state.gold_estimated_usd
                        row.regen_deadline_utc = state.next_live_at_utc
                        self._rows[label] = row
                else:
                    self._note_account_error(label)

            self._emit_table()
            return
        except Exception as e:
            with self._state_lock:
                row = self._rows.get(label) or RowState(
                    label=label, energy=0, gold=0, usd_cents=0
                )
                row.status = "исключение"
                row.last_error = str(e)
                self._rows[label] = row
            self._note_account_error(label)
            self.log_message.emit(f"[{label}] {e}\n{traceback.format_exc()}")
            self._emit_table()

    def _ordered_row_states(self) -> list[RowState]:
        """Порядок строк как в accounts.yaml, чтобы номера слева не прыгали при запуске бота."""
        by_label = {r.label: r for r in self._rows.values()}
        ordered: list[RowState] = []
        seen: set[str] = set()
        for lab in self._table_label_order:
            r = by_label.get(lab)
            if r is not None:
                ordered.append(r)
                seen.add(lab)
        rest = [r for r in self._rows.values() if r.label not in seen]
        rest.sort(key=lambda r: r.label.lower())
        ordered.extend(rest)
        return ordered

    def _emit_table(self) -> None:
        try:
            with self._state_lock:
                rows = self._ordered_row_states()
                payload: list[dict[str, Any]] = []
                for r in rows:
                    try:
                        regen_iso = (
                            r.regen_deadline_utc.isoformat()
                            if r.regen_deadline_utc is not None
                            else None
                        )
                    except Exception:
                        regen_iso = None
                    payload.append(
                        {
                            "label": r.label,
                            "energy": r.energy,
                            "gold": r.gold,
                            "gold_estimated_usd": r.gold_estimated_usd,
                            "status": r.status,
                            "last_move_at": r.last_move_at,
                            "regen_deadline_iso": regen_iso,
                            "daily_claim_rewards_text": r.daily_claim_rewards_text,
                            "daily_checkin_deadline_iso": r.daily_checkin_deadline_iso,
                            "daily_checkin_streak": r.daily_checkin_streak,
                            "daily_checkin_streak_total": r.daily_checkin_streak_total,
                            "season_rewards_text": r.season_rewards_text,
                            "last_error": r.last_error or "",
                            "proxy_cell": r.proxy_cell,
                            "proxy_tooltip": r.proxy_tooltip,
                        }
                    )
        except Exception as e:
            self.log_message.emit(
                "[таблица] Ошибка подготовки строк: "
                f"{e}\n{traceback.format_exc()}"
            )
            return
        try:
            self.table_updated.emit(payload)
        except Exception as e:
            self.log_message.emit(
                f"[таблица] Ошибка обновления UI: {e}\n{traceback.format_exc()}"
            )

    def _send_summary(self, notifier: TelegramNotifier) -> None:
        if not self._telegram_notify_ok():
            return
        with self._state_lock:
            if not self._rows:
                return
            ordered = self._ordered_row_states()
        payload = [
            {
                "label": r.label,
                "energy": r.energy,
                "gold": r.gold,
                "status": r.status,
            }
            for r in ordered
        ]
        g = self._cfg.gamee
        text = format_summary_message(
            payload,
            g.gold_micro_divisor,
            g.gold_estimate_usd_micro_divisor,
        )
        notifier.send(text, silent=True)
        self.log_message.emit("Отправлена сводка в Telegram.")
