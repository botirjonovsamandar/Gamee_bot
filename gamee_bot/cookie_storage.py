"""Persistent cookie storage per-account для curl_cffi сессий.

Сохраняет cookies в JSON формате, по одному файлу на label.
Реальный браузер хранит cookies между запусками — без этого Cloudflare
state (cf_clearance, __cfruid) теряется при каждом перезапуске.
"""
from __future__ import annotations

import json
import logging
from http.cookiejar import Cookie, CookieJar
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


def _cookies_dir(base_dir: Path) -> Path:
    d = base_dir / "sessions" / "cookies"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _safe_label(label: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in label)[:64] or "_default"


def _cookie_to_dict(c: Cookie) -> dict[str, Any]:
    return {
        "version": c.version,
        "name": c.name,
        "value": c.value,
        "port": c.port,
        "domain": c.domain,
        "path": c.path,
        "secure": c.secure,
        "expires": c.expires,
        "discard": c.discard,
        "comment": c.comment,
        "comment_url": c.comment_url,
        "rfc2109": c.rfc2109,
        "domain_specified": c.domain_specified,
        "domain_initial_dot": c.domain_initial_dot,
        "path_specified": c.path_specified,
        "rest": dict(c._rest) if hasattr(c, "_rest") else {},
    }


def _dict_to_cookie(d: dict[str, Any]) -> Cookie:
    return Cookie(
        version=d.get("version", 0) or 0,
        name=d["name"],
        value=d.get("value", ""),
        port=d.get("port"),
        port_specified=bool(d.get("port")),
        domain=d.get("domain", ""),
        domain_specified=d.get("domain_specified", True),
        domain_initial_dot=d.get("domain_initial_dot", False),
        path=d.get("path", "/"),
        path_specified=d.get("path_specified", True),
        secure=d.get("secure", False),
        expires=d.get("expires"),
        discard=d.get("discard", False),
        comment=d.get("comment"),
        comment_url=d.get("comment_url"),
        rest=d.get("rest", {}) or {},
        rfc2109=d.get("rfc2109", False),
    )


def save_cookies(label: str, jar: CookieJar, base_dir: Path) -> None:
    """Сохранить cookies в sessions/cookies/{label}.json. Не падает на ошибках."""
    try:
        data = [_cookie_to_dict(c) for c in jar]
        path = _cookies_dir(base_dir) / f"{_safe_label(label)}.json"
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except Exception:
        log.debug("save_cookies failed for %s", label, exc_info=True)


def load_cookies(label: str, base_dir: Path) -> list[Cookie]:
    """Загрузить ранее сохранённые cookies. Возвращает [] если файла нет."""
    try:
        path = _cookies_dir(base_dir) / f"{_safe_label(label)}.json"
        if not path.exists():
            return []
        data = json.loads(path.read_text(encoding="utf-8"))
        return [_dict_to_cookie(d) for d in data if d.get("name")]
    except Exception:
        log.debug("load_cookies failed for %s", label, exc_info=True)
        return []
