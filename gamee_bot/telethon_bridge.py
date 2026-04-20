from __future__ import annotations

import asyncio
import re
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError
from telethon.tl.functions.messages import RequestAppWebViewRequest
from telethon.tl.types import InputBotAppShortName, InputPeerUser, InputUser

from gamee_bot.config import AppConfig, TelethonConfig, resolve_account_gamee_start_param
from gamee_bot.tma_auth import ensure_telegram_mini_app_init_data

_init_cache: dict[tuple[str, str | None], tuple[str, float]] = {}
_CACHE_TTL_SEC = 45 * 60

# SQLite-сессия одного файла нельзя открывать параллельно из разных asyncio.run / потоков.
_session_io_locks: dict[str, threading.Lock] = {}
_session_io_locks_guard = threading.Lock()

# Пул реальных Android-устройств для эмуляции официального Telegram-клиента.
# Выбирается детерминированно по session path (один аккаунт — одно устройство).
_ANDROID_DEVICE_POOL: tuple[tuple[str, str], ...] = (
    ("Samsung SM-S908B", "SDK 34"),      # Galaxy S22 Ultra
    ("Samsung SM-S918B", "SDK 34"),      # Galaxy S23 Ultra
    ("Samsung SM-S928B", "SDK 34"),      # Galaxy S24 Ultra
    ("Xiaomi 2201123G", "SDK 34"),       # Xiaomi 12 Pro
    ("Xiaomi 23113RKC6G", "SDK 34"),     # Xiaomi 14 Pro
    ("Google Pixel 8 Pro", "SDK 34"),    # Pixel 8 Pro
    ("Google Pixel 7", "SDK 34"),        # Pixel 7
    ("OnePlus IN2025", "SDK 34"),        # OnePlus 9 Pro
    ("OnePlus CPH2449", "SDK 34"),       # OnePlus 11
)
_TELETHON_APP_VERSION = "10.14.5"
_TELETHON_SYSTEM_VERSION = "Android 14"

# Официальная мини-аппа Gamee в Telegram (как в t.me/gamee/start?startapp=…)
_GAMEE_MINI_BOT_USERNAME = "gamee"
_GAMEE_MINI_APP_SHORT_NAME = "start"


def normalize_phone_for_telegram(phone: str) -> str:
    """Пробелы/табы не мешают; можно без «+», если указан полный код страны (например 380…)."""
    s = phone.strip()
    parts: list[str] = []
    for c in s:
        if c.isspace():
            continue
        if c in "-.()":
            continue
        parts.append(c)
    return "".join(parts)


def _session_io_lock(session_path: str) -> threading.Lock:
    key = str(Path(session_path).resolve())
    with _session_io_locks_guard:
        if key not in _session_io_locks:
            _session_io_locks[key] = threading.Lock()
        return _session_io_locks[key]


def run_telethon_locked(session_path: str, coro):
    """Один asyncio.run на файл session без параллельного доступа."""
    lock = _session_io_lock(session_path)
    with lock:
        return asyncio.run(coro)


def clear_init_cache(label: str | None = None) -> None:
    global _init_cache
    if label is None:
        _init_cache.clear()
    else:
        rm = [k for k in _init_cache if k[0] == label]
        for k in rm:
            del _init_cache[k]


def telethon_session_absolute_path(telethon_session: str, accounts_yaml: Path) -> Path:
    """Путь к .session относительно каталога accounts.yaml."""
    base = accounts_yaml.parent.resolve()
    p = Path(telethon_session.strip())
    if not p.is_absolute():
        p = (base / p).resolve()
    return p


def parse_init_data_from_webview_url(url: str) -> str:
    if not url:
        return ""
    u = urlparse(url)
    for key, vals in parse_qs(u.query, keep_blank_values=True).items():
        if key == "tgWebAppData" and vals:
            return unquote(vals[0])
    frag = u.fragment or ""
    if "tgWebAppData=" in frag:
        raw = frag.split("tgWebAppData=", 1)[1]
        raw = raw.split("&")[0]
        return unquote(raw)
    m = re.search(r"[#&]tgWebAppData=([^&]+)", url)
    if m:
        return unquote(m.group(1))
    return ""


def _peer_to_input_user(peer: Any) -> InputUser:
    if isinstance(peer, InputPeerUser):
        return InputUser(user_id=peer.user_id, access_hash=peer.access_hash)
    raise TypeError(f"Ожидался бот (InputPeerUser), получено: {type(peer)}")


def _abs_session(session_file: str) -> str:
    return str(Path(session_file).expanduser().resolve())


def _client_for_session(path: str, api_id: int, api_hash: str) -> TelegramClient:
    """
    Эмулируем профиль Telegram Android: устройство выбирается из пула
    детерминированно по пути сессии (один аккаунт — одно устройство).
    Без receive_updates — только RPC для входа / WebView.
    """
    import hashlib as _hl
    idx = int.from_bytes(_hl.md5(path.encode()).digest()[:4], "big") % len(_ANDROID_DEVICE_POOL)
    device_model, _sdk_tag = _ANDROID_DEVICE_POOL[idx]

    return TelegramClient(
        path,
        api_id,
        api_hash,
        device_model=device_model,
        system_version=_TELETHON_SYSTEM_VERSION,
        app_version=_TELETHON_APP_VERSION,
        lang_code="en",
        system_lang_code="en-US",
        receive_updates=False,
        connection_retries=3,
        request_retries=3,
    )


