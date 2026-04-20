from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl


@dataclass(frozen=True, slots=True)
class TelegramMiniAppInitData:
    """Нормализованная структура `tgWebAppData` / `initData`.

    Это только структурная валидация и парсинг, без притворства официальным клиентом.
    Криптографическая проверка подписи зависит от доступного валидатора/секрета и может
    быть добавлена поверх этого слоя отдельным backend-ом.
    """

    raw: str
    fields: dict[str, str]
    auth_date: int
    hash_value: str | None
    signature: str | None
    query_id: str | None
    start_param: str | None
    user: dict[str, Any] | None


def parse_telegram_mini_app_init_data(raw: str) -> TelegramMiniAppInitData:
    text = (raw or "").strip()
    if not text:
        raise ValueError("Пустой initData / tgWebAppData")

    pairs = parse_qsl(text, keep_blank_values=True, strict_parsing=False)
    if not pairs:
        raise ValueError("initData не похож на query string Telegram Mini App")

    fields: dict[str, str] = {}
    for key, value in pairs:
        fields[str(key)] = str(value)

    auth_raw = (fields.get("auth_date") or "").strip()
    if not auth_raw:
        raise ValueError("В initData отсутствует auth_date")
    try:
        auth_date = int(auth_raw)
    except ValueError as e:
        raise ValueError("Поле auth_date должно быть целым числом") from e
    if auth_date <= 0:
        raise ValueError("Поле auth_date должно быть положительным")

    hash_value = (fields.get("hash") or "").strip() or None
    signature = (fields.get("signature") or "").strip() or None
    if hash_value is None and signature is None:
        raise ValueError("В initData отсутствуют hash/signature для последующей валидации")

    user_blob = (fields.get("user") or "").strip()
    user: dict[str, Any] | None = None
    if user_blob:
        try:
            parsed = json.loads(user_blob)
        except json.JSONDecodeError as e:
            raise ValueError("Поле user в initData не является валидным JSON") from e
        if not isinstance(parsed, dict):
            raise ValueError("Поле user в initData должно быть JSON-объектом")
        user = parsed

    return TelegramMiniAppInitData(
        raw=text,
        fields=fields,
        auth_date=auth_date,
        hash_value=hash_value,
        signature=signature,
        query_id=(fields.get("query_id") or "").strip() or None,
        start_param=(fields.get("start_param") or "").strip() or None,
        user=user,
    )


def ensure_telegram_mini_app_init_data(raw: str) -> str:
    """Проверяет структуру initData и возвращает исходную строку без изменений."""
    parsed = parse_telegram_mini_app_init_data(raw)
    return parsed.raw
