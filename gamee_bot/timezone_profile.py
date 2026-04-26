"""Per-account timezone — детерминированно по hash(label).

Реальные пользователи Gamee расположены в разных часовых поясах.
Если все аккаунты сидят в одном TZ — статистическая аномалия.

Используется в:
- worker.py: quiet hours / time-of-day activity calculation
- js_runtime.py: Date.getTimezoneOffset() override
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone, timedelta


# Реалистичный пул TZ для Gamee (пост-СССР + EU)
_TIMEZONE_POOL: tuple[tuple[str, int], ...] = (
    # (display name, UTC offset hours)
    ("Europe/Moscow", 3),
    ("Europe/Kyiv", 2),       # UTC+2 (EET, лето +3)
    ("Europe/Minsk", 3),
    ("Europe/Istanbul", 3),
    ("Europe/Bucharest", 2),
    ("Asia/Almaty", 6),
    ("Asia/Tashkent", 5),
    ("Asia/Yerevan", 4),
    ("Asia/Tbilisi", 4),
    ("Asia/Baku", 4),
    ("Europe/Warsaw", 1),
    ("Europe/Riga", 2),
)


def get_account_timezone(label: str) -> tuple[str, int]:
    """Возвращает (tz_name, utc_offset_hours) детерминированно по label."""
    digest = hashlib.sha256((label or "_default").encode("utf-8")).digest()
    idx = digest[0] % len(_TIMEZONE_POOL)
    return _TIMEZONE_POOL[idx]


def get_account_local_time(label: str, now_utc: datetime | None = None) -> datetime:
    """Локальное время аккаунта (учитывая его TZ)."""
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)
    elif now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    _, offset_hours = get_account_timezone(label)
    return now_utc.astimezone(timezone(timedelta(hours=offset_hours)))


def get_account_local_hour(label: str) -> int:
    """Текущий локальный час для аккаунта (0-23)."""
    return get_account_local_time(label).hour
