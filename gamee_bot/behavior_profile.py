"""Per-account & per-session поведенческие модификаторы для имитации реального юзера.

Включает:
- Personality (стабильно для аккаунта): fast/normal/slow — темп игры
- Mood (per session, меняется при каждом запуске): good/neutral/bad
- Daily budget variance (±30%) — разное количество ходов в разные дни
- Quiet hours offset (±30 мин) — аккаунты не "просыпаются" синхронно
- Random startup delay (0-60 сек) — аккаунты не вламываются в API одновременно

Цель: разнообразить поведение разных аккаунтов и разных запусков, чтобы
поведенческая ML-модель не могла отделить bot fleet от реальных юзеров.
"""
from __future__ import annotations

import hashlib
import math
import random
from dataclasses import dataclass
from datetime import datetime


# ── Personality (стабильно для аккаунта по hash(label)) ──────────────────

_PERSONALITY_MULTIPLIERS = {
    "fast": 0.6,    # Быстрый игрок: бьёт энергию быстрее
    "normal": 1.0,
    "slow": 1.5,    # Вдумчивый: дольше думает между ходами
}


def get_personality(label: str) -> str:
    """Стабильный personality для аккаунта (по hash label)."""
    digest = hashlib.sha256((label or "_default").encode("utf-8")).digest()
    r = digest[0] / 255.0
    if r < 0.25:
        return "fast"
    if r < 0.75:
        return "normal"
    return "slow"


def personality_multiplier(label: str) -> float:
    return _PERSONALITY_MULTIPLIERS[get_personality(label)]


# ── Quiet hours offset (стабильно для аккаунта) ──────────────────────────

def quiet_hours_offset_minutes(label: str) -> int:
    """Per-account offset для quiet hours: ±30 минут."""
    digest = hashlib.sha256((label or "_default").encode("utf-8")).digest()
    return (digest[1] % 61) - 30


# ── Daily budget variance (стабильно для аккаунта + дня) ─────────────────

def daily_budget_multiplier(label: str, date_str: str) -> float:
    """Variance ±30% дневного бюджета. Стабильно для (label, date)."""
    digest = hashlib.sha256(f"{label}|{date_str}".encode("utf-8")).digest()
    # 0.7 - 1.3
    return 0.7 + (digest[0] / 255.0) * 0.6


# ── Mood (per session, не зависит от label) ──────────────────────────────

@dataclass(frozen=True, slots=True)
class SessionMood:
    name: str
    delay_multiplier: float


def roll_mood() -> SessionMood:
    """Назначить новое настроение для сессии (при каждом запуске бота)."""
    r = random.random()
    if r < 0.20:
        return SessionMood(name="good", delay_multiplier=0.85)
    if r < 0.80:
        return SessionMood(name="neutral", delay_multiplier=1.0)
    return SessionMood(name="bad", delay_multiplier=1.3)


# ── Combined delay multiplier ────────────────────────────────────────────

def combined_delay_multiplier(label: str, mood: SessionMood) -> float:
    """personality * mood — итоговый множитель для задержек хода."""
    return personality_multiplier(label) * mood.delay_multiplier


# ── Random startup delay (0-60 сек) ──────────────────────────────────────

def startup_delay() -> float:
    """Случайная задержка перед первым запросом аккаунта (0-60 сек).

    Дополнительно к stagger между потоками — аккаунты не сразу начинают
    запрашивать API после старта потока.
    """
    return random.uniform(0.0, 60.0)


# ── BEH-1: Pareto delays + outliers (long-tail) ──────────────────────────

def pareto_move_delay() -> float:
    """Move delay с Pareto-распределением и редкими outliers.

    80% — quick (3-6с)
    15% — normal (5-12с)
    5%  — outliers (15-60с — "юзер отвлёкся", читает текст, отвечает на сообщение)
    """
    r = random.random()
    if r < 0.05:
        # Длинный outlier — юзер отвлёкся
        return random.uniform(15.0, 60.0)
    if r < 0.20:
        # Средняя пауза — подумал/прочитал
        return random.uniform(5.0, 12.0)
    # Quick — обычный темп игры
    base = random.lognormvariate(math.log(4.5), 0.20)
    return max(3.0, min(7.0, base))


# ── BEH-2: Burst pattern (серии ходов + длинные паузы) ───────────────────

@dataclass(frozen=True, slots=True)
class BurstPlan:
    """План распределения ходов: серии быстрых ходов с длинными паузами."""
    bursts: tuple[int, ...]   # сколько ходов в каждом burst
    pauses: tuple[float, ...] # длинные паузы между bursts (сек)


