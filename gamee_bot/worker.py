from __future__ import annotations

import gc
import hashlib
import math
import random
import threading
import traceback
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from time import monotonic, sleep
from typing import Any

from PySide6.QtCore import QThread, Signal

from gamee_bot.account_store import AccountRecord, load_accounts
from gamee_bot.behavior_profile import (
    BurstPlan,
    MarkovSpeedState,
    SessionMood,
    SessionType,
    activity_level_for_hour,
    combined_delay_multiplier,
    daily_budget_multiplier,
    is_in_bad_week,
    is_vacation_day,
    pareto_move_delay,
    plan_burst_schedule,
    quiet_hours_offset_minutes,
    roll_mood,
    roll_session_type,
    second_session_delay_hours,
    should_abandon_session,
    should_play_second_session,
    should_skip_by_time_of_day,
    startup_delay,
    warmup_multiplier,
)
from gamee_bot.client import (
    AccountGameState,
    GameeClient,
    GameeSession,
    GameeTransientServerError,
    PlayOutcome,
)
from gamee_bot.daily_schedule import (
    daily_available_by_schedule,
    daily_claim_key,
    next_daily_claim_key,
    next_daily_reset_utc,
)
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
    format_board_move_message,
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


# Порог энергии для НАЧАЛА игры (10, 15 или 20 — стабильно по label).
# Играем до тех пор, пока энергия >= ENERGY_COST_PER_MOVE (тратим ВСЮ).
ENERGY_COST_PER_MOVE = 5
MIN_ENERGY_TO_PLAY_OPTIONS = (10, 15, 20)
ENERGY_REGEN_MINUTES = 10  # 1 энергия = 10 минут
POST_NEXT_LIVE_POLL_SLACK_SEC = 120
_REGEN_WAIT_JITTER_SEC = 60.0
# После сбоя не ждать «до регена» часами — быстрый повтор (смена прокси подхватывается в этом же цикле).
_ERROR_RETRY_IDLE_SEC = 5.0


def _supervisor_poll_delay() -> float:
    """Jittered supervisor poll delay (3-15с) — не выглядит как regular polling."""
    return random.uniform(3.0, 15.0)


def _post_daily_idle() -> float:
    return random.uniform(5.0, 15.0)


# Пауза между стартом потоков аккаунтов (порядок как в accounts.yaml), чтобы не вшмыть API/UI разом.
# Conservative stagger: official-grade pacing between account thread starts.
def _account_stagger_delay() -> float:
    """Stagger между запуском потоков аккаунтов, чтобы loginUsingTelegram не шёл бурстом."""
    return random.uniform(0.5, 2.5)


# Одновременно не более N потоков аккаунтов в rewardedProgress (иначе прокси/API «задыхаются», все в SSL read).
_SEASON_API_MAX_PARALLEL = 5
_MAX_ERROR_BACKOFF_SEC = 90.0
_RATE_LIMIT_RETRY_IDLE_SEC = (300.0, 720.0)
_SEASON_SYNC_MIN_INTERVAL_SEC = 45.0
_IDLE_JITTER_SEC = 3.0


