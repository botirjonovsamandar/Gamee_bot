"""JSON-RPC id field randomization для разнообразия паттерна payload'ов.

Текущая проблема: id всегда равен method name → bot framework signature.
Реальные клиенты используют разные форматы:
- Counter (1, 2, 3, ...)
- UUID4
- timestamp+hash
- method+suffix

Per-account стиль выбирается стабильно (по hash label),
а сам id varies per request в рамках выбранного стиля.

Корреляция request/response в client.py делается по позиции в массиве
(не по id), так что id может быть любым уникальным значением.
"""
from __future__ import annotations

import hashlib
import threading
import time
import uuid


_ID_STYLES = ("counter", "uuid", "method_hash", "ts_random")


class JsonRpcIdGenerator:
    """Per-session генератор id для batch-запросов.

    Стиль выбирается per-account детерминированно, чтобы один аккаунт
    не "менял рисунок" между сессиями (это было бы подозрительно).
    """

    __slots__ = ("_style", "_counter", "_lock", "_seed")

    def __init__(self, label: str = ""):
        digest = hashlib.sha256((label or "_default").encode("utf-8")).digest()
        self._style = _ID_STYLES[digest[0] % len(_ID_STYLES)]
        self._counter = 1 + (digest[1] % 100)  # старт со случайного номера
        self._seed = digest[:8].hex()
        self._lock = threading.Lock()

    @property
    def style(self) -> str:
        return self._style

    def next(self, method: str = "") -> str | int:
        with self._lock:
            n = self._counter
            self._counter += 1
        if self._style == "counter":
            return n
        if self._style == "uuid":
            return str(uuid.uuid4())
        if self._style == "method_hash":
            short = method.split(".")[-1] if "." in method else method
            return f"{short}_{self._seed[:6]}_{n}"
        # ts_random
        ts_ms = int(time.time() * 1000)
        rand = uuid.uuid4().hex[:6]
        return f"{ts_ms}-{rand}"
