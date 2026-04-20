from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import yaml

from gamee_bot.proxy_url import normalize_gamee_proxy_url

TELETHON_CREDENTIALS_REQUIRED_MSG = (
    "В настройках не указаны api_id и api_hash Telegram. Откройте меню «Настройки», "
    "вкладка «Telegram», вставьте оба значения с сайта https://my.telegram.org → "
    "API development tools и нажмите «Сохранить»."
)

BACKGROUND_MODE_MANUAL_ONLY = "manual_only"
BACKGROUND_MODE_READ_ONLY = "read_only"
BACKGROUND_MODE_FULL_AUTO = "full_auto"
_BACKGROUND_MODES = {
    BACKGROUND_MODE_MANUAL_ONLY,
    BACKGROUND_MODE_READ_ONLY,
    BACKGROUND_MODE_FULL_AUTO,
}

GAMEE_TRANSPORT_BACKEND_DEFAULT = "curl_cffi_raw_http"

# Official Telegram Android client credentials (public, from open-source builds).
# Used as defaults when the user has not configured custom api_id/api_hash.
TELEGRAM_ANDROID_API_ID = 4
TELEGRAM_ANDROID_API_HASH = "014b35b6184100b085b0d0572f9b5103"


def ensure_config_file(path: Path) -> None:
    """Создаёт config.yaml с шаблоном, если файла ещё нет (первый запуск / клон без конфига)."""
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    default: dict[str, Any] = {
        "gamee": {"transport_backend": GAMEE_TRANSPORT_BACKEND_DEFAULT},
        "telethon": {"api_id": TELEGRAM_ANDROID_API_ID, "api_hash": TELEGRAM_ANDROID_API_HASH},
        "compliance": {
            "background_mode": BACKGROUND_MODE_MANUAL_ONLY,
            "session_duration_minutes": 45,
            "quiet_hours_enabled": False,
            "quiet_hours_start_hour": 0,
            "quiet_hours_end_hour": 8,
            "daily_move_budget": 30,
            "max_moves_per_session": 8,
            "error_cooldown_seconds": 30,
            "stop_after_error_streak": 3,
            "require_confirm_mass_code": True,
            "require_confirm_play_session": True,
        },
        "paths": {"accounts": "accounts.yaml"},
        "ui": {"window_title": "Gamee — кубик доски"},
    }
    path.write_text(
        yaml.safe_dump(default, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


def ensure_accounts_file(path: Path) -> None:
    """Создаёт accounts.yaml с пустым списком, если файла ещё нет."""
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("accounts: []\n", encoding="utf-8")


def parse_gamee_ref_input(raw: str | None) -> str | None:
    """
    Реф для Gamee: целая ссылка (t.me/.../start?startapp=…), или только хвост после startapp=.
    Возвращает значение start_param для Mini App либо None.
    """
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    low = s.lower()
    key = "startapp="
    if key in low:
        idx = low.index(key)
        val = s[idx + len(key) :].strip()
        val = val.split("&")[0].split("#")[0].strip()
        return val or None
    if "t.me/" in low or low.startswith("http://") or low.startswith("https://"):
        url = s if low.startswith("http") else "https://" + s.lstrip("/")
        qs = parse_qs(urlparse(url).query)
        vals = qs.get("startapp")
        if vals:
            v = unquote(vals[0]).strip()
            return v or None
        return None
    return s


def _require_api_id() -> int:
    raise ValueError(
        "api_id не указан в config.yaml → telethon → api_id. "
        "Получите свой api_id на https://my.telegram.org → API development tools."
    )


def _require_api_hash() -> str:
    raise ValueError(
        "api_hash не указан в config.yaml → telethon → api_hash. "
        "Получите свой api_hash на https://my.telegram.org → API development tools."
    )


def normalize_background_mode(raw: Any) -> str:
    s = str(raw or "").strip().lower()
    if s == "rewards_only":
        return BACKGROUND_MODE_READ_ONLY
    if s in _BACKGROUND_MODES:
        return s
    return BACKGROUND_MODE_MANUAL_ONLY


def background_mode_label(mode: str) -> str:
    m = normalize_background_mode(mode)
    if m == BACKGROUND_MODE_FULL_AUTO:
        return "Полный автомат"
    if m == BACKGROUND_MODE_READ_ONLY:
        return "Только чтение"
    return "Только ручной режим"


def local_time_in_quiet_hours(
    enabled: bool,
    start_hour: int,
    end_hour: int,
    *,
    now: datetime | None = None,
) -> bool:
    if not enabled:
        return False
    dt = now or datetime.now()
    hour = int(dt.hour)
    start = max(0, min(23, int(start_hour)))
    end = max(0, min(23, int(end_hour)))
    if start == end:
        return True
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


def gamee_proxy_table_summary(raw: Any) -> tuple[str, str]:
    """Тип прокси для ячейки таблицы; в подсказке — узел (хост:порт), без пароля в ячейке."""
    norm = normalize_gamee_proxy_url(raw)
    if not norm:
        return "без прокси", "API Gamee без прокси (к серверу игры идёт ваш IP)."
    u = urlparse(norm)
    scheme = (u.scheme or "http").lower()
    host = u.hostname or "?"
    try:
        port = u.port
    except ValueError:
        return (
            "прокси?",
            "Некорректная строка (часто это http://host:port:user:pass). "
            "Сохраните как host:port:user:pass или user:pass@host:port / socks5://…",
        )
    ps = f":{port}" if port else ""
    if scheme in ("http", "https"):
        kind = "HTTP"
    elif scheme in ("socks5", "socks5h"):
        kind = "SOCKS5"
    else:
        kind = scheme.upper()
    netloc = host + ps
    cell = kind
    if u.username:
        tip = f"{kind}, пользователь «{u.username}», узел {netloc}"
    else:
        tip = f"{kind}, узел {netloc}"
    return cell, tip


@dataclass
class GameeConfig:
    api_url: str
    transport_backend: str
    life_currency_id: int
    gold_currency_id: int
    ticket_currency_id: int
    money_currency_id: int  # MONEY в getAssets, баланс в долларах
    life_micro_divisor: int
    gold_micro_divisor: int
    ticket_micro_divisor: int
    reward_micro_divisor: int
    money_micro_divisor: int  # микро MONEY на 1 цент USD (обычно 10_000)
    # Оценка конвертации Gold Points → USDT в UI prizes.gamee.com: micro / этот делитель.
    gold_estimate_usd_micro_divisor: int
    # telegram.checkTask.code (промокод на prizes.gamee.com); см. HAR.
    check_task_id: int


@dataclass
class TelegramConfig:
    bot_token: str
    chat_id: str
    notify_on_move: bool
    notify_on_daily_claim: bool
    notify_on_season_claim: bool
    summary_interval_seconds: int


@dataclass
class TelethonConfig:
    """Credentials for Telethon: official Android defaults or custom from my.telegram.org."""

    api_id: int
    api_hash: str
    gamee_start_param: str | None
    # Числовой ref для user.linkTelegramReferral после loginUsingTelegram (как в HAR).
    telegram_referral_ref: int | None


@dataclass
class ComplianceConfig:
    background_mode: str
    session_duration_minutes: int
    quiet_hours_enabled: bool
    quiet_hours_start_hour: int
    quiet_hours_end_hour: int
    daily_move_budget: int
    max_moves_per_session: int
    error_cooldown_seconds: int
    stop_after_error_streak: int
    require_confirm_mass_code: bool
    require_confirm_play_session: bool


@dataclass
class AppConfig:
    gamee: GameeConfig
    telegram: TelegramConfig
    telethon: TelethonConfig
    compliance: ComplianceConfig
    accounts_path: Path
    window_title: str


def telethon_credentials_ready(cfg: AppConfig) -> bool:
    """Пара api_id + api_hash с my.telegram.org — нужна для Telethon и запуска бота."""
    return cfg.telethon.api_id > 0 and bool(cfg.telethon.api_hash.strip())


def resolve_account_gamee_start_param(
    cfg: AppConfig,
    account_gamee_ref_raw: str | None,
    *,
    inherit_global_if_empty: bool = False,
) -> str | None:
    """
    Реф (start_param) для аккаунта из accounts.yaml.
    По умолчанию пустое поле в YAML НЕ подменяется глобальным конфигом — смена настроек
    не трогает старые записи. inherit_global_if_empty=True — только для шага «Добавить аккаунт».
    """
    r = (account_gamee_ref_raw or "").strip()
    if r:
        return parse_gamee_ref_input(r)
    if inherit_global_if_empty:
        return cfg.telethon.gamee_start_param
    return None


def resolve_account_telegram_referral_ref(
    cfg: AppConfig,
    account_telegram_referral_ref: int | None,
    *,
    inherit_global_if_empty: bool = False,
) -> int | None:
    """Числовой ref для linkTelegramReferral: только из YAML, если не запрошен fallback как при добавлении аккаунта."""
    if account_telegram_referral_ref is not None:
        return int(account_telegram_referral_ref)
    if inherit_global_if_empty:
        return cfg.telethon.telegram_referral_ref
    return None


def _optional_int_yaml(raw: Any) -> int | None:
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    try:
        v = int(s)
    except ValueError:
        return None
    return v if v > 0 else None


def _int_yaml_default(
    raw: Any,
    default: int,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    try:
        val = int(raw)
    except (TypeError, ValueError):
        val = int(default)
    if minimum is not None and val < minimum:
        val = minimum
    if maximum is not None and val > maximum:
        val = maximum
    return val


def load_config(path: Path) -> AppConfig:
    ensure_config_file(path)
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    base = path.parent.resolve()
    g = raw.get("gamee") or {}
    t = raw.get("telegram") or {}
    th = raw.get("telethon") or {}
    c = raw.get("compliance") or {}
    p = raw.get("paths") or {}
    u = raw.get("ui") or {}
    acc = p.get("accounts", "accounts.yaml")
    accounts_path = Path(acc)
    if not accounts_path.is_absolute():
        accounts_path = (base / accounts_path).resolve()
    ensure_accounts_file(accounts_path)
    try:
        telethon_api_id = int(th.get("api_id", 0) or 0)
    except (TypeError, ValueError):
        telethon_api_id = 0
    telethon_api_hash = str(th.get("api_hash", "") or "").strip()
    return AppConfig(
        gamee=GameeConfig(
            api_url=str(g.get("api_url", "https://api2.gamee.com/")),
            transport_backend=str(
                g.get("transport_backend", GAMEE_TRANSPORT_BACKEND_DEFAULT)
            ).strip()
            or GAMEE_TRANSPORT_BACKEND_DEFAULT,
            life_currency_id=int(g.get("life_currency_id", 5)),
            gold_currency_id=int(g.get("gold_currency_id", 65)),
            ticket_currency_id=int(g.get("ticket_currency_id", 1)),
            money_currency_id=int(g.get("money_currency_id", 4)),
            life_micro_divisor=int(g.get("life_micro_divisor", 1_000_000)),
            gold_micro_divisor=int(g.get("gold_micro_divisor", 1_000_000)),
            ticket_micro_divisor=int(g.get("ticket_micro_divisor", 1_000_000)),
            reward_micro_divisor=int(g.get("reward_micro_divisor", 1_000_000)),
            money_micro_divisor=int(g.get("money_micro_divisor", 10_000)),
            gold_estimate_usd_micro_divisor=int(
                g.get("gold_estimate_usd_micro_divisor", 1_000_000_000_000)
            ),
            check_task_id=int(g.get("check_task_id", 2950)),
        ),
        telegram=TelegramConfig(
            bot_token=str(t.get("bot_token", "")),
            chat_id=str(t.get("chat_id", "")),
            notify_on_move=bool(t.get("notify_on_move", True)),
            notify_on_daily_claim=bool(t.get("notify_on_daily_claim", True)),
            notify_on_season_claim=bool(t.get("notify_on_season_claim", True)),
            summary_interval_seconds=int(t.get("summary_interval_seconds", 3600)),
        ),
        telethon=TelethonConfig(
            api_id=telethon_api_id if telethon_api_id > 0 else _require_api_id(),
            api_hash=telethon_api_hash if telethon_api_hash else _require_api_hash(),
            gamee_start_param=parse_gamee_ref_input(
                th.get("gamee_ref") if "gamee_ref" in th else th.get("mini_app_start_param")
            ),
            telegram_referral_ref=_optional_int_yaml(th.get("telegram_referral_ref")),
        ),
        compliance=ComplianceConfig(
            background_mode=normalize_background_mode(c.get("background_mode")),
            session_duration_minutes=_int_yaml_default(
                c.get("session_duration_minutes", 45), 45, minimum=0
            ),
            quiet_hours_enabled=bool(c.get("quiet_hours_enabled", False)),
            quiet_hours_start_hour=_int_yaml_default(
                c.get("quiet_hours_start_hour", 0), 0, minimum=0, maximum=23
            ),
            quiet_hours_end_hour=_int_yaml_default(
                c.get("quiet_hours_end_hour", 8), 8, minimum=0, maximum=23
            ),
            daily_move_budget=_int_yaml_default(c.get("daily_move_budget", 30), 30, minimum=1),
            max_moves_per_session=_int_yaml_default(
                c.get("max_moves_per_session", 8), 8, minimum=1
            ),
            error_cooldown_seconds=_int_yaml_default(
                c.get("error_cooldown_seconds", 30), 30, minimum=5
            ),
            stop_after_error_streak=_int_yaml_default(
                c.get("stop_after_error_streak", 3), 3, minimum=1
            ),
            require_confirm_mass_code=bool(c.get("require_confirm_mass_code", True)),
            require_confirm_play_session=bool(c.get("require_confirm_play_session", True)),
        ),
        accounts_path=accounts_path,
        window_title=str(u.get("window_title", "Gamee — кубик доски")),
    )


def read_yaml_mapping(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


def read_full_config_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


def save_config_sections(
    path: Path,
    *,
    gamee: dict[str, Any] | None = None,
    telegram: dict[str, Any] | None = None,
    telethon: dict[str, Any] | None = None,
    compliance: dict[str, Any] | None = None,
    paths: dict[str, Any] | None = None,
    ui: dict[str, Any] | None = None,
) -> None:
    """Обновляет только перечисленные секции; остальные ключи файла сохраняются."""
    existing = read_full_config_yaml(path)
    if gamee is not None:
        merged = {**(existing.get("gamee") or {}), **gamee}
        for k in (
            "poll_interval_seconds",
            "min_energy_to_play",
            "energy_regen_minutes",
            "play_delay_seconds",
        ):
            merged.pop(k, None)
        existing["gamee"] = merged
    if telegram is not None:
        merged = {**(existing.get("telegram") or {}), **telegram}
        existing["telegram"] = merged
    if telethon is not None:
        merged = {**(existing.get("telethon") or {}), **telethon}
        for k in ("mini_app_bot", "mini_app_short_name", "mini_app_start_param"):
            merged.pop(k, None)
        existing["telethon"] = merged
    if compliance is not None:
        merged = {**(existing.get("compliance") or {}), **compliance}
        merged["background_mode"] = normalize_background_mode(merged.get("background_mode"))
        existing["compliance"] = merged
    if paths is not None:
        merged = {**(existing.get("paths") or {}), **paths}
        existing["paths"] = merged
    if ui is not None:
        merged = {**(existing.get("ui") or {}), **ui}
        existing["ui"] = merged
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(existing, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
