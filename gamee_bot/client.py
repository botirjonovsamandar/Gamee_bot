from __future__ import annotations

import base64
import json
import random
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from gamee_bot.account_store import set_account_gamee_registration_state
from gamee_bot.config import GameeConfig
from gamee_bot.gamee_transport import (
    GameeTransport,
    GameeTransportError,
    GameeTransportHTTPError,
    GameeTransportRequest,
    build_default_gamee_transport,
)
from gamee_bot.http_profile import GameeHttpClientProfile
from gamee_bot.telethon_bridge import clear_init_cache

# HTTP: коды протухшей сессии; 403 часто означает WAF/прокси и не должен запускать relogin storm.
_HTTP_RELOGIN_STATUS_CODES = frozenset({401, 419, 498})

# Временные отказы / лимиты — повтор до raise_for_status.
_RETRYABLE_HTTP_STATUS = frozenset({502, 503, 504})
_MAX_HTTP_TRANSIENT_RETRIES = 4
_HTTP_REQUEST_MAX_PARALLEL = 12
_HTTP_REQUEST_START_GAP_SEC = 0.12
_HTTP_REQUEST_START_JITTER_SEC = 0.18

# Harmless methods that can be added to any batch as "noise" to vary fingerprint.
_BATCH_NOISE_METHODS = [
    "app.telegram.get",
    "user.getBalance",
    "rewardedProgress.getAll",
    "dailyCheckin.getInformation",
]
# Methods that must NOT have their batch shuffled or padded (order-sensitive).
_BATCH_NO_RANDOMIZE_IDS = frozenset({
    "user.authentication.loginUsingTelegram",
    "user.claimActivity",
    "user.getActivities",
})
_ACTIVITIES_PAGE_LIMIT = 25


class GameeTransientServerError(RuntimeError):
    """Транзиентная ошибка сервера Gamee (например -32603 Server error).

    Не выводить traceback — это ожидаемая ошибка временной недоступности.
    Worker должен пометить аккаунт временной ошибкой и повторить позже.
    """
    pass


def _http_status_from_error(exc: BaseException) -> int | None:
    """Нормализованная HTTP-ошибка транспорта -> код ответа."""
    resp = getattr(exc, "response", None)
    if resp is not None:
        sc = getattr(resp, "status_code", None)
        if sc is not None:
            return int(sc)
    return None


def _api_error_body_hint(raw: str) -> str:
    """Убираем мегабайты HTML Cloudflare из логов; оставляем суть."""
    t = raw or ""
    low = t.lower()
    if "attention required!" in low and "cloudflare" in low:
        return (
            "Cloudflare WAF (страница «Attention Required»): запрос временно ограничен "
            "или требует браузерную проверку. Прокси может быть рабочим; обычно нужен cooldown "
            "и меньше одновременных запусков."
        )
    if "cloudflare" in low and "<!doctype html>" in low[:200].lower():
        return (
            "Cloudflare отдал HTML вместо JSON — запрос временно ограничен/поставлен на проверку. "
            "Прокси может быть рабочим; нужен cooldown и меньше параллельных логинов."
        )
    return t[:900]


def _jsonrpc_message_suggests_relogin(message: str) -> bool:
    """Текст ошибки JSON-RPC — признаки протухшего токена (не лимит запросов 429)."""
    low = str(message).lower()
    if any(x in low for x in ("401", "419", "498")):
        return True
    if "403" in low and any(
        x in low
        for x in (
            "session",
            "expired",
            "invalid token",
            "authentication",
            "unauthorized",
        )
    ):
        return True
    return any(
        x in low
        for x in (
            "unauthorized",
            "forbidden",
            "session",
            "expired",
            "invalid token",
            "token expired",
            "authentication",
            "not authenticated",
            "authorize",
            "access denied",
        )
    )


def _jsonrpc_error_code(err: Any) -> int | None:
    if not isinstance(err, dict):
        return None
    try:
        return int(err.get("code"))
    except (TypeError, ValueError):
        return None


def _jsonrpc_error_message(err: Any) -> str:
    if not isinstance(err, dict):
        return str(err)
    msg = str(err.get("message", "") or "").strip()
    data = err.get("data")
    details: list[str] = []
    if isinstance(data, dict):
        for key in ("reason", "code", "name", "message"):
            value = data.get(key)
            if value is not None and str(value).strip():
                details.append(f"{key}={value}")
    elif data is not None and str(data).strip():
        details.append(str(data))
    code = _jsonrpc_error_code(err)
    if code is not None and msg:
        msg = f"{msg} (code={code})"
    elif code is not None:
        msg = f"code={code}"
    if details:
        msg = f"{msg}: {', '.join(details)}" if msg else ", ".join(details)
    return msg or str(err)


def _jsonrpc_error_suggests_relogin(err: Any) -> bool:
    # Matches the public web client's CODE_ENTITY_EXPIRED handling.
    if _jsonrpc_error_code(err) in {-32005, 401, 419, 498}:
        return True
    return _jsonrpc_message_suggests_relogin(_jsonrpc_error_message(err))


def _board_get_error_is_missing_reward_progress(berr: Any) -> bool:
    """
    luckyGame.board.get иногда отвечает, что нет rewarded progress по доске
    (новый сезон, аккаунт не «привязан» к треку, первый заход).
    Это не сбой сессии: энергию берём из LIFE в getAssets; ход board.play может проходить.
    """
    if isinstance(berr, dict):
        msg = str(berr.get("message", ""))
        data = berr.get("data")
        extra = ""
        if isinstance(data, dict):
            extra = str(data.get("code", data.get("name", "")))
        blob = f"{msg} {extra}".lower()
    else:
        blob = str(berr).lower()
    compact = blob.replace("_", "").replace(" ", "")
    return (
        "rewarded progress not found" in blob
        or "boardgetrewardprogressnotfound" in compact
    )


def _jwt_expiry_unix(token: str) -> int | None:
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return None
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload))
        exp = data.get("exp")
        return int(exp) if exp is not None else None
    except (ValueError, json.JSONDecodeError):
        return None


def _pick_token_from_login_result(result: dict[str, Any]) -> str | None:
    tokens = result.get("tokens")
    if isinstance(tokens, dict):
        t = tokens.get("authenticate")
        if isinstance(t, str) and t:
            return t
    return None


def _login_result_is_brand_new_gamee_user(result: dict[str, Any]) -> bool:
    """Ответ loginUsingTelegram: только при newRegistration=True применяем рефы."""
    user = result.get("user")
    if not isinstance(user, dict):
        return False
    about = user.get("about")
    if not isinstance(about, dict):
        return False
    return about.get("newRegistration") is True


def _board_next_live_added_utc(board_result: dict[str, Any] | None) -> datetime | None:
    """luckyGame.board.get → lives.nextLiveAddedTimestamp — когда придёт +1 энергии."""
    if not isinstance(board_result, dict):
        return None
    lives = board_result.get("lives")
    if not isinstance(lives, dict):
        return None
    ts = lives.get("nextLiveAddedTimestamp")
    if not ts or not isinstance(ts, str):
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def _board_number_of_lives(board_result: dict[str, Any] | None) -> int | None:
    """Энергия на Telegram Board в UI совпадает с lives.numberOfLives, не с LIFE в getAssets."""
    if not isinstance(board_result, dict):
        return None
    lives = board_result.get("lives")
    if not isinstance(lives, dict):
        return None
    n = lives.get("numberOfLives")
    if n is None:
        return None
    try:
        return int(n)
    except (TypeError, ValueError):
        return None


def _lives_from_wallet_life_micro(life_raw: int, cfg: GameeConfig) -> int:
    """
    Число «жизней» по amountMicroToken валюты LIFE из getAssets, когда нет numberOfLives.

    В ответах prizes (см. new login.har) 7 жизней ↔ 7_000_000 micro → **1e6 micro на жизнь**.
    Старый дефолт life_micro_divisor=10_000_000 давал 7_000_000 // 10_000_000 == 0.
    """
    if life_raw <= 0:
        return 0
    div = cfg.life_micro_divisor if cfg.life_micro_divisor > 0 else 1_000_000
    n = life_raw // div
    if n > 0:
        return n
    alt = 1_000_000
    if div != alt:
        return life_raw // alt
    return 0


