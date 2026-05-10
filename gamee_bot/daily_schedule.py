from __future__ import annotations

from datetime import datetime, time, timedelta, timezone


UZBEKISTAN_TZ = timezone(timedelta(hours=5), name="UZT")
DAILY_RESET_LOCAL_TIME = time(hour=17, minute=0)


def _now_utc(now_utc: datetime | None = None) -> datetime:
    if now_utc is None:
        return datetime.now(timezone.utc)
    if now_utc.tzinfo is None:
        return now_utc.replace(tzinfo=timezone.utc)
    return now_utc.astimezone(timezone.utc)


def daily_reset_today_utc(now_utc: datetime | None = None) -> datetime:
    now_local = _now_utc(now_utc).astimezone(UZBEKISTAN_TZ)
    reset_local = datetime.combine(
        now_local.date(),
        DAILY_RESET_LOCAL_TIME,
        tzinfo=UZBEKISTAN_TZ,
    )
    return reset_local.astimezone(timezone.utc)


def daily_available_by_schedule(now_utc: datetime | None = None) -> bool:
    now = _now_utc(now_utc)
    return now >= daily_reset_today_utc(now)


def next_daily_reset_utc(now_utc: datetime | None = None) -> datetime:
    now = _now_utc(now_utc)
    today_reset = daily_reset_today_utc(now)
    if now < today_reset:
        return today_reset
    return today_reset + timedelta(days=1)


def daily_claim_key(now_utc: datetime | None = None) -> str:
    now_local = _now_utc(now_utc).astimezone(UZBEKISTAN_TZ)
    if daily_available_by_schedule(now_utc):
        return now_local.date().isoformat()
    return (now_local.date() - timedelta(days=1)).isoformat()


def next_daily_claim_key(now_utc: datetime | None = None) -> str:
    return next_daily_reset_utc(now_utc).astimezone(UZBEKISTAN_TZ).date().isoformat()