def plan_burst_schedule(total_moves: int) -> BurstPlan:
    """Разбить N ходов на 2-5 burstов по 2-7 ходов с длинными паузами 20-90с.

    Реальные юзеры: серия быстрых ходов → отвлёкся (заметил уведомление,
    залип в чат, сходил налить чай) → вернулся → ещё серия.
    """
    if total_moves <= 3:
        return BurstPlan(bursts=(total_moves,), pauses=())
    bursts: list[int] = []
    remaining = total_moves
    while remaining > 0:
        size = min(remaining, random.randint(2, 7))
        bursts.append(size)
        remaining -= size
    pauses = tuple(random.uniform(20.0, 90.0) for _ in range(len(bursts) - 1))
    return BurstPlan(bursts=tuple(bursts), pauses=pauses)


# ── BEH-4: Abandoned sessions ────────────────────────────────────────────

def should_abandon_session() -> bool:
    """15% сессий — юзер залогинился, посмотрел state и закрыл, не сыграв."""
    return random.random() < 0.15


# ── BEH-5: Quick vs Deep session ─────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class SessionType:
    name: str           # "quick" / "deep"
    energy_fraction: float  # какую долю энергии слить (0.0-1.0)


def roll_session_type() -> SessionType:
    """70% quick (слить 30-60% энергии), 30% deep (слить всё)."""
    if random.random() < 0.70:
        return SessionType(name="quick", energy_fraction=random.uniform(0.30, 0.65))
    return SessionType(name="deep", energy_fraction=1.0)


# ── BEH-6: Cross-day variance + vacation ─────────────────────────────────

def is_vacation_day(label: str, date_str: str) -> bool:
    """Раз в ~12 дней аккаунт пропускает день полностью (vacation)."""
    digest = hashlib.sha256(f"{label}|vacation|{date_str}".encode("utf-8")).digest()
    return (digest[0] / 255.0) < 0.08  # ~8%


def is_in_bad_week(label: str, date: datetime) -> bool:
    """Редкие "bad weeks" 5-7 дней неактивности (~4% недель)."""
    week_num = date.isocalendar().week + date.year * 100
    digest = hashlib.sha256(f"{label}|badweek|{week_num}".encode("utf-8")).digest()
    return (digest[0] / 255.0) < 0.04


# ── BEH-9: Time-of-day activity curve ────────────────────────────────────

def activity_level_for_hour(local_hour: int) -> float:
    """Уровень активности по часам локального времени (0.0 - 1.0).

    Реальные юзеры активнее вечером, минимально ночью.
    """
    levels = {
        0: 0.05, 1: 0.05, 2: 0.05, 3: 0.05, 4: 0.05, 5: 0.05,
        6: 0.10, 7: 0.20, 8: 0.30, 9: 0.35, 10: 0.40, 11: 0.45,
        12: 0.55, 13: 0.50, 14: 0.45, 15: 0.45, 16: 0.50, 17: 0.65,
        18: 0.85, 19: 1.00, 20: 1.00, 21: 0.90, 22: 0.70, 23: 0.40,
    }
    return levels.get(int(local_hour) % 24, 0.5)


def should_skip_by_time_of_day(local_hour: int) -> bool:
    """Probabilistic skip: чем ниже activity_level, тем выше шанс не играть."""
    level = activity_level_for_hour(local_hour)
    return random.random() > level


# ── BEH-3: Markov auto-correlation темпа ────────────────────────────────

class MarkovSpeedState:
    """Markov-like память темпа: speed остаётся прежним с вероятностью inertia."""

    __slots__ = ("_state", "_inertia")

    def __init__(self, inertia: float = 0.7):
        self._state = "normal"
        self._inertia = inertia

    @property
    def state(self) -> str:
        return self._state

    def step(self) -> str:
        if random.random() < self._inertia:
            return self._state
        choices = ["fast", "normal", "slow"]
        choices.remove(self._state)
        self._state = random.choice(choices)
        return self._state

    def multiplier(self) -> float:
        return {"fast": 0.7, "normal": 1.0, "slow": 1.4}[self._state]


# ── BEH-7: Account warmup phase ──────────────────────────────────────────

def warmup_multiplier(age_days: float) -> float:
    """Новый аккаунт играет меньше первые дни (warmup curve)."""
    if age_days < 0:
        age_days = 0
    if age_days < 1.0:
        return 0.20
    if age_days < 2.0:
        return 0.35
    if age_days < 4.0:
        return 0.55
    if age_days < 7.0:
        return 0.80
    return 1.0


# ── BEH-8: Multi-session day decision ────────────────────────────────────

def should_play_second_session(label: str, date_str: str) -> bool:
    """50% дней — есть вторая сессия с интервалом 4-12 часов."""
    digest = hashlib.sha256(f"{label}|2nd|{date_str}".encode("utf-8")).digest()
    return (digest[0] / 255.0) < 0.50


def second_session_delay_hours() -> float:
    """Интервал до второй сессии: 4-12 часов."""
    return random.uniform(4.0, 12.0)