async def _disconnect_safe(client: TelegramClient | None) -> None:
    if not client:
        return
    try:
        if client.is_connected():
            await client.disconnect()
    except (ConnectionError, OSError, RuntimeError):
        pass


async def _sign_in_user(
    client: TelegramClient,
    phone: str,
    code: str,
    phone_code_hash: str,
    password: str | None,
) -> None:
    try:
        await client.sign_in(phone, code, phone_code_hash=phone_code_hash)
    except SessionPasswordNeededError:
        if not password:
            raise RuntimeError("Включена 2FA — введите пароль облака Telegram") from None
        await client.sign_in(password=password)
    if not await client.is_user_authorized():
        raise RuntimeError("Авторизация не завершена")
    await client.get_me()
    if client.session is not None:
        client.session.save()


async def telethon_send_code(
    session_file: str,
    api_id: int,
    api_hash: str,
    phone: str,
) -> str:
    """Отправляет код; возвращает phone_code_hash."""
    phone = normalize_phone_for_telegram(phone)
    path = _abs_session(session_file)
    client = _client_for_session(path, api_id, api_hash)
    await client.connect()
    try:
        sent = await client.send_code_request(phone)
        return sent.phone_code_hash
    finally:
        await _disconnect_safe(client)


async def _gamee_webview_init_data(client: TelegramClient, gamee_start_param: str | None) -> str:
    if not await client.is_user_authorized():
        raise RuntimeError(
            "Сессия Telethon не авторизована (в .session нет действующего входа). "
            "Типично: гонка по одному файлу сессии или прерванный вход. "
            "Мы не вызываем log_out и не завершаем чужие сессии Telegram. "
            "Свой api_id/api_hash с my.telegram.org для этой программы снижает риски при смешении с TD. "
            "Повторите «Добавить аккаунт» или восстановите sessions/*.session.bak_*"
        )
    peer = await client.get_input_entity(_GAMEE_MINI_BOT_USERNAME)
    if not isinstance(peer, InputPeerUser):
        raise RuntimeError(f"{_GAMEE_MINI_BOT_USERNAME!r} — не user-бот")
    app = InputBotAppShortName(
        bot_id=_peer_to_input_user(peer),
        short_name=_GAMEE_MINI_APP_SHORT_NAME,
    )
    result = await client(
        RequestAppWebViewRequest(
            peer=peer,
            app=app,
            platform="android",
            write_allowed=True,
            start_param=gamee_start_param or None,
        )
    )
    url = getattr(result, "url", "") or ""
    init = parse_init_data_from_webview_url(url)
    if not init:
        raise RuntimeError(
            "Не удалось извлечь tgWebAppData из URL. "
            "Проверьте реф в настройках: полную ссылку или хвост после startapp=. "
            f"Фрагмент URL: {url[:200]}…"
        )
    return ensure_telegram_mini_app_init_data(init)


async def telethon_fetch_init_data(
    session_file: str,
    tc: TelethonConfig,
    *,
    gamee_start_param: str | None,
) -> str:
    path = _abs_session(session_file)
    client = _client_for_session(path, tc.api_id, tc.api_hash)
    await client.connect()
    try:
        return await _gamee_webview_init_data(client, gamee_start_param)
    finally:
        await _disconnect_safe(client)


async def telethon_sign_in_and_fetch_init_data(
    session_file: str,
    tc: TelethonConfig,
    phone: str,
    code: str,
    phone_code_hash: str,
    password: str | None = None,
    *,
    gamee_start_param: str | None,
) -> str:
    """Один клиент: вход + WebView Gamee + одно сохранение сессии."""
    phone = normalize_phone_for_telegram(phone)
    path = _abs_session(session_file)
    client = _client_for_session(path, tc.api_id, tc.api_hash)
    await client.connect()
    try:
        await _sign_in_user(client, phone, code, phone_code_hash, password)
        return await _gamee_webview_init_data(client, gamee_start_param)
    finally:
        await _disconnect_safe(client)


def resolve_init_data(
    record_label: str,
    init_data: str,
    telethon_session: str | None,
    cfg: AppConfig,
    *,
    account_gamee_ref: str | None = None,
) -> str:
    if not telethon_session or not telethon_session.strip():
        if not init_data:
            raise ValueError(f"Аккаунт «{record_label}»: нет init_data и telethon_session")
        return ensure_telegram_mini_app_init_data(init_data)
    tc = cfg.telethon
    spath = telethon_session_absolute_path(telethon_session, cfg.accounts_path)
    if not spath.is_file():
        raise FileNotFoundError(f"Нет файла сессии Telethon: {spath}")

    sp = resolve_account_gamee_start_param(
        cfg, account_gamee_ref, inherit_global_if_empty=False
    )
    cache_key = (record_label, sp)
    abs_s = str(spath)
    now = time.monotonic()
    if cache_key in _init_cache:
        data, t0 = _init_cache[cache_key]
        if now - t0 < _CACHE_TTL_SEC:
            return data

    async def _run_fetch() -> str:
        return await telethon_fetch_init_data(abs_s, tc, gamee_start_param=sp)

    init = ensure_telegram_mini_app_init_data(run_telethon_locked(abs_s, _run_fetch()))
    _init_cache[cache_key] = (init, now)
    return init