def _micro_by_currency_id(virtual_tokens: list[dict[str, Any]], currency_id: int) -> int:
    for vt in virtual_tokens:
        c = vt.get("currency")
        if not isinstance(c, dict):
            continue
        if int(c.get("id", -1)) != currency_id:
            continue
        return int(vt.get("amountMicroToken", 0))
    return 0


def _micro_by_ticker(virtual_tokens: list[dict[str, Any]], ticker: str) -> int:
    want = ticker.strip().upper()
    if not want:
        return 0
    for vt in virtual_tokens:
        c = vt.get("currency")
        if not isinstance(c, dict):
            continue
        if str(c.get("ticker") or "").upper() != want:
            continue
        return int(vt.get("amountMicroToken", 0))
    return 0


def _wallet_virtual_tokens_merged(
    get_assets_result: dict[str, Any],
    batch_user: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Кошелёк user.getAssets: virtualTokens/assets в result плюс user.assets у того же RPC-ответа.

    В Telegram-боте часть валют (в т.ч. дубликаты id) приходит в `user.assets`.
    Совпадающие currency.id позже перезаписывают (приоритет у user.assets).
    """
    by_id: dict[int, dict[str, Any]] = {}
    order: list[int] = []

    def ingest(chunk: object) -> None:
        if not isinstance(chunk, list):
            return
        for vt in chunk:
            if not isinstance(vt, dict):
                continue
            c = vt.get("currency")
            if not isinstance(c, dict):
                continue
            try:
                cid = int(c.get("id", -1))
            except (TypeError, ValueError):
                continue
            if cid < 0:
                continue
            if cid not in by_id:
                order.append(cid)
            by_id[cid] = vt

    for key in ("virtualTokens", "assets"):
        ingest(get_assets_result.get(key))
    if isinstance(batch_user, dict):
        ingest(batch_user.get("assets"))

    return [by_id[i] for i in order]


def _gold_estimated_usd_from_micro(amount_micro: int, divisor: int) -> float | None:
    """Оценка «est. $…» на сайте: GOLDPOINTS amountMicroToken / 1e12 (два деления на 1e6 в минифицированном коде)."""
    if amount_micro <= 0 or divisor <= 0:
        return None
    v = amount_micro / float(divisor)
    return v if v > 0 else None


def _friendly_reward_currency_label(name: str, ticker: str | None) -> str:
    """Краткие подписи наград (эмодзи для лога и колонок без смены заголовков таблицы)."""
    n = (name or "").strip()
    t = (ticker or "").strip()
    nl = n.lower()
    tl = t.lower()
    tu = t.upper()
    if tu in ("LIFE", "LIVES") or tl in ("life", "lives") or nl in ("lives", "life"):
        return "⚡"
    if tu == "GOLDPOINTS" or ("gold" in nl and "point" in nl):
        return "💰"
    if tu in ("TICKET", "TICKETS") or nl == "tickets":
        return "🎟️"
    if tu == "TGXP" or nl == "xp":
        return "⭐"
    if tu == "TGBOARDPROGRESS" or "telegram board progress" in nl:
        return "🎲"
    if n:
        return n
    if t:
        return t
    return "?"


def _format_reward_amount(amount_micro: int, divisor: int) -> str:
    if divisor <= 0:
        divisor = 1
    v = amount_micro / divisor
    if abs(v - round(v)) < 1e-9:
        return str(int(round(v)))
    return f"{v:.2f}".rstrip("0").rstrip(".")


def _format_rewards(
    play_result: dict[str, Any],
    divisor: int,
    *,
    skip_tickers: frozenset[str] | None = None,
) -> str:
    """Читабельные суммы: API хранит amountMicroToken (у тикетов 250000000 = 250 шт.)."""
    parts: list[str] = []
    skip = {t.upper() for t in skip_tickers} if skip_tickers else None

    def append_from(key: str) -> None:
        arr = play_result.get(key)
        if not isinstance(arr, list):
            return
        for r in arr:
            if not isinstance(r, dict):
                continue
            c = r.get("currency")
            name = "?"
            ticker: str | None = None
            if isinstance(c, dict):
                name = str(c.get("name", "") or c.get("ticker", "") or "?")
                tv = c.get("ticker")
                ticker = str(tv).upper() if tv is not None else None
            if skip and ticker and ticker in skip:
                continue
            amt = int(r.get("amountMicroToken", 0))
            label = _friendly_reward_currency_label(name, ticker)
            parts.append(f"{label} {_format_reward_amount(amt, divisor)}")

    append_from("rewards")
    append_from("luckyGameRewards")
    if not parts:
        return "—"
    return ", ".join(parts)


def _xp_from_play_result(play_result: dict[str, Any] | None, divisor: int) -> int:
    """Сумма XP (TGXP) из ответа board.play."""
    if not isinstance(play_result, dict) or divisor <= 0:
        return 0
    total = 0
    for key in ("luckyGameRewards", "rewards"):
        arr = play_result.get(key)
        if not isinstance(arr, list):
            continue
        for r in arr:
            if not isinstance(r, dict):
                continue
            c = r.get("currency")
            if not isinstance(c, dict):
                continue
            if str(c.get("ticker") or "").upper() != "TGXP":
                continue
            total += int(r.get("amountMicroToken", 0)) // divisor
    return total


def _dice_face_from_play_result(play_result: dict[str, Any] | None, divisor: int) -> int | None:
    """В ответе board.play очки кубика приходят как награда TGBOARDPROGRESS (1–6)."""
    if not isinstance(play_result, dict) or divisor <= 0:
        return None
    for key in ("luckyGameRewards", "rewards"):
        arr = play_result.get(key)
        if not isinstance(arr, list):
            continue
        for r in arr:
            if not isinstance(r, dict):
                continue
            c = r.get("currency")
            if not isinstance(c, dict):
                continue
            tv = c.get("ticker")
            if str(tv or "").upper() != "TGBOARDPROGRESS":
                continue
            amt = int(r.get("amountMicroToken", 0))
            n = amt // divisor
            if 1 <= n <= 6:
                return n
    return None


def _parse_iso_datetime_utc(raw: str | None) -> datetime | None:
    if not raw or not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _format_rewards_flat_list(rewards: list[dict[str, Any]], divisor: int) -> str:
    """Список наград как в ответе dailyCheckin.claim."""
    parts: list[str] = []
    for r in rewards:
        if not isinstance(r, dict):
            continue
        c = r.get("currency")
        name = "?"
        ticker: str | None = None
        if isinstance(c, dict):
            name = str(c.get("name", "") or c.get("ticker", "") or "?")
            tv = c.get("ticker")
            ticker = str(tv).upper() if tv is not None else None
        amt = int(r.get("amountMicroToken", 0))
        label = _friendly_reward_currency_label(name, ticker)
        parts.append(f"{label} {_format_reward_amount(amt, divisor)}")
    if not parts:
        return "—"
    return ", ".join(parts)


def _format_activity_rewards_blob(rewards: Any, divisor: int) -> str:
    if isinstance(rewards, list):
        return _format_rewards_flat_list(rewards, divisor)
    if not isinstance(rewards, dict):
        return "—"
    parts: list[str] = []
    virtual_tokens = rewards.get("virtualTokens")
    if isinstance(virtual_tokens, list):
        vt = _format_rewards_flat_list(virtual_tokens, divisor)
        if vt and vt != "—":
            parts.append(vt)
    for key, label in (("gems", "Gem"), ("tickets", "Tickets")):
        try:
            amount = int(rewards.get(key, 0) or 0)
        except (TypeError, ValueError):
            amount = 0
        if amount > 0:
            parts.append(f"{label} {amount}")
    return ", ".join(parts) if parts else "—"


def _activities_from_result(result: dict[str, Any]) -> list[dict[str, Any]]:
    raw = result.get("activities")
    if isinstance(raw, dict):
        for key in ("items", "activities", "data"):
            nested = raw.get(key)
            if isinstance(nested, list):
                raw = nested
                break
    if not isinstance(raw, list):
        return []
    return [a for a in raw if isinstance(a, dict)]


def _activities_get_params(
    offset: int = 0,
    limit: int = _ACTIVITIES_PAGE_LIMIT,
) -> dict[str, Any]:
    return {"pagination": {"offset": max(0, int(offset)), "limit": max(1, int(limit))}}


def _activity_id(activity: dict[str, Any]) -> int | None:
    try:
        return int(activity.get("id"))
    except (TypeError, ValueError):
        return None


def _activity_name(activity: dict[str, Any]) -> str:
    metadata = activity.get("metadata")
    if isinstance(metadata, dict):
        name = str(metadata.get("name") or "").strip()
        if name:
            return name
    typ = str(activity.get("type") or "").strip()
    aid = _activity_id(activity)
    if typ and aid is not None:
        return f"{typ} #{aid}"
    return typ or (f"activity #{aid}" if aid is not None else "activity")


def _activity_claimable_board_reward(activity: dict[str, Any]) -> bool:
    if _json_bool_or_none(activity.get("isClaimed")) is True:
        return False
    if str(activity.get("type") or "") != "task_gamee_collect_currency":
        return False
    return _activity_id(activity) is not None


def _format_activity_claim_rewards(
    claim_result: dict[str, Any] | None,
    source_activity: dict[str, Any],
    divisor: int,
) -> str:
    if isinstance(claim_result, dict):
        rewards_text = _format_activity_rewards_blob(
            claim_result.get("rewards"),
            divisor,
        )
        if rewards_text != "—":
            return rewards_text
        activities = _activities_from_result(claim_result)
        if activities:
            rewards_text = _format_activity_rewards_blob(
                activities[0].get("rewards"),
                divisor,
            )
            if rewards_text != "—":
                return rewards_text
    return _format_activity_rewards_blob(source_activity.get("rewards"), divisor)


def _json_bool_or_none(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return value != 0
    if isinstance(value, str):
        v = value.strip().lower()
        if v in {"1", "true", "yes", "y", "on"}:
            return True
        if v in {"0", "false", "no", "n", "off", ""}:
            return False
    return None


def _format_check_task_code_result(result: dict[str, Any], cfg: GameeConfig) -> str:
    """Человекочитаемо: ответ telegram.checkTask.code (rewards + completed)."""
    parts: list[str] = []
    if _json_bool_or_none(result.get("completed")) is True:
        parts.append("выполнено")
    rewards = result.get("rewards")
    if not isinstance(rewards, list):
        return " · ".join(parts) if parts else "OK"
    for r in rewards:
        if not isinstance(r, dict):
            continue
        c = r.get("currency")
        if not isinstance(c, dict):
            continue
        try:
            cid = int(c.get("id", -1))
        except (TypeError, ValueError):
            cid = -1
        name = str(c.get("name", "") or c.get("ticker", "") or "?")
        tv = c.get("ticker")
        ticker = str(tv).upper() if tv is not None else None
        amt = int(r.get("amountMicroToken", 0))
        if cid == cfg.gold_currency_id:
            div = cfg.gold_micro_divisor
        elif cid == cfg.ticket_currency_id:
            div = cfg.ticket_micro_divisor
        elif cid == cfg.life_currency_id:
            div = cfg.life_micro_divisor
        elif cid == cfg.money_currency_id:
            div = cfg.money_micro_divisor
        else:
            div = cfg.reward_micro_divisor if cfg.reward_micro_divisor > 0 else 1_000_000
        label = _friendly_reward_currency_label(name, ticker)
        parts.append(f"{label} {_format_reward_amount(amt, div)}")
    return " · ".join(parts) if parts else "OK"


@dataclass
class DailyCheckinSnapshot:
    claimed_today: bool
    next_available_utc: datetime | None
    streak: int
    streak_total: int = 0  # len(dailyCheckinDays), обычно 14
    api_error: str | None = None
    claim_available: bool | None = None

    def can_claim_now(self, now: datetime | None = None) -> bool:
        if self.api_error:
            return False
        if self.claim_available is not None:
            return bool(self.claim_available)
        if self.claimed_today:
            return False
        if now is None:
            now = datetime.now(timezone.utc)
        if self.next_available_utc is None:
            return True
        na = self.next_available_utc
        if na.tzinfo is None:
            na = na.replace(tzinfo=timezone.utc)
        return now >= na


def _daily_claim_available_from_result(result: dict[str, Any]) -> bool | None:
    flag_keys = (
        "claimAvailable",
        "canClaim",
        "canClaimToday",
        "availableToClaim",
        "isClaimAvailable",
    )
    for key in (
        *flag_keys,
        "claimable",
    ):
        if key in result:
            value = _json_bool_or_none(result.get(key))
            if value is not None:
                return value
    status = str(result.get("status") or result.get("state") or "").strip().lower()
    if status in {"available", "claimable", "ready", "can_claim"}:
        return True
    if status in {"claimed", "locked", "unavailable", "not_available", "cooldown"}:
        return False
    days = result.get("dailyCheckinDays")
    if not isinstance(days, list):
        return None
    for day in days:
        if not isinstance(day, dict):
            continue
        for key in (*flag_keys, "claimable"):
            if key not in day:
                continue
            value = _json_bool_or_none(day.get(key))
            if value is True:
                return True
        day_status = str(day.get("status") or day.get("state") or "").strip().lower()
        if day_status in {"available", "claimable", "ready", "can_claim"}:
            return True
        for nested_key in ("reward", "dailyReward", "prize"):
            nested = day.get(nested_key)
            if not isinstance(nested, dict):
                continue
            for key in flag_keys:
                if key not in nested:
                    continue
                value = _json_bool_or_none(nested.get(key))
                if value is True:
                    return True
    return None


def _daily_checkin_from_result(result: dict[str, Any]) -> DailyCheckinSnapshot:
    claimed = _json_bool_or_none(result.get("claimedToday")) is True
    next_dt = None
    for key in (
        "nextClaimAvailableTimestamp",
        "nextDailyClaimTimestamp",
        "nextAvailableTimestamp",
        "nextClaimTimestamp",
    ):
        next_dt = _parse_iso_datetime_utc(result.get(key))
        if next_dt is not None:
            break
    claim_available = _daily_claim_available_from_result(result)
    try:
        streak = int(result.get("streak") or 0)
    except (TypeError, ValueError):
        streak = 0
    days = result.get("dailyCheckinDays")
    streak_total = len(days) if isinstance(days, list) else 0
    return DailyCheckinSnapshot(
        claimed_today=claimed,
        next_available_utc=next_dt,
        streak=streak,
        streak_total=streak_total,
        api_error=None,
        claim_available=claim_available,
    )


@dataclass
class SeasonPassProgress:
    """Сезонный пропуск (ветка TGXP в rewardedProgress.getAll)."""

    free_claimed: int
    premium_claimed: int
    total_milestones: int
    collected_amount_micro: int
    claimable_free_milestone_ids: list[int]
    claimable_premium_milestone_ids: list[int]

    def to_cell(self, cfg: GameeConfig, last_claim_summary: str = "") -> str:
        div = cfg.reward_micro_divisor if cfg.reward_micro_divisor > 0 else 1
        xp = self.collected_amount_micro // div
        n_ready = len(self.claimable_free_milestone_ids) + len(
            self.claimable_premium_milestone_ids
        )
        base = (
            f"{self.free_claimed}/{self.total_milestones} "
            f"· прем {self.premium_claimed}/{self.total_milestones} · ⭐{xp}"
        )
        if n_ready > 0:
            # Вех с claimAvailable=True по ветке reward / premiumReward.
            base += f" · к клейму: {n_ready}"
        s = (last_claim_summary or "").strip()
        if s:
            return f"{base} · {s}"
        return base


def _season_program_from_get_all_result(result: dict[str, Any]) -> dict[str, Any] | None:
    rps = result.get("rewardedProgress")
    if not isinstance(rps, list):
        return None
    for p in rps:
        if not isinstance(p, dict):
            continue
        cc = p.get("collectCurrency") or {}
        if str(cc.get("ticker") or "").upper() == "TGXP":
            return p
        if str(p.get("name") or "").strip() == "Season Pass":
            return p
    return None


def _season_pass_progress_from_program(prog: dict[str, Any]) -> SeasonPassProgress | None:
    ms = prog.get("milestones")
    if not isinstance(ms, list) or not ms:
        return None
    total = len(ms)
    free_claimed = 0
    premium_claimed = 0
    claimable: list[int] = []
    claimable_premium: list[int] = []
    for m in ms:
        if not isinstance(m, dict):
            continue
        mid = m.get("id")
        mid_ok = isinstance(mid, int)
        rew = m.get("reward")
        if isinstance(rew, dict):
            if rew.get("claimedAt"):
                free_claimed += 1
            elif rew.get("claimAvailable") is True and mid_ok:
                claimable.append(mid)
        prem = m.get("premiumReward")
        if isinstance(prem, dict):
            if prem.get("claimedAt"):
                premium_claimed += 1
            elif prem.get("claimAvailable") is True and mid_ok:
                claimable_premium.append(mid)
    claimable.sort()
    claimable_premium.sort()
    try:
        coll = int(prog.get("collectedAmountMicroToken") or 0)
    except (TypeError, ValueError):
        coll = 0
    return SeasonPassProgress(
        free_claimed=free_claimed,
        premium_claimed=premium_claimed,
        total_milestones=total,
        collected_amount_micro=coll,
        claimable_free_milestone_ids=claimable,
        claimable_premium_milestone_ids=claimable_premium,
    )


@dataclass
class AccountGameState:
    energy: int
    gold: int
    tickets: int
    usd_cents: int
    gold_estimated_usd: float | None = None
    last_error: str | None = None
    next_live_at_utc: datetime | None = None


@dataclass
class PlayOutcome:
    ok: bool
    before: AccountGameState
    after: AccountGameState | None = None
    # Награды с клетки без очков кубика (TGBOARDPROGRESS) — число кубика отдельно в dice_value.
    rewards_text: str = ""
    dice_value: int | None = None
    xp_gained: int = 0
    error: str | None = None


@dataclass(frozen=True)
class ActivityRewardClaim:
    activity_id: int
    name: str
    rewards_text: str


@dataclass
class GameeSession:
    init_data: str
    install_uuid: str
    # http_profile: TLS + заголовки API, постоянны для label (gamee_http_profile_for_label).
    http_profile: GameeHttpClientProfile
    auth_token: str | None = None
    money_usd_cents: int = 0
    telegram_referral_ref: int | None = None
    referral_linked: bool = False
    accounts_yaml_path: Path | None = None
    account_label: str | None = None

    def token_valid(self, skew_seconds: int = 120) -> bool:
        if not self.auth_token:
            return False
        exp = _jwt_expiry_unix(self.auth_token)
        if exp is None:
            return True
        return time.time() < float(exp) - skew_seconds


class GameeClient:
    _request_semaphore = threading.Semaphore(_HTTP_REQUEST_MAX_PARALLEL)
    _request_gate_lock = threading.Lock()
    _next_request_not_before = 0.0
    # ADAPT-1/2: cluster-wide rate-limit awareness (per-proxy multiplier)
    # Когда один аккаунт ловит 429, все аккаунты с того же proxy замедляются.
    _rate_limit_state: dict[str, tuple[float, float]] = {}  # proxy -> (last_429_ts, multiplier)
    _rate_limit_lock = threading.Lock()

    def __init__(
        self,
        cfg: GameeConfig,
        *,
        proxy_url: str | None = None,
        http_profile: GameeHttpClientProfile,
        transport: GameeTransport | None = None,
        account_label: str = "",
        cookie_base_dir: Any = None,
    ) -> None:
        self._cfg = cfg
        self._transport = transport or build_default_gamee_transport(
            backend_name=cfg.transport_backend,
            proxy_url=proxy_url,
            http_profile=http_profile,
            account_label=account_label,
            cookie_base_dir=cookie_base_dir,
        )
        self._proxy_url = self._transport.proxy_url
        # Один раз за жизнь клиента: GET prizes → cookie / контекст для Cloudflare перед API.
        self._browser_warmup_done = False
        # Gamee validates JSON-RPC id format; keep request ids equal to method names.
        # NET-5: assets cache на 30с (избегает predictable get_assets_state перед каждым play)
        self._assets_cache: tuple[AccountGameState, float] | None = None
        self._assets_cache_ttl_sec = 30.0

    @property
    def proxy_url(self) -> str | None:
        """Текущий нормализованный URL прокси (или None = без прокси)."""
        return self._proxy_url

    @property
    def telemetry_generator(self) -> Any:
        """Input telemetry generator из transport (может быть None)."""
        return getattr(self._transport, "telemetry_generator", None)

    def simulate_play_tap(self, session: GameeSession) -> None:
        """Генерировать tap-событие на кнопку Play и выдержать реалистичную паузу.

        Не ломает основной поток при любой ошибке. Попутно пробует
        отправить telemetry-payload на analytics-endpoint (non-fatal).
        """
        try:
            gen = self.telemetry_generator
            if gen is None:
                return
            prof = session.http_profile
            cx = prof.screen_width * 0.5
            cy = prof.screen_height * 0.7
            events = gen.generate_tap(cx, cy, target="button.play-btn")
            if events:
                duration_ms = events[-1].timestamp - events[0].timestamp
                time.sleep(max(0.0, duration_ms / 1000.0))
            payloads = gen.assemble_telemetry_payload(events)
            if not payloads:
                return
            analytics_url = self._cfg.api_url.rstrip("/").replace("api2", "analytics") + "/events"
            for batch in payloads:
                try:
                    self._transport.send(GameeTransportRequest(
                        method="POST",
                        url=analytics_url,
                        headers=self._headers(session),
                        data=json.dumps(batch.to_payload(), ensure_ascii=False).encode(),
                        timeout=(5.0, 10.0),
                        purpose="telemetry",
                    ))
                except Exception:
                    pass
        except Exception:
            pass

    def close(self) -> None:
        self._transport.close()

    @classmethod
    def _reserve_request_delay(cls) -> float:
        with cls._request_gate_lock:
            now = time.monotonic()
            slot_at = max(now, cls._next_request_not_before)
            cls._next_request_not_before = (
                slot_at
                + _HTTP_REQUEST_START_GAP_SEC
                + random.uniform(0.0, _HTTP_REQUEST_START_JITTER_SEC)
            )
        return max(0.0, slot_at - now)

    @staticmethod
    def _retry_delay_seconds(attempt: int) -> float:
        base = min(1.25 * (2**attempt), 18.0)
        return base + random.uniform(0.25, min(1.75, max(0.25, base * 0.2)))

    @classmethod
    def _note_rate_limit(cls, proxy_key: str) -> None:
        """Зафиксировать 429 от данного proxy → все аккаунты с этого IP замедлятся."""
        with cls._rate_limit_lock:
            cls._rate_limit_state[proxy_key] = (time.time(), 2.5)

    @classmethod
    def _rate_limit_multiplier(cls, proxy_key: str) -> float:
        """Текущий slowdown multiplier (decays exponentially за 5 минут)."""
        with cls._rate_limit_lock:
            entry = cls._rate_limit_state.get(proxy_key)
        if entry is None:
            return 1.0
        last_ts, mult = entry
        elapsed = time.time() - last_ts
        if elapsed > 300.0:
            return 1.0
        # Экспоненциальное decay: каждую минуту делим на 2
        decay = 0.5 ** (elapsed / 60.0)
        return 1.0 + (mult - 1.0) * decay

    def _call_with_http_gate(self, fn):
        self.__class__._request_semaphore.acquire()
        try:
            delay = self.__class__._reserve_request_delay()
            # ADAPT-2: если по нашему proxy недавно был 429, добавляем slowdown
            proxy_key = self._proxy_url or "_direct"
            mult = self.__class__._rate_limit_multiplier(proxy_key)
            if mult > 1.0:
                delay *= mult
            if delay > 0:
                time.sleep(delay)
            return fn()
        finally:
            self.__class__._request_semaphore.release()

    def _ensure_prizes_page_warmup(self, session: GameeSession) -> None:
        """GET главной prizes — те же TLS/cookie jar, что и у POST api2 (как заход из браузера).

        Bootstrap diversification (NET-2):
        - случайная задержка 50-300мс перед GET (имитация загрузки/рендера)
        - 30% шанс попутно подгрузить favicon.ico (как реальный браузер)
        """
        if self._browser_warmup_done:
            return
        p = session.http_profile
        headers = p.ordered_navigation_headers()
        # Random delay — имитация задержки JS bundle parse + render
        time.sleep(random.uniform(0.05, 0.30))
        try:
            resp = self._call_with_http_gate(
                lambda: self._transport.send(
                    GameeTransportRequest(
                        method="GET",
                        url="https://prizes.gamee.com/",
                        headers=headers,
                        timeout=(15.0, 35.0),
                        allow_redirects=True,
                        purpose="bootstrap_page",
                    )
                )
            )
        except GameeTransportError:
            return
        code = int(getattr(resp, "status_code", 0) or 0)
        if 200 <= code < 400:
            self._browser_warmup_done = True
        # 30% шанс — favicon.ico (так делает любой браузер при первой загрузке)
        if self._browser_warmup_done and random.random() < 0.30:
            try:
                fav_headers = p.ordered_navigation_headers()
                fav_headers["accept"] = "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8"
                fav_headers["sec-fetch-dest"] = "image"
                fav_headers["sec-fetch-mode"] = "no-cors"
                fav_headers["sec-fetch-site"] = "same-origin"
                fav_headers["referer"] = "https://prizes.gamee.com/"
                self._call_with_http_gate(
                    lambda: self._transport.send(
                        GameeTransportRequest(
                            method="GET",
                            url="https://prizes.gamee.com/favicon.ico",
                            headers=fav_headers,
                            timeout=(5.0, 10.0),
                            allow_redirects=True,
                            purpose="favicon",
                        )
                    )
                )
            except Exception:
                pass

    def _headers(self, session: GameeSession) -> dict[str, str]:
        # Профиль согласован с TLS (session.http_profile ↔ self._http_profile при правильном использовании).
        # Strict Chrome Android header ordering via OrderedDict.
        p = session.http_profile
        auth = session.auth_token if session.auth_token and session.token_valid() else ""
        return p.ordered_api_headers(
            install_uuid=session.install_uuid,
            auth_token=auth,
        )


    @staticmethod
    def _randomize_batch(body):
        """Vary batch composition to avoid static fingerprinting."""
        if not isinstance(body, list) or len(body) < 2:
            return body
        ids_in_batch = {item.get("id", "") for item in body}
        if ids_in_batch & _BATCH_NO_RANDOMIZE_IDS:
            return body
        items = list(body)
        if random.random() < 0.65:
            random.shuffle(items)
        if random.random() < 0.35:
            noise_method = random.choice(_BATCH_NOISE_METHODS)
            if noise_method not in ids_in_batch:
                noise_item = {"jsonrpc": "2.0", "id": noise_method, "method": noise_method, "params": {}}
                items.insert(random.randint(0, len(items)), noise_item)
        return items
    def _post_batch_raw(self, session: GameeSession, body: list[dict[str, Any]] | dict[str, Any]) -> list[dict[str, Any]]:
        """Один POST без перелогина. Для loginUsingTelegram — только так, иначе рекурсия."""
        url = self._cfg.api_url.rstrip("/") + "/"
        send_body = self._randomize_batch(body) if isinstance(body, list) else body
        # Gamee validates JSON-RPC id format. Do not randomize ids here:
        # loginUsingTelegram returns -32700 "Invalid [id] format" otherwise.
        id_map: dict[Any, str] = {}
        payload = json.dumps(send_body, ensure_ascii=False)
        r = None
        for attempt in range(_MAX_HTTP_TRANSIENT_RETRIES):
            try:
                r = self._call_with_http_gate(
                    lambda: self._transport.send(
                        GameeTransportRequest(
                            method="POST",
                            url=url,
                            headers=self._headers(session),
                            data=payload.encode("utf-8"),
                            timeout=(15.0, 50.0),
                            purpose="api_jsonrpc",
                        )
                    )
                )
            except GameeTransportError:
                if attempt + 1 < _MAX_HTTP_TRANSIENT_RETRIES:
                    time.sleep(self._retry_delay_seconds(attempt))
                    continue
                raise
            code = int(r.status_code)
            if code == 429:
                # ADAPT-1: фиксируем rate-limit для proxy → все аккаунты с него замедлятся
                proxy_key = self._proxy_url or "_direct"
                self.__class__._note_rate_limit(proxy_key)
                r.raise_for_status()
            if code in _RETRYABLE_HTTP_STATUS and attempt + 1 < _MAX_HTTP_TRANSIENT_RETRIES:
                time.sleep(self._retry_delay_seconds(attempt))
                continue
            r.raise_for_status()
            break
        assert r is not None
        data = r.json()
        # Восстанавливаем id → method для совместимости с _by_id.
        def _restore_ids(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
            if not id_map:
                return rows
            for row in rows:
                if isinstance(row, dict) and "id" in row:
                    mapped = id_map.get(row["id"])
                    if mapped is not None:
                        row["id"] = mapped
            return rows
        if isinstance(data, list):
            return _restore_ids(data)
        if isinstance(data, dict):
            return _restore_ids([data])
        raise RuntimeError("Неожиданный ответ API")

    def _post_batch(self, session: GameeSession, body: list[dict[str, Any]] | dict[str, Any]) -> list[dict[str, Any]]:
        """POST с автоматическим перелогином только для явного протухания сессии."""
        try:
            return self._post_batch_raw(session, body)
        except GameeTransportHTTPError as e:
            code = _http_status_from_error(e)
            raw = ""
            resp = getattr(e, "response", None)
            if resp is not None:
                try:
                    raw = resp.text or ""
                except Exception:
                    raw = ""
            should_relogin = code in _HTTP_RELOGIN_STATUS_CODES
            if not should_relogin and code == 403 and raw and _jsonrpc_message_suggests_relogin(raw):
                should_relogin = True
            if should_relogin:
                session.auth_token = None
                session.referral_linked = False
                self.login_telegram(session)
                return self._post_batch_raw(session, body)
            raise

    def _force_relogin(self, session: GameeSession) -> None:
        """Сброс токена и полный loginUsingTelegram (свежий JWT)."""
        session.auth_token = None
        session.referral_linked = False
        self.login_telegram(session)

    @staticmethod
    def _invalidate_init_cache(session: GameeSession) -> None:
        """Сбросить кеш initData для аккаунта — следующий вызов resolve_init_data получит свежий."""
        try:
            from gamee_bot.telethon_bridge import clear_init_cache
            label = (session.account_label or "").strip()
            if label:
                clear_init_cache(label)
        except Exception:
            pass

    @staticmethod
    def _by_id(rows: list[dict[str, Any]], req_id: str) -> dict[str, Any]:
        for row in rows:
            if row.get("id") == req_id:
                return row
        # Defensive: build a summary of what we DID get back for diagnostics
        got_ids = [r.get("id", "<no id>") for r in rows] if rows else []
        preview = str(rows)[:500] if rows else "<empty>"
        raise RuntimeError(
            f"API ответ не содержит '{req_id}'. "
            f"Получены ID: {got_ids}. Тело: {preview}"
        )

    def _refresh_init_data_via_telethon(self, session: GameeSession) -> bool:
        """Сбросить кеш initData. Worker подберёт fresh data в следующем цикле.

        Раньше эта функция сразу делала Telethon WebView request (5-30с блокировка),
        что приводило к зависанию интерфейса. Теперь только invalidate — быстро.
        """
        try:
            self._invalidate_init_cache(session)
        except Exception:
            pass
        return False  # worker через _session_for сделает fresh resolve

    def _refresh_init_data_via_telethon_DISABLED(self, session: GameeSession) -> bool:
        """[DISABLED] Прямой Telethon refresh — слишком медленно, блокирует поток."""
        label = (session.account_label or "").strip()
        if not label:
            return False
        try:
            self._invalidate_init_cache(session)
            from gamee_bot.telethon_bridge import resolve_init_data
            from gamee_bot.account_store import load_accounts
            from gamee_bot.config import load_config
            yaml_path = session.accounts_yaml_path
            if yaml_path is None:
                return False
            accounts = load_accounts(yaml_path)
            acc = next((a for a in accounts if a.label.strip() == label), None)
            if acc is None or not acc.telethon_session:
                return False
            cfg_path = yaml_path.parent / "config.yaml"
            if not cfg_path.exists():
                return False
            cfg = load_config(cfg_path)
            gr = None if acc.gamee_preexisting else acc.gamee_ref
            fresh = resolve_init_data(
                acc.label,
                acc.init_data or "",
                acc.telethon_session,
                cfg,
                account_gamee_ref=gr,
            )
            if fresh and fresh != session.init_data:
                session.init_data = fresh
                return True
        except Exception:
            pass
        return False

    def login_telegram(self, session: GameeSession) -> None:
        """Вход с быстрым retry на transient server errors (-32603).

        Стратегия:
        - 1 попытка сразу
        - При -32603: refresh init_data + 2-5с пауза + 1 retry
        - Если и retry упал — отдаём управление worker'у
          (он сам сделает повторы через error_cooldown)
        """
        try:
            self._login_telegram_once(session)
            return
        except GameeTransientServerError:
            pass  # пробуем ещё раз с fresh init_data
        # Один retry с обновлением init_data
        try:
            self._refresh_init_data_via_telethon(session)
        except Exception:
            pass
        time.sleep(random.uniform(2.0, 5.0))
        # На втором заходе — если опять -32603, поднимаем без traceback
        self._login_telegram_once(session)

    def _login_telegram_once(self, session: GameeSession) -> None:
        """Одна попытка login. Поднимает GameeTransientServerError для -32603."""
        batch = [
            {"jsonrpc": "2.0", "id": "app.telegram.get", "method": "app.telegram.get", "params": {}},
            {
                "jsonrpc": "2.0",
                "id": "user.authentication.loginUsingTelegram",
                "method": "user.authentication.loginUsingTelegram",
                "params": {"initData": session.init_data},
            },
        ]
        session.auth_token = None
        self._ensure_prizes_page_warmup(session)
        try:
            rows = self._post_batch_raw(session, batch)
        except GameeTransportHTTPError as e:
            resp = getattr(e, "response", None)
            raw = ""
            if resp is not None:
                raw = resp.text or ""
            hint = _api_error_body_hint(raw)
            self._invalidate_init_cache(session)
            raise RuntimeError(
                f"loginUsingTelegram: HTTP {resp.status_code if resp is not None else '?'} "
                f"от {self._cfg.api_url!r} — {hint}"
            ) from e
        if not rows or not isinstance(rows, list):
            self._invalidate_init_cache(session)
            raise GameeTransientServerError(
                f"loginUsingTelegram: пустой ответ от API: {str(rows)[:200]}"
            )

        # Handle batch-level server errors (id=None) — invalid initData / overload.
        for row in rows:
            if row.get("id") is None and "error" in row:
                err_obj = row["error"]
                code = err_obj.get("code", "?")
                msg = err_obj.get("message", "unknown")
                detail = err_obj.get("data", {})
                reason = detail.get("reason", "") if isinstance(detail, dict) else str(detail)
                self._invalidate_init_cache(session)
                # -32603 (Server error) — transient, retry в login_telegram
                if code == -32603:
                    raise GameeTransientServerError(
                        f"Server error (code={code}, msg={msg}, reason={reason})"
                    )
                # Другие ошибки — не transient, не делаем retry
                raise RuntimeError(
                    f"loginUsingTelegram: сервер отклонил запрос "
                    f"(code={code}, message={msg}, reason={reason})."
                )

        login_row = self._by_id(rows, "user.authentication.loginUsingTelegram")
        if "error" in login_row:
            self._invalidate_init_cache(session)
            raise RuntimeError(str(login_row["error"]))
        result = login_row.get("result")
        if not isinstance(result, dict):
            self._invalidate_init_cache(session)
            raise RuntimeError("login: нет result")
        token = _pick_token_from_login_result(result)
        if not token:
            self._invalidate_init_cache(session)
            raise RuntimeError("login: нет authenticate token")
        session.auth_token = token
        session.money_usd_cents = 0
        yaml_path = session.accounts_yaml_path
        label = (session.account_label or "").strip()
        is_new = _login_result_is_brand_new_gamee_user(result)
        if not is_new:
            session.telegram_referral_ref = None
            session.referral_linked = True
            if yaml_path is not None and label:
                set_account_gamee_registration_state(
                    yaml_path, label, brand_new_user=False
                )
                clear_init_cache(label)
        else:
            if yaml_path is not None and label:
                set_account_gamee_registration_state(
                    yaml_path, label, brand_new_user=True
                )
            self._try_link_telegram_referral(session)

    def _try_link_telegram_referral(self, session: GameeSession) -> None:
        rid = session.telegram_referral_ref
        if rid is None:
            session.referral_linked = True
            return
        body = [
            {
                "jsonrpc": "2.0",
                "id": "user.linkTelegramReferral",
                "method": "user.linkTelegramReferral",
                "params": {"ref": int(rid)},
            }
        ]
        rows = self._post_batch_raw(session, body)
        row = self._by_id(rows, "user.linkTelegramReferral")
        if "error" not in row:
            session.referral_linked = True
            return
        err = row["error"]
        msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
        low = str(msg).lower()
        if any(
            x in low
            for x in (
                "already",
                "exist",
                "bound",
                "duplicate",
                "linked",
                "invalid",
            )
        ):
            session.referral_linked = True
            return
        raise RuntimeError(f"user.linkTelegramReferral: {msg}")

    def ensure_session(self, session: GameeSession) -> None:
        if not session.token_valid():
            session.referral_linked = False
            self.login_telegram(session)
        elif session.telegram_referral_ref is not None and not session.referral_linked:
            self._try_link_telegram_referral(session)

    def submit_check_task_code(
        self, session: GameeSession, *, task_id: int, code: str, _relogin: bool = False
    ) -> tuple[bool, str]:
        """telegram.checkTask.code — промокод с prizes.gamee.com (см. HAR)."""
        raw = (code or "").strip()
        if not raw:
            return False, "пустой код"
        try:
            tid = int(task_id)
        except (TypeError, ValueError):
            return False, "Task ID должен быть числом"
        if tid < 1:
            return False, "Task ID должен быть положительным"
        self.ensure_session(session)
        body = [
            {
                "jsonrpc": "2.0",
                "id": "telegram.checkTask.code",
                "method": "telegram.checkTask.code",
                "params": {"taskId": tid, "code": raw},
            }
        ]
        rows = self._post_batch(session, body)
        row = self._by_id(rows, "telegram.checkTask.code")
        if "error" in row:
            err = row["error"]
            if not _relogin and _jsonrpc_error_suggests_relogin(err):
                self._force_relogin(session)
                return self.submit_check_task_code(
                    session, task_id=tid, code=raw, _relogin=True
                )
            return False, _jsonrpc_error_message(err)
        result = row.get("result")
        if not isinstance(result, dict):
            return True, "OK"
        if _json_bool_or_none(result.get("completed")) is False:
            reason_parts: list[str] = []
            for key in ("message", "reason", "status", "state", "error", "code"):
                value = result.get(key)
                if value is not None and str(value).strip():
                    reason_parts.append(f"{key}={value}")
            reason = ", ".join(reason_parts)
            return False, f"не выполнено{': ' + reason if reason else ''}"
        return True, _format_check_task_code_result(result, self._cfg)

    def get_assets_state(self, session: GameeSession, _relogin: bool = False) -> AccountGameState:
        self.ensure_session(session)
        rows = self._post_batch(
            session,
            [
                {"jsonrpc": "2.0", "id": "user.getAssets", "method": "user.getAssets", "params": {}},
                {"jsonrpc": "2.0", "id": "luckyGame.board.get", "method": "luckyGame.board.get", "params": {}},
            ],
        )
        row = self._by_id(rows, "user.getAssets")
        if "error" in row:
            err = row["error"]
            msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
            if not _relogin and _jsonrpc_message_suggests_relogin(str(msg)):
                self._force_relogin(session)
                return self.get_assets_state(session, _relogin=True)
            return AccountGameState(
                energy=0,
                gold=0,
                tickets=0,
                usd_cents=0,
                gold_estimated_usd=None,
                last_error=str(msg),
                next_live_at_utc=None,
            )
        result = row.get("result")
        if not isinstance(result, dict):
            return AccountGameState(
                0,
                0,
                0,
                0,
                gold_estimated_usd=None,
                last_error="getAssets: пусто",
                next_live_at_utc=None,
            )
        batch_user = row.get("user") if isinstance(row.get("user"), dict) else None
        vt = _wallet_virtual_tokens_merged(result, batch_user)
        life_raw = _micro_by_currency_id(vt, self._cfg.life_currency_id)
        gold_raw = _micro_by_currency_id(vt, self._cfg.gold_currency_id)
        if gold_raw <= 0:
            gold_raw = _micro_by_ticker(vt, "GOLDPOINTS")
        ticket_raw = _micro_by_currency_id(vt, self._cfg.ticket_currency_id)
        if ticket_raw <= 0:
            ticket_raw = _micro_by_ticker(vt, "TICKET")
        gold = gold_raw // self._cfg.gold_micro_divisor
        tickets = ticket_raw // self._cfg.ticket_micro_divisor
        gold_est_usd = _gold_estimated_usd_from_micro(
            gold_raw, self._cfg.gold_estimate_usd_micro_divisor
        )

        board_row = self._by_id(rows, "luckyGame.board.get")
        energy: int
        err_extra: str | None = None
        next_live: datetime | None = None
        br_dict: dict[str, Any] | None = None
        if "error" not in board_row:
            br = board_row.get("result")
            br_dict = br if isinstance(br, dict) else None
            n = _board_number_of_lives(br_dict)
            energy = (
                n
                if n is not None
                else _lives_from_wallet_life_micro(life_raw, self._cfg)
            )
            next_live = _board_next_live_added_utc(br_dict)
        else:
            berr = board_row["error"]
            bmsg = berr.get("message", str(berr)) if isinstance(berr, dict) else str(berr)
            if not _relogin and _jsonrpc_message_suggests_relogin(str(bmsg)):
                self._force_relogin(session)
                return self.get_assets_state(session, _relogin=True)
            energy = _lives_from_wallet_life_micro(life_raw, self._cfg)
            if _board_get_error_is_missing_reward_progress(berr):
                err_extra = None
            else:
                err_extra = (
                    f"board.get: {bmsg} (энергия запасной оценкой из getAssets)"
                )

        return AccountGameState(
            energy=energy,
            gold=gold,
            tickets=tickets,
            usd_cents=0,
            gold_estimated_usd=gold_est_usd,
            last_error=err_extra,
            next_live_at_utc=next_live,
        )

    def play_board(self, session: GameeSession, *, _relogin: bool = False) -> PlayOutcome:
        self.ensure_session(session)
        # NET-5: 60% попыток использовать кешированный state если ему < 30с
        before: AccountGameState | None = None
        if self._assets_cache is not None and random.random() < 0.60:
            cached_state, cached_at = self._assets_cache
            if time.monotonic() - cached_at < self._assets_cache_ttl_sec:
                before = cached_state
        if before is None:
            before = self.get_assets_state(session)
            self._assets_cache = (before, time.monotonic())
        if before.last_error:
            return PlayOutcome(ok=False, before=before, error=before.last_error)

        batch = [
            {"jsonrpc": "2.0", "id": "luckyGame.board.play", "method": "luckyGame.board.play", "params": {}},
            {"jsonrpc": "2.0", "id": "luckyGame.board.get", "method": "luckyGame.board.get", "params": {}},
        ]
        try:
            rows = self._post_batch(session, batch)
        except Exception as e:
            return PlayOutcome(ok=False, before=before, error=str(e))

        play_row = self._by_id(rows, "luckyGame.board.play")
        if "error" in play_row:
            err = play_row["error"]
            msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
            if not _relogin and _jsonrpc_message_suggests_relogin(str(msg)):
                self._force_relogin(session)
                return self.play_board(session, _relogin=True)
            return PlayOutcome(ok=False, before=before, error=msg)

        play_res = play_row.get("result")
        div = self._cfg.reward_micro_divisor
        dice: int | None = None
        rewards_text = ""
        xp_gained = 0
        if isinstance(play_res, dict):
            dice = _dice_face_from_play_result(play_res, div)
            xp_gained = _xp_from_play_result(play_res, div)
            rewards_text = _format_rewards(
                play_res,
                div,
                skip_tickers=frozenset({"TGBOARDPROGRESS"}),
            )

        after = self.get_assets_state(session)
        # NET-5: обновляем кеш свежим состоянием
        self._assets_cache = (after, time.monotonic())
        return PlayOutcome(
            ok=True,
            before=before,
            after=after,
            rewards_text=rewards_text,
            dice_value=dice,
            xp_gained=xp_gained,
        )

    def get_daily_checkin_snapshot(
        self, session: GameeSession, *, _relogin: bool = False
    ) -> DailyCheckinSnapshot:
        """Ответ dailyCheckin.getInformation (без клейма)."""
        self.ensure_session(session)
        try:
            rows = self._post_batch(
                session,
                [
                    {
                        "jsonrpc": "2.0",
                        "id": "dailyCheckin.getInformation",
                        "method": "dailyCheckin.getInformation",
                        "params": {},
                    }
                ],
            )
            row = self._by_id(rows, "dailyCheckin.getInformation")
        except Exception as e:
            return DailyCheckinSnapshot(
                claimed_today=False,
                next_available_utc=None,
                streak=0,
                streak_total=0,
                api_error=str(e),
            )
        if "error" in row:
            err = row["error"]
            msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
            if not _relogin and _jsonrpc_message_suggests_relogin(str(msg)):
                self._force_relogin(session)
                return self.get_daily_checkin_snapshot(session, _relogin=True)
            return DailyCheckinSnapshot(
                claimed_today=False,
                next_available_utc=None,
                streak=0,
                streak_total=0,
                api_error=str(msg),
            )
        res = row.get("result")
        if not isinstance(res, dict):
            return DailyCheckinSnapshot(
                claimed_today=False,
                next_available_utc=None,
                streak=0,
                streak_total=0,
                api_error="пустой result",
            )
        return _daily_checkin_from_result(res)

    def claim_daily_checkin(
        self, session: GameeSession, *, _relogin: bool = False
    ) -> tuple[bool, str, DailyCheckinSnapshot | None]:
        """dailyCheckin.claim + обновлённый getInformation. Возвращает (успех, текст наград, снимок)."""
        self.ensure_session(session)
        batch = [
            {"jsonrpc": "2.0", "id": "dailyCheckin.claim", "method": "dailyCheckin.claim", "params": {}},
            {
                "jsonrpc": "2.0",
                "id": "dailyCheckin.getInformation",
                "method": "dailyCheckin.getInformation",
                "params": {},
            },
        ]
        try:
            rows = self._post_batch(session, batch)
        except Exception as e:
            return False, str(e), None
        claim_row = self._by_id(rows, "dailyCheckin.claim")
        if "error" in claim_row:
            err = claim_row["error"]
            msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
            if not _relogin and _jsonrpc_message_suggests_relogin(str(msg)):
                self._force_relogin(session)
                return self.claim_daily_checkin(session, _relogin=True)
            return False, str(msg), None
        rewards_text = ""
        claim_res = claim_row.get("result")
        div = self._cfg.reward_micro_divisor
        if isinstance(claim_res, dict):
            rw = claim_res.get("rewards")
            if isinstance(rw, list):
                rewards_text = _format_rewards_flat_list(rw, div)
        snap: DailyCheckinSnapshot | None = None
        try:
            info_row = self._by_id(rows, "dailyCheckin.getInformation")
            if "error" not in info_row:
                r2 = info_row.get("result")
                if isinstance(r2, dict):
                    snap = _daily_checkin_from_result(r2)
        except KeyError:
            pass
        return True, rewards_text, snap

    def get_activities(
        self, session: GameeSession, *, _relogin: bool = False
    ) -> list[dict[str, Any]]:
        self.ensure_session(session)
        rows = self._post_batch(
            session,
            [
                {
                    "jsonrpc": "2.0",
                    "id": "user.getActivities",
                    "method": "user.getActivities",
                    "params": _activities_get_params(),
                }
            ],
        )
        row = self._by_id(rows, "user.getActivities")
        if "error" in row:
            err = row["error"]
            if not _relogin and _jsonrpc_error_suggests_relogin(err):
                self._force_relogin(session)
                return self.get_activities(session, _relogin=True)
            raise RuntimeError(_jsonrpc_error_message(err))
        result = row.get("result")
        if not isinstance(result, dict):
            return []
        return _activities_from_result(result)

    def claim_board_activity_rewards(
        self, session: GameeSession, *, _relogin: bool = False
    ) -> list[ActivityRewardClaim]:
        self.ensure_session(session)
        activities = self.get_activities(session)
        claimed: list[ActivityRewardClaim] = []
        attempted_ids: set[int] = set()
        div = self._cfg.reward_micro_divisor

        for _ in range(12):
            activity: dict[str, Any] | None = None
            activity_id: int | None = None
            for item in activities:
                aid = _activity_id(item)
                if aid is None or aid in attempted_ids:
                    continue
                if _activity_claimable_board_reward(item):
                    activity = item
                    activity_id = aid
                    break
            if activity is None or activity_id is None:
                break

            attempted_ids.add(activity_id)
            batch = [
                {
                    "jsonrpc": "2.0",
                    "id": "user.claimActivity",
                    "method": "user.claimActivity",
                    "params": {"activityId": activity_id},
                },
                {
                    "jsonrpc": "2.0",
                    "id": "user.getActivities",
                    "method": "user.getActivities",
                    "params": _activities_get_params(),
                },
            ]
            rows = self._post_batch(session, batch)
            claim_row = self._by_id(rows, "user.claimActivity")
            if "error" in claim_row:
                err = claim_row["error"]
                if not _relogin and _jsonrpc_error_suggests_relogin(err):
                    self._force_relogin(session)
                    return self.claim_board_activity_rewards(session, _relogin=True)
                msg = _jsonrpc_error_message(err)
                low = msg.lower()
                if "already" not in low or "claim" not in low:
                    if claimed:
                        return claimed
                    raise RuntimeError(msg)
            else:
                claim_res = claim_row.get("result")
                claim_dict = claim_res if isinstance(claim_res, dict) else None
                claimed.append(
                    ActivityRewardClaim(
                        activity_id=activity_id,
                        name=_activity_name(activity),
                        rewards_text=_format_activity_claim_rewards(
                            claim_dict,
                            activity,
                            div,
                        ),
                    )
                )
                self._assets_cache = None

            refreshed = False
            try:
                activities_row = self._by_id(rows, "user.getActivities")
                if "error" not in activities_row:
                    result = activities_row.get("result")
                    if isinstance(result, dict):
                        activities = _activities_from_result(result)
                        refreshed = True
            except Exception:
                refreshed = False
            if not refreshed:
                activities = [
                    item
                    for item in activities
                    if _activity_id(item) != activity_id
                ]

        return claimed

    def get_season_pass_progress(
        self, session: GameeSession, *, _relogin: bool = False
    ) -> SeasonPassProgress | None:
        """rewardedProgress.getAll — только прогресс Season Pass (TGXP), без клейма."""
        self.ensure_session(session)
        try:
            rows = self._post_batch(
                session,
                [
                    {
                        "jsonrpc": "2.0",
                        "id": "rewardedProgress.getAll",
                        "method": "rewardedProgress.getAll",
                        "params": {"pagination": {"offset": 0, "limit": 3}},
                    }
                ],
            )
            row = self._by_id(rows, "rewardedProgress.getAll")
        except Exception:
            return None
        if "error" in row:
            err = row["error"]
            msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
            if not _relogin and _jsonrpc_message_suggests_relogin(str(msg)):
                self._force_relogin(session)
                return self.get_season_pass_progress(session, _relogin=True)
            return None
        res = row.get("result")
        if not isinstance(res, dict):
            return None
        prog = _season_program_from_get_all_result(res)
        if prog is None:
            return None
        return _season_pass_progress_from_program(prog)

    def claim_season_pass_free_all(
        self, session: GameeSession, *, _auth_retry: bool = False
    ) -> tuple[str, SeasonPassProgress | None]:
        """Клеймит все бесплатные вехи (premium=false), как в веб-клиенте; батч claim+getAll."""
        return self._claim_season_pass_track(
            session, premium=False, _auth_retry=_auth_retry
        )

    def claim_season_pass_premium_all(
        self, session: GameeSession, *, _auth_retry: bool = False
    ) -> tuple[str, SeasonPassProgress | None]:
        """Клеймит премиум-вехи (premium=true), как в HAR prizes.gamee.com."""
        return self._claim_season_pass_track(
            session, premium=True, _auth_retry=_auth_retry
        )

    def _claim_season_pass_track(
        self,
        session: GameeSession,
        *,
        premium: bool,
        _auth_retry: bool = False,
    ) -> tuple[str, SeasonPassProgress | None]:
        """Клеймит одну ветку Season Pass (бесплатную или премиум); батч claim+getAll."""
        self.ensure_session(session)
        div = self._cfg.reward_micro_divisor
        summaries: list[str] = []
        last_progress: SeasonPassProgress | None = None
        # Первая точка — один getAll; дальше прогресс из ответа батча, лишние GET не дёргаем.
        progress = self.get_season_pass_progress(session)
        # После клейма API может ещё отдавать тот же milestoneId — без выхода цикл крутится до лимита.
        previously_claimed: int | None = None
        for _ in range(24):
            if progress is None:
                break
            last_progress = progress
            ids = (
                progress.claimable_premium_milestone_ids
                if premium
                else progress.claimable_free_milestone_ids
            )
            if not ids:
                break
            mid = ids[0]
            if previously_claimed is not None and mid == previously_claimed:
                break
            batch = [
                {
                    "jsonrpc": "2.0",
                    "id": "rewardedProgress.claim",
                    "method": "rewardedProgress.claim",
                    "params": {"milestoneId": mid, "premium": premium},
                },
                {
                    "jsonrpc": "2.0",
                    "id": "rewardedProgress.getAll",
                    "method": "rewardedProgress.getAll",
                    "params": {"pagination": {"offset": 0, "limit": 3}},
                },
                {"jsonrpc": "2.0", "id": "user.getAssets", "method": "user.getAssets", "params": {}},
            ]
            try:
                rows = self._post_batch(session, batch)
            except Exception as e:
                if summaries:
                    return "; ".join(summaries), last_progress
                return str(e), last_progress
            claim_row = self._by_id(rows, "rewardedProgress.claim")
            if "error" in claim_row:
                err = claim_row["error"]
                msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
                if (
                    not _auth_retry
                    and _jsonrpc_message_suggests_relogin(str(msg))
                ):
                    self._force_relogin(session)
                    return self._claim_season_pass_track(
                        session, premium=premium, _auth_retry=True
                    )
                if summaries:
                    return "; ".join(summaries), last_progress
                return str(msg), last_progress
            previously_claimed = mid
            claim_res = claim_row.get("result")
            one = ""
            if isinstance(claim_res, dict):
                rw = claim_res.get("rewards")
                if isinstance(rw, list):
                    one = _format_rewards_flat_list(rw, div)
            if one and one != "—":
                summaries.append(one)
            progress = None
            ga = self._by_id(rows, "rewardedProgress.getAll")
            if "error" not in ga:
                r2 = ga.get("result")
                if isinstance(r2, dict):
                    prog = _season_program_from_get_all_result(r2)
                    if prog is not None:
                        parsed = _season_pass_progress_from_program(prog)
                        if parsed is not None:
                            last_progress = parsed
                            progress = parsed
            # Без второго getAll здесь: при 20+ аккаунтах fallback давал лавину параллельных запросов.
            if progress is None:
                break
        return "; ".join(summaries) if summaries else "", last_progress
