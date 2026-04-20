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
_RETRYABLE_HTTP_STATUS = frozenset({429, 502, 503, 504})
_MAX_HTTP_TRANSIENT_RETRIES = 4
# Conservative rate limiting: match official client request velocities.
# Under production credentials, request pacing must be indistinguishable
# from a real user tapping through the Mini App UI.
_HTTP_REQUEST_MAX_PARALLEL = 2
_HTTP_REQUEST_START_GAP_SEC = 0.8
_HTTP_REQUEST_START_JITTER_SEC = 1.2


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
            "Cloudflare WAF (страница «Attention Required»): блок по IP/прокси или без cookie "
            "после проверки. Попробуй резидентский/другой прокси или запуск без прокси с «чистого» IP."
        )
    if "cloudflare" in low and "<!doctype html>" in low[:200].lower():
        return (
            "Cloudflare отдал HTML вместо JSON — запрос распознан как автоматический. "
            "Смени прокси или сеть."
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


def _format_check_task_code_result(result: dict[str, Any], cfg: GameeConfig) -> str:
    """Человекочитаемо: ответ telegram.checkTask.code (rewards + completed)."""
    parts: list[str] = []
    if result.get("completed") is True:
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

    def can_claim_now(self, now: datetime | None = None) -> bool:
        if self.api_error or self.claimed_today:
            return False
        if now is None:
            now = datetime.now(timezone.utc)
        if self.next_available_utc is None:
            return True
        na = self.next_available_utc
        if na.tzinfo is None:
            na = na.replace(tzinfo=timezone.utc)
        return now >= na


def _daily_checkin_from_result(result: dict[str, Any]) -> DailyCheckinSnapshot:
    claimed = bool(result.get("claimedToday"))
    next_dt = _parse_iso_datetime_utc(result.get("nextClaimAvailableTimestamp"))
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

    def __init__(
        self,
        cfg: GameeConfig,
        *,
        proxy_url: str | None = None,
        http_profile: GameeHttpClientProfile,
        transport: GameeTransport | None = None,
    ) -> None:
        self._cfg = cfg
        self._transport = transport or build_default_gamee_transport(
            backend_name=cfg.transport_backend,
            proxy_url=proxy_url,
            http_profile=http_profile,
        )
        self._proxy_url = self._transport.proxy_url
        # Один раз за жизнь клиента: GET prizes → cookie / контекст для Cloudflare перед API.
        self._browser_warmup_done = False

    @property
    def proxy_url(self) -> str | None:
        """Текущий нормализованный URL прокси (или None = без прокси)."""
        return self._proxy_url

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

    def _call_with_http_gate(self, fn):
        self.__class__._request_semaphore.acquire()
        try:
            delay = self.__class__._reserve_request_delay()
            if delay > 0:
                time.sleep(delay)
            return fn()
        finally:
            self.__class__._request_semaphore.release()

    def _ensure_prizes_page_warmup(self, session: GameeSession) -> None:
        """GET главной prizes — те же TLS/cookie jar, что и у POST api2 (как заход из браузера)."""
        if self._browser_warmup_done:
            return
        p = session.http_profile
        headers = p.ordered_navigation_headers()
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

    def _headers(self, session: GameeSession) -> dict[str, str]:
        # Профиль согласован с TLS (session.http_profile ↔ self._http_profile при правильном использовании).
        # Strict Chrome Android header ordering via OrderedDict.
        p = session.http_profile
        auth = session.auth_token if session.auth_token and session.token_valid() else ""
        return p.ordered_api_headers(
            install_uuid=session.install_uuid,
            auth_token=auth,
        )

    def _post_batch_raw(self, session: GameeSession, body: list[dict[str, Any]] | dict[str, Any]) -> list[dict[str, Any]]:
        """Один POST без перелогина. Для loginUsingTelegram — только так, иначе рекурсия."""
        url = self._cfg.api_url.rstrip("/") + "/"
        payload = json.dumps(body, ensure_ascii=False)
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
            if code in _RETRYABLE_HTTP_STATUS and attempt + 1 < _MAX_HTTP_TRANSIENT_RETRIES:
                time.sleep(self._retry_delay_seconds(attempt))
                continue
            r.raise_for_status()
            break
        assert r is not None
        data = r.json()
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return [data]
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

    def login_telegram(self, session: GameeSession) -> None:
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
            raise RuntimeError(
                f"loginUsingTelegram: пустой или неожиданный ответ от API: {str(rows)[:300]}"
            )

        # Handle batch-level server errors (id=None) — e.g. invalid initData,
        # server overload, or rejected batch. These come as:
        # {"jsonrpc": "2.0", "error": {...}, "id": null}
        for row in rows:
            if row.get("id") is None and "error" in row:
                err_obj = row["error"]
                code = err_obj.get("code", "?")
                msg = err_obj.get("message", "unknown")
                detail = err_obj.get("data", {})
                reason = detail.get("reason", "") if isinstance(detail, dict) else str(detail)
                self._invalidate_init_cache(session)
                raise RuntimeError(
                    f"loginUsingTelegram: сервер отклонил запрос "
                    f"(code={code}, message={msg}, reason={reason}). "
                    f"initData будет обновлён при следующей попытке."
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
        self, session: GameeSession, *, task_id: int, code: str
    ) -> tuple[bool, str]:
        """telegram.checkTask.code — промокод с prizes.gamee.com (см. HAR)."""
        raw = (code or "").strip()
        if not raw:
            return False, "пустой код"
        self.ensure_session(session)
        body = [
            {
                "jsonrpc": "2.0",
                "id": "telegram.checkTask.code",
                "method": "telegram.checkTask.code",
                "params": {"taskId": int(task_id), "code": raw},
            }
        ]
        rows = self._post_batch(session, body)
        row = self._by_id(rows, "telegram.checkTask.code")
        if "error" in row:
            err = row["error"]
            msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
            return False, str(msg)
        result = row.get("result")
        if not isinstance(result, dict):
            return True, "OK"
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
        before = self.get_assets_state(session)
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