def _looks_like_rate_limit_error(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return (
        "http 429" in msg
        or "cloudflare отдал html" in msg
        or "cloudflare waf" in msg
    )


def _regen_wait_slack() -> float:
    """Задержка после расчётного регена перед проверкой энергии: 2-3 минуты."""
    return POST_NEXT_LIVE_POLL_SLACK_SEC + random.uniform(0.0, _REGEN_WAIT_JITTER_SEC)


# Когда энергия уже >= threshold — играем почти сразу (1-3с)
_READY_TO_PLAY_JITTER_SEC = (1.0, 3.0)

STATUS_IDLE = "ожидание"
STATUS_SYNCING = "синхронизация…"
STATUS_DAILY_IN_PROGRESS = "ежедневная награда…"
STATUS_DAILY_DONE = "ежедневная награда выполнена"
STATUS_MOVE_IN_PROGRESS = "бросок кубика…"
STATUS_MOVE_DONE = "ход выполнен"
STATUS_WATCHING_ANIMATION = "смотрит анимацию…"
STATUS_SLEEPING = "сон до регена"
STATUS_BOOTSTRAP = "быстрый первый проход"
STATUS_REGEN_WAIT = "ожидание регена"

_DICE_ANIMATION_MIN_SEC = 6.0
_DICE_ANIMATION_MAX_SEC = 7.5
_REWARD_ANIMATION_EXTRA_MIN_SEC = 3.0
_REWARD_ANIMATION_EXTRA_MAX_SEC = 4.5


def _format_wait_duration(seconds: float) -> str:
    total = max(0, int(round(seconds)))
    hours, rem = divmod(total, 3600)
    minutes, sec = divmod(rem, 60)
    if hours > 0:
        return f"{hours}ч {minutes:02d}м"
    if minutes > 0:
        return f"{minutes}м {sec:02d}с"
    return f"{sec}с"


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


def play_energy_threshold_for_label(label: str) -> int:
    key = (label or "").strip().encode("utf-8")
    digest = hashlib.sha256(key).digest()
    return MIN_ENERGY_TO_PLAY_OPTIONS[digest[0] % len(MIN_ENERGY_TO_PLAY_OPTIONS)]


def _human_move_delay() -> float:
    """Бросок→анимация→результат→след.бросок: 3-8с с lognormal.

    Реальная анимация кубика короче, юзеры быстро жмут "ещё бросок"
    чтобы добить энергию. Lognormal вокруг 5с с natural variance.
    """
    base = random.lognormvariate(math.log(5.0), 0.25)
    return max(3.0, min(8.0, base))


def _human_pre_move_delay() -> float:
    """Brief thinking pause before tapping 'Play' button (1-3s)."""
    return random.uniform(1.0, 3.0)


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
        self._last_error_delay: dict[str, float] = {}
        self._season_sync_due_at: dict[str, float] = {}
        # daily_move_budget tracking: (UTC date str) -> {label -> count}
        self._daily_moves: dict[str, dict[str, int]] = {}
        self._daily_moves_lock = threading.Lock()
        self._daily_status_log_keys: set[tuple[str, str, str]] = set()
        self._daily_status_log_lock = threading.Lock()
        # Per-session mood (назначается при run()): good/neutral/bad
        self._session_mood: SessionMood = SessionMood(name="neutral", delay_multiplier=1.0)
        # Markov-like speed state per-account (BEH-3)
        self._speed_state: dict[str, MarkovSpeedState] = {}
        # Запланированные вторые сессии (BEH-8): label -> monotonic time когда играть снова
        self._second_session_at: dict[str, float] = {}
        # Fast bootstrap: на каждом запуске сначала быстро сливаем текущую энергию,
        # затем аккаунты живут по steady target без лишних API-запросов.
        self._bootstrap_pending_labels: set[str] = set()
        self._bootstrap_started_labels: set[str] = set()
        self._bootstrap_done_labels: set[str] = set()
        self._steady_energy_target_by_label: dict[str, int] = {}
        self._bootstrap_notice_emitted = False
        # TG-1 poller (фоновые getMe/get_dialogs)
        self._tg_poller: Any = None

    def _personalized_move_delay(self, label: str) -> float:
        """Move delay: Pareto (с outliers) × personality × mood × Markov speed state."""
        base = pareto_move_delay()
        # Markov state — темп игры держится с inertia 0.7
        st = self._speed_state.get(label)
        if st is None:
            st = MarkovSpeedState(inertia=0.7)
            self._speed_state[label] = st
        st.step()
        markov_mult = st.multiplier()
        return base * combined_delay_multiplier(label, self._session_mood) * markov_mult

    def _personalized_pre_move_delay(self, label: str) -> float:
        """Pre-move delay с personality + mood multiplier."""
        base = _human_pre_move_delay()
        return base * combined_delay_multiplier(label, self._session_mood)

    def _account_age_days(self, label: str) -> float:
        """Возраст аккаунта в днях (по полю created_at в accounts.yaml)."""
        acc = self._account_record_for_label(label)
        if acc is None or not getattr(acc, "created_at", None):
            return 999.0  # mature если нет поля
        try:
            from datetime import datetime as _dt, timezone as _tz
            created = _dt.fromisoformat(str(acc.created_at).replace("Z", "+00:00"))
            now = _dt.now(_tz.utc)
            return max(0.0, (now - created).total_seconds() / 86400.0)
        except Exception:
            return 999.0

    def _personal_daily_budget(self, label: str) -> int:
        """Daily budget с variance; 0 в config означает без дневного лимита."""
        base = self._cfg.compliance.daily_move_budget
        if base <= 0:
            return 0
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        variance = daily_budget_multiplier(label, date_str)
        warmup = warmup_multiplier(self._account_age_days(label))
        return max(1, int(base * variance * warmup))

    def _fast_bootstrap_enabled(self) -> bool:
        return bool(self._cfg.compliance.fast_bootstrap_enabled)

    @staticmethod
    def _ordered_float_range(lo: float, hi: float) -> tuple[float, float]:
        a = max(0.0, float(lo))
        b = max(0.0, float(hi))
        return (a, b) if a <= b else (b, a)

    def _account_stagger_delay_for_label(self, label: str) -> float:
        if self._is_bootstrap_pending(label):
            c = self._cfg.compliance
            lo, hi = self._ordered_float_range(
                c.bootstrap_account_stagger_min_seconds,
                c.bootstrap_account_stagger_max_seconds,
            )
            return random.uniform(lo, hi)
        return _account_stagger_delay()

    def _bootstrap_move_delay(self) -> float:
        c = self._cfg.compliance
        lo, hi = self._ordered_float_range(
            c.bootstrap_move_delay_min_seconds,
            c.bootstrap_move_delay_max_seconds,
        )
        return random.uniform(lo, hi)

    def _post_move_animation_delay(
        self, label: str, *, bootstrap: bool, has_reward_animation: bool
    ) -> float:
        if bootstrap:
            base = self._bootstrap_move_delay()
        else:
            base = max(
                self._personalized_move_delay(label),
                random.uniform(_DICE_ANIMATION_MIN_SEC, _DICE_ANIMATION_MAX_SEC),
            )
        if has_reward_animation:
            base += random.uniform(
                _REWARD_ANIMATION_EXTRA_MIN_SEC,
                _REWARD_ANIMATION_EXTRA_MAX_SEC,
            )
        return base

    def _steady_targets(self) -> tuple[int, ...]:
        targets = tuple(
            int(x)
            for x in self._cfg.compliance.steady_energy_targets
            if int(x) >= ENERGY_COST_PER_MOVE
        )
        return targets or MIN_ENERGY_TO_PLAY_OPTIONS

    def _choose_steady_energy_target(self, label: str) -> int:
        target = int(random.choice(self._steady_targets()))
        with self._state_lock:
            self._steady_energy_target_by_label[label] = target
        return target

    def _steady_energy_target_for_label(self, label: str) -> int:
        with self._state_lock:
            target = self._steady_energy_target_by_label.get(label)
        if target is not None and target >= ENERGY_COST_PER_MOVE:
            return int(target)
        return self._choose_steady_energy_target(label)

    def _is_bootstrap_pending(self, label: str) -> bool:
        if not self._fast_bootstrap_enabled():
            return False
        with self._state_lock:
            return label in self._bootstrap_pending_labels

    def _mark_bootstrap_started(self, label: str) -> bool:
        if not self._fast_bootstrap_enabled():
            return False
        with self._state_lock:
            if label not in self._bootstrap_pending_labels:
                return False
            if label in self._bootstrap_started_labels:
                return False
            self._bootstrap_started_labels.add(label)
            return True

    def _sleep_seconds_until_energy(
        self,
        *,
        current_energy: int,
        target_energy: int,
        regen_deadline_utc: datetime | None,
    ) -> float:
        if current_energy >= target_energy:
            return random.uniform(*_READY_TO_PLAY_JITTER_SEC)
        now = datetime.now(timezone.utc)
        if regen_deadline_utc is None:
            need = max(1, target_energy - current_energy)
            return float(need * ENERGY_REGEN_MINUTES * 60) + _regen_wait_slack()
        at = regen_deadline_utc
        if at.tzinfo is None:
            at = at.replace(tzinfo=timezone.utc)
        energy_after_next = current_energy + 1
        still_need = max(0, target_energy - energy_after_next)
        sec_to_next = max(0.0, (at - now).total_seconds())
        return float(sec_to_next + still_need * ENERGY_REGEN_MINUTES * 60) + _regen_wait_slack()

    def _set_row_status(self, label: str, status: str) -> None:
        with self._state_lock:
            row = self._rows.get(label)
            if row is None:
                return
            row.status = status
            self._rows[label] = row

    def _complete_bootstrap_if_pending(
        self,
        label: str,
        state: AccountGameState,
    ) -> None:
        if not self._fast_bootstrap_enabled():
            return
        with self._state_lock:
            if label not in self._bootstrap_pending_labels:
                return
            self._bootstrap_pending_labels.discard(label)
            self._bootstrap_done_labels.add(label)
        target = self._choose_steady_energy_target(label)
        wait = self._sleep_seconds_until_energy(
            current_energy=state.energy,
            target_energy=target,
            regen_deadline_utc=state.next_live_at_utc,
        )
        self._set_row_status(
            label,
            f"{STATUS_REGEN_WAIT} до {target} энергии (~{_format_wait_duration(wait)})",
        )
        self.log_message.emit(
            f"[{label}] Bootstrap: энергия слита, следующий возврат при "
            f"{target} энергии через ~{_format_wait_duration(wait)}."
        )

    def _note_steady_wakeup_if_due(self, label: str, current_energy: int) -> int | None:
        with self._state_lock:
            target = self._steady_energy_target_by_label.get(label)
            if target is None or current_energy < target:
                return None
            del self._steady_energy_target_by_label[label]
        return target

    def _daily_moves_used(self, label: str) -> int:
        key = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        with self._daily_moves_lock:
            return self._daily_moves.get(key, {}).get(label, 0)

    def _daily_moves_add(self, label: str, delta: int = 1) -> None:
        key = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        with self._daily_moves_lock:
            day = self._daily_moves.setdefault(key, {})
            day[label] = day.get(label, 0) + delta
            # Удаляем данные за вчера, чтобы не копить.
            stale = [k for k in self._daily_moves if k != key]
            for k in stale:
                del self._daily_moves[k]

    def _log_daily_status_once(
        self, label: str, day_key: str, status_key: str, message: str
    ) -> None:
        key = (label, day_key, status_key)
        with self._daily_status_log_lock:
            if key in self._daily_status_log_keys:
                return
            if len(self._daily_status_log_keys) > 10000:
                self._daily_status_log_keys = {
                    k for k in self._daily_status_log_keys if k[1] == day_key
                }
            self._daily_status_log_keys.add(key)
        self.log_message.emit(message)

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
        self._last_error_delay.pop(label, None)

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
        # Per-account offset ±30 минут — аккаунты не "просыпаются" синхронно
        offset_min = quiet_hours_offset_minutes(label)
        from datetime import timedelta
        shifted_now = datetime.now() + timedelta(minutes=offset_min)
        # Vacation days и bad weeks отключены по требованию пользователя:
        # все аккаунты должны играть всегда (когда есть энергия).
        if local_time_in_quiet_hours(
            c.quiet_hours_enabled,
            c.quiet_hours_start_hour,
            c.quiet_hours_end_hour,
            now=shifted_now,
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

    @staticmethod
    def _row_is_rate_limited(row: RowState) -> bool:
        return _looks_like_rate_limit_error(RuntimeError(row.last_error or row.status))

    def _idle_sleep_seconds_for_row(self, label: str, row: RowState) -> float:
        """Пауза аккаунта до следующего цикла.

        Логика: если энергия < порога — спим до тех пор, пока сервер
        не накопит нужное количество. Используем nextLiveAddedTimestamp
        (regen_deadline_utc) как базу, дальше считаем сколько ещё нужно.
        Не дёргаем API во время сна.
        """
        if self._row_is_rate_limited(row):
            return random.uniform(*_RATE_LIMIT_RETRY_IDLE_SEC)

        if self._row_needs_quick_retry_after_error(row):
            streak = max(1, self._error_streaks.get(label, 1))
            prev = self._last_error_delay.get(label, _ERROR_RETRY_IDLE_SEC)
            base = random.uniform(_ERROR_RETRY_IDLE_SEC, prev * 3.0)
            delay = min(base, _MAX_ERROR_BACKOFF_SEC) + random.uniform(0.0, 5.0)
            self._last_error_delay[label] = delay
            return delay

        bootstrap = self._is_bootstrap_pending(label)
        threshold = (
            ENERGY_COST_PER_MOVE
            if bootstrap
            else self._steady_energy_target_for_label(label)
        )

        # Энергия уже >= порога — играть почти сразу (1-3с jitter).
        if row.energy >= threshold:
            return (
                random.uniform(0.1, 0.8)
                if bootstrap
                else random.uniform(*_READY_TO_PLAY_JITTER_SEC)
            )

        delay = self._sleep_seconds_until_energy(
            current_energy=row.energy,
            target_energy=threshold,
            regen_deadline_utc=row.regen_deadline_utc,
        )
        if not daily_available_by_schedule():
            next_key = next_daily_claim_key()
            if row.daily_bot_claim_day_key != next_key:
                next_reset = next_daily_reset_utc()
                now = datetime.now(timezone.utc)
                daily_delay = max(0.0, (next_reset - now).total_seconds())
                daily_delay += random.uniform(1.0, 12.0)
                if daily_delay < delay:
                    self._set_row_status(
                        label,
                        f"ожидание daily 17:00 UZ (~{_format_wait_duration(daily_delay)})",
                    )
                    return daily_delay
        if bootstrap and row.energy < ENERGY_COST_PER_MOVE:
            state = AccountGameState(
                energy=row.energy,
                gold=row.gold,
                tickets=0,
                usd_cents=row.usd_cents,
                gold_estimated_usd=row.gold_estimated_usd,
                last_error=row.last_error or None,
                next_live_at_utc=row.regen_deadline_utc,
            )
            self._complete_bootstrap_if_pending(label, state)
        else:
            self._set_row_status(
                label,
                f"{STATUS_REGEN_WAIT} до {threshold} энергии (~{_format_wait_duration(delay)})",
            )
        return delay

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
        # Clean runtime reset: счётчики/ошибки с нуля, но init_data cache сохраняем
        # для быстрого повторного старта.
        try:
            GameeClient._next_request_not_before = 0.0
            # Сброс cluster-wide rate-limit state — все proxy "чистые" при новом запуске
            with GameeClient._rate_limit_lock:
                GameeClient._rate_limit_state.clear()
        except Exception:
            pass
        # Сброс daily moves, error streaks, speed states — всё с нуля
        with self._daily_moves_lock:
            self._daily_moves.clear()
        self._error_streaks.clear()
        self._error_cooldown_until.clear()
        self._last_error_delay.clear()
        self._speed_state.clear()
        self._second_session_at.clear()
        self._season_sync_due_at.clear()
        with self._daily_status_log_lock:
            self._daily_status_log_keys.clear()
        self._bootstrap_pending_labels.clear()
        self._bootstrap_started_labels.clear()
        self._bootstrap_done_labels.clear()
        self._steady_energy_target_by_label.clear()
        self._bootstrap_notice_emitted = False
        # Per-session mood: настроение этого запуска (good/neutral/bad)
        self._session_mood = roll_mood()
        self.log_message.emit(f"Сессия запущена: настроение = {self._session_mood.name}.")
        # TG poller отключён: создаёт fight за Telethon session lock с основным циклом.
        # Background getMe/get_dialogs можно вернуть позже когда session-locking будет сделан правильно.
        self._tg_poller = None

        notifier = TelegramNotifier(self._cfg.telegram.bot_token, self._cfg.telegram.chat_id)
        self._notifier = notifier
        last_summary = monotonic()
        try:
            while self._running:
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
                        self._bootstrap_pending_labels.clear()
                        self._bootstrap_started_labels.clear()
                        self._bootstrap_done_labels.clear()
                        self._steady_energy_target_by_label.clear()
                        self._bootstrap_notice_emitted = False
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
                            self._bootstrap_pending_labels.discard(dead)
                            self._bootstrap_started_labels.discard(dead)
                            self._bootstrap_done_labels.discard(dead)
                            self._steady_energy_target_by_label.pop(dead, None)
                            self._wake_labels.pop(dead, None)
                    for dead in list(self._sessions.keys()):
                        if dead not in alive_labels:
                            del self._sessions[dead]
                            self._error_streaks.pop(dead, None)
                            self._error_cooldown_until.pop(dead, None)
                            self._season_sync_due_at.pop(dead, None)
                            self._bootstrap_pending_labels.discard(dead)
                            self._bootstrap_started_labels.discard(dead)
                            self._bootstrap_done_labels.discard(dead)
                            self._steady_energy_target_by_label.pop(dead, None)
                            self._wake_labels.pop(dead, None)
                    for acc in accounts:
                        if self._fast_bootstrap_enabled():
                            if (
                                acc.label not in self._bootstrap_pending_labels
                                and acc.label not in self._bootstrap_done_labels
                            ):
                                self._bootstrap_pending_labels.add(acc.label)
                                if not self._bootstrap_notice_emitted:
                                    self.log_message.emit(
                                        "Быстрый первый проход: старт. "
                                        "Аккаунты быстро выполнят действия и затем перейдут в ожидание регена."
                                    )
                                    self._bootstrap_notice_emitted = True
                        else:
                            self._bootstrap_pending_labels.discard(acc.label)
                            self._bootstrap_started_labels.discard(acc.label)
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
                        name=f"worker-{hashlib.md5(acc.label.encode()).hexdigest()[:8]}",
                        daemon=True,
                    )
                    self._account_threads[acc.label] = t
                    t.start()
                    if i + 1 < len(pending_start):
                        self._sleep_interruptible(
                            self._account_stagger_delay_for_label(acc.label)
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

                self._sleep_interruptible(_supervisor_poll_delay())
        finally:
            self._running = False
            self._stop_all_account_threads()
            if self._tg_poller is not None:
                try:
                    self._tg_poller.stop()
                except Exception:
                    pass
                self._tg_poller = None
            self._notifier = None
            notifier.close()

    def _account_thread_main(self, label: str) -> None:
        # Стартуем сразу — пользователь не хочет ждать.
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
                            account_label=label,
                            cookie_base_dir=self._cfg.accounts_path.parent,
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
                    if _looks_like_rate_limit_error(e):
                        self.log_message.emit(
                            f"[{label}] Cloudflare/429: {e} Повторим позже без смены прокси."
                        )
                    else:
                        self.log_message.emit(
                            f"[{label}] Сбой цикла аккаунта (продолжаем попытки): "
                            f"{e}\n{traceback.format_exc()}"
                        )
                    with self._state_lock:
                        row = self._rows.get(label) or RowState(
                            label=label, energy=0, gold=0, usd_cents=0
                        )
                        row.status = (
                            "Cloudflare/429 cooldown"
                            if _looks_like_rate_limit_error(e)
                            else "сбой цикла"
                        )
                        row.last_error = str(e).strip() or repr(e)
                        self._rows[label] = row
                    self._note_account_error(label)
                    self._emit_table()
                    idle = self._idle_sleep_seconds_for_row(label, row)
                    self._emit_table()
                    self._sleep_interruptible(idle, label=label)
                    continue
                with self._state_lock:
                    row = self._rows.get(label)
                idle = self._idle_sleep_seconds_for_row(label, row) if row is not None else 10.0
                self._emit_table()
                self._sleep_interruptible(idle, label=label)
        finally:
            if client is not None:
                client.close()

    def _session_for(self, acc: AccountRecord) -> GameeSession:
        yaml_path = self._cfg.accounts_path
        fresh = build_gamee_session_for_account(self._cfg, acc)
        resolved = fresh.init_data
        ref_id = fresh.telegram_referral_ref
        with self._state_lock:
            s = self._sessions.get(acc.label)
            if s is None or s.init_data != resolved:
                self._sessions[acc.label] = fresh
                return fresh
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
        fast: bool = False,
    ) -> bool:
        now_utc = datetime.now(timezone.utc)
        next_reset = next_daily_reset_utc(now_utc)
        if not daily_available_by_schedule(now_utc):
            next_key = next_daily_claim_key(now_utc)
            wait = max(0.0, (next_reset - now_utc).total_seconds())
            with self._state_lock:
                row = self._rows.get(label)
                if row is not None:
                    row.daily_claim_rewards_text = "ожидание 17:00 UZ"
                    row.daily_checkin_deadline_iso = next_reset.isoformat()
                    self._rows[label] = row
            self._log_daily_status_once(
                label,
                next_key,
                "before_17_uz",
                f"[{label}] Ежедневная награда будет доступна через "
                f"~{_format_wait_duration(wait)} (17:00 UZ).",
            )
            self._emit_table()
            return False

        day_key = daily_claim_key(now_utc)
        with self._state_lock:
            row = self._rows.get(label)
            persisted_rw = (row.daily_claim_rewards_text if row else "") or ""
            persisted_day = (row.daily_bot_claim_day_key if row else "") or ""
            current_streak = int(row.daily_checkin_streak if row else 0)
            current_streak_total = int(row.daily_checkin_streak_total if row else 0)

        trusted_persisted_claim = (
            persisted_day == day_key
            and bool(persisted_rw.strip())
            and persisted_rw.strip().lower() not in {"уже забрана", "already claimed"}
            and not persisted_rw.strip().lower().startswith("не взята")
        )
        if trusted_persisted_claim:
            with self._state_lock:
                row = self._rows.get(label)
                if row is not None:
                    row.daily_claim_rewards_text = persisted_rw
                    row.daily_checkin_deadline_iso = next_reset.isoformat()
                    self._rows[label] = row
            self._emit_table()
            return False

        snap = client.get_daily_checkin_snapshot(session)

        iso: str | None = None
        last_rw = ""
        bot_day = ""
        can_claim_now = False
        claimed_reward = False

        if snap.api_error:
            self._note_account_error(label)
            last_rw = "ежедн. — ошибка"
            self.log_message.emit(
                f"[{label}] Ежедневная награда: проверка не удалась — {snap.api_error}"
            )
        else:
            can_claim_now = snap.can_claim_now()
            if snap.claimed_today and not can_claim_now:
                last_rw = ""
                self._log_daily_status_once(
                    label,
                    day_key,
                    "claimed_by_api_probe",
                    f"[{label}] Ежедневная награда: API пишет, что уже забрана; "
                    "проверяю claim напрямую.",
                )
                if allow_claim:
                    can_claim_now = True
            else:
                last_rw = ""

            if can_claim_now and not allow_claim:
                self._log_daily_status_once(
                    label,
                    day_key,
                    "available_autoclaim_off",
                    f"[{label}] Ежедневная награда доступна, но автоклейм выключен.",
                )
            elif not can_claim_now and not snap.claimed_today:
                wait_note = ""
                if snap.next_available_utc is not None:
                    na = snap.next_available_utc
                    if na.tzinfo is None:
                        na = na.replace(tzinfo=timezone.utc)
                    if now_utc < na:
                        wait_note = f" Будет доступна через ~{_format_wait_duration((na - now_utc).total_seconds())}."
                if not wait_note:
                    wait_note = " Повторная проверка будет только при следующей серии ходов."
                self._log_daily_status_once(
                    label,
                    day_key,
                    "not_available",
                    f"[{label}] Ежедневная награда пока недоступна.{wait_note}",
                )

        will_try_claim = not snap.api_error and allow_claim and can_claim_now
        if will_try_claim:
            with self._state_lock:
                row = self._rows.get(label)
                if row is not None:
                    row.status = STATUS_DAILY_IN_PROGRESS
                    self._rows[label] = row
            self._emit_table()

        if will_try_claim:
            ok, rw, snap2 = client.claim_daily_checkin(session)
            if ok:
                last_rw = rw if rw.strip() not in ("", "—") else "OK"
                bot_day = day_key
                claimed_reward = True
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
                if not fast:
                    self._sleep_interruptible(_post_daily_idle(), label=label)
                with self._state_lock:
                    row = self._rows.get(label)
                    if row is not None:
                        row.status = STATUS_BOOTSTRAP if fast else STATUS_IDLE
                        self._rows[label] = row
                self._emit_table()
            else:
                err_hint = (rw or "").strip() or "клейм не удался"
                self.log_message.emit(f"[{label}] Ежедневная награда не взята: {err_hint}")
                snap = client.get_daily_checkin_snapshot(session)
                last_rw = f"не взята: {err_hint}"
                bot_day = ""
                with self._state_lock:
                    row = self._rows.get(label)
                    if row is not None:
                        row.status = STATUS_IDLE
                        self._rows[label] = row
                self._emit_table()

        if bot_day == day_key:
            iso = next_reset.isoformat()
        elif not snap.api_error and snap.next_available_utc is not None:
            na = snap.next_available_utc
            if na.tzinfo is None:
                na = na.replace(tzinfo=timezone.utc)
            if now_utc < na:
                iso = na.isoformat()

        if snap.api_error:
            bot_day = ""

        if not snap.api_error:
            streak_n, streak_tot = snap.streak, snap.streak_total
        else:
            streak_n, streak_tot = current_streak, current_streak_total

        with self._state_lock:
            row = self._rows.get(label)
            if row is not None:
                row.daily_claim_rewards_text = last_rw
                row.daily_bot_claim_day_key = bot_day
                row.daily_checkin_deadline_iso = iso
                row.daily_checkin_streak = streak_n
                row.daily_checkin_streak_total = streak_tot
                self._rows[label] = row
        self._emit_table()
        return claimed_reward

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
        bootstrap = self._is_bootstrap_pending(label)
        if self._mark_bootstrap_started(label):
            with self._state_lock:
                row = self._rows.get(label) or RowState(
                    label=label, energy=0, gold=0, usd_cents=0
                )
                row.proxy_cell = px_cell
                row.proxy_tooltip = px_tip
                row.status = STATUS_BOOTSTRAP
                self._rows[label] = row
            self._emit_table()
        try:
            session = self._session_for(acc)
            with self._state_lock:
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
            try:
                state = client.get_assets_state(session)
            except GameeTransientServerError as te:
                # Gamee вернул JSON-RPC -32603. Это не обязательно outage сервера:
                # часто причина в профиле запроса, initData или прокси.
                with self._state_lock:
                    row = self._rows.get(label) or RowState(
                        label=label, energy=0, gold=0, usd_cents=0
                    )
                    row.proxy_cell = px_cell
                    row.proxy_tooltip = px_tip
                    row.status = "сервер занят, повтор позже"
                    row.last_error = "Server error -32603 (transient)"
                    self._rows[label] = row
                self._note_account_error(label)
                self.log_message.emit(
                    f"[{label}] Gamee вернул -32603 при логине ({str(te)[:140]}). Повторим позже."
                )
                self._emit_table()
                return
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
                    row.status = STATUS_BOOTSTRAP if bootstrap else STATUS_IDLE
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

            bootstrap = self._is_bootstrap_pending(label)
            daily_claimed = self._apply_daily_checkin(
                client,
                session,
                label,
                allow_claim=self._background_mode_allows_claims(),
                fast=bootstrap,
            )
            if daily_claimed:
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
                    self._emit_table()
                else:
                    self._note_account_error(label)

            reached_steady_target: int | None = None
            if not bootstrap:
                reached_steady_target = self._note_steady_wakeup_if_due(label, state.energy)
            threshold = (
                ENERGY_COST_PER_MOVE
                if bootstrap
                else reached_steady_target or self._steady_energy_target_for_label(label)
            )
            if state.energy < threshold:
                if bootstrap and state.energy < ENERGY_COST_PER_MOVE:
                    self._complete_bootstrap_if_pending(label, state)
                self._emit_table()
                return

            # ── Энергию ВСЕГДА сливаем до конца (как реальные юзеры) ──
            # Quick/Deep sessions отключены — сливаем до 0 (energy < ENERGY_COST_PER_MOVE).
            # Abandoned sessions тоже отключены — пользователь требует всегда сливать.

            # Начальная сессия: имитация просмотра страницы перед игрой
            try:
                gen = client.telemetry_generator
                if gen is not None:
                    gen.generate_session_interaction(
                        page_height=3200, num_taps=1, num_scrolls=2
                    )
            except Exception:
                pass

            # ── Play board loop ──
            # Дневной бюджет с per-account variance ±30%
            daily_budget = 0 if bootstrap else self._personal_daily_budget(label)
            # BEH-2: Burst plan — разбиваем ходы на серии с длинными паузами между ними
            estimated_moves = max(1, state.energy // ENERGY_COST_PER_MOVE)
            burst_plan = (
                BurstPlan(bursts=(estimated_moves,), pauses=())
                if bootstrap
                else plan_burst_schedule(estimated_moves)
            )
            burst_idx = 0
            moves_in_current_burst = 0
            move_idx = 0
            series_interrupted = False
            while self._running and state.energy >= ENERGY_COST_PER_MOVE:
                if daily_budget > 0 and self._daily_moves_used(label) >= daily_budget:
                    self.log_message.emit(
                        f"[{label}] Дневной бюджет ходов ({daily_budget}) исчерпан."
                    )
                    break
                # Check for proxy changes mid-loop
                fresh = self._account_record_for_label(label)
                if fresh is not None:
                    try:
                        want_p = normalize_and_validate_gamee_proxy(fresh.proxy_url)
                    except ValueError as e:
                        self.log_message.emit(
                            f"[{label}] Прокси в accounts.yaml: {e} — прерываю серию ходов."
                        )
                        series_interrupted = True
                        break
                    if want_p != client.proxy_url:
                        self.log_message.emit(
                            f"[{label}] Прокси изменён — переподключение с новым каналом."
                        )
                        series_interrupted = True
                        break

                move_idx += 1
                with self._state_lock:
                    row = self._rows.get(label) or RowState(
                        label=label, energy=0, gold=0, usd_cents=0
                    )
                    row.status = (
                        f"{STATUS_BOOTSTRAP}: {STATUS_MOVE_IN_PROGRESS}"
                        if bootstrap
                        else STATUS_MOVE_IN_PROGRESS
                    )
                    self._rows[label] = row
                self._emit_table()

                if bootstrap:
                    self._sleep_interruptible(random.uniform(0.05, 0.15), label=label)
                else:
                    # Human-like: brief thinking pause before tapping "Play"
                    self._sleep_interruptible(
                        self._personalized_pre_move_delay(label),
                        label=label,
                    )
                if not self._running:
                    break

                if not bootstrap:
                    # Имитация tap-события на кнопку Play (телеметрия + реалистичная задержка)
                    client.simulate_play_tap(session)
                    if not self._running:
                        break

                outcome = client.play_board(session)
                if not outcome.ok:
                    with self._state_lock:
                        row = self._rows.get(label) or RowState(
                            label=label, energy=0, gold=0, usd_cents=0
                        )
                        row.status = "ход не удался"
                        row.last_error = outcome.error or "?"
                        self._rows[label] = row
                    self._note_account_error(label)
                    self.log_message.emit(
                        f"[{label}] Бросок кубика #{move_idx}: не удалось — {outcome.error}"
                    )
                    self._emit_table()
                    series_interrupted = True
                    break

                before = outcome.before
                after = outcome.after
                assert after is not None
                state = after
                ts = _local_time_last_move()
                with self._state_lock:
                    row = self._rows.get(label) or RowState(
                        label=label, energy=0, gold=0, usd_cents=0
                    )
                    row.energy = after.energy
                    row.gold = after.gold
                    row.usd_cents = after.usd_cents
                    row.gold_estimated_usd = after.gold_estimated_usd
                    row.last_error = after.last_error or ""
                    row.regen_deadline_utc = after.next_live_at_utc
                    row.last_move_at = ts
                    row.status = (
                        f"{STATUS_BOOTSTRAP}: {STATUS_MOVE_DONE}"
                        if bootstrap
                        else STATUS_MOVE_DONE
                    )
                    self._rows[label] = row

                self._note_account_success(label)
                self._daily_moves_add(label)
                reward_line = (
                    outcome.rewards_text
                    if outcome.rewards_text.strip() not in ("", "—")
                    else "ничего"
                )
                has_reward_animation = reward_line != "ничего"
                dice_s = str(outcome.dice_value) if outcome.dice_value is not None else "?"
                self.log_message.emit(
                    f"[{label}] Ход #{move_idx}: выпало {dice_s}, награда {reward_line}, "
                    f"энергия {before.energy}->{after.energy}, золото {before.gold}->{after.gold}."
                )
                self.session_earnings_move.emit(
                    label,
                    after.gold - before.gold,
                    after.tickets - before.tickets,
                    outcome.xp_gained,
                )
                if not bootstrap and not after.last_error:
                    self._apply_season_pass(
                        client,
                        session,
                        label,
                        claim=self._background_mode_allows_claims(),
                        notifier=notifier if self._background_mode_allows_claims() else None,
                    )
                self._emit_table()

                if not self._running:
                    break
                post_move_delay = self._post_move_animation_delay(
                    label,
                    bootstrap=bootstrap,
                    has_reward_animation=has_reward_animation,
                )
                with self._state_lock:
                    row = self._rows.get(label) or RowState(
                        label=label, energy=0, gold=0, usd_cents=0
                    )
                    row.status = (
                        f"{STATUS_BOOTSTRAP}: {STATUS_WATCHING_ANIMATION}"
                        if bootstrap
                        else STATUS_WATCHING_ANIMATION
                    )
                    self._rows[label] = row
                self._emit_table()
                if bootstrap:
                    self._sleep_interruptible(post_move_delay, label=label)
                else:
                    # BEH-2: Если завершён текущий burst — длинная пауза перед следующим
                    moves_in_current_burst += 1
                    self._sleep_interruptible(post_move_delay, label=label)
                    if not self._running:
                        break
                    if (
                        burst_idx < len(burst_plan.bursts)
                        and moves_in_current_burst >= burst_plan.bursts[burst_idx]
                        and burst_idx < len(burst_plan.pauses)
                    ):
                        pause = burst_plan.pauses[burst_idx]
                        self.log_message.emit(
                            f"[{label}] Burst #{burst_idx+1} done — long pause {pause:.0f}s."
                        )
                        self._sleep_interruptible(pause, label=label)
                        burst_idx += 1
                        moves_in_current_burst = 0
                with self._state_lock:
                    row = self._rows.get(label) or RowState(
                        label=label, energy=0, gold=0, usd_cents=0
                    )
                    row.status = STATUS_BOOTSTRAP if bootstrap else STATUS_IDLE
                    self._rows[label] = row
                self._emit_table()
                if state.energy < ENERGY_COST_PER_MOVE:
                    break

            if bootstrap and not series_interrupted and state.energy < ENERGY_COST_PER_MOVE:
                self._complete_bootstrap_if_pending(label, state)
                self._emit_table()
            return
        except Exception as e:
            rate_limited = _looks_like_rate_limit_error(e)
            with self._state_lock:
                row = self._rows.get(label) or RowState(
                    label=label, energy=0, gold=0, usd_cents=0
                )
                row.status = "Cloudflare/429 cooldown" if rate_limited else "исключение"
                row.last_error = str(e)
                self._rows[label] = row
            self._note_account_error(label)
            if rate_limited:
                self.log_message.emit(
                    f"[{label}] Cloudflare/429: {e} Повторим позже без смены прокси."
                )
            else:
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
