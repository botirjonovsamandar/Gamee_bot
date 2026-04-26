from __future__ import annotations

import os
import re
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from gamee_bot.config import normalize_gamee_proxy_url, read_yaml_mapping


def safe_account_filename(label: str) -> str:
    s = re.sub(r"[^\w\-]+", "_", label.strip(), flags=re.UNICODE)
    s = s.strip(" _") or "account"
    return s[:64]


@dataclass
class AccountRecord:
    label: str
    init_data: str
    install_uuid: str
    telethon_session: str | None = None
    # Сырой ввод как в настройках (ссылка или хвост startapp=). Пусто → общий реф из config.
    gamee_ref: str | None = None
    # user.linkTelegramReferral → params.ref; пусто → telethon.telegram_referral_ref из config.
    telegram_referral_ref: int | None = None
    # True = уже был зарегистрирован в Gamee до этого логина; рефы из YAML не применяются.
    gamee_preexisting: bool = False
    proxy_url: str | None = None  # только HTTP к API Gamee (api2.gamee.com); пусто — без прокси
    # Дата первой инициализации аккаунта (ISO-8601). Используется для warmup phase —
    # новые аккаунты играют меньше первые дни (имитация изучения интерфейса).
    created_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"label": self.label, "install_uuid": self.install_uuid}
        if self.telethon_session:
            d["telethon_session"] = self.telethon_session
        else:
            d["init_data"] = self.init_data
        if self.gamee_ref and str(self.gamee_ref).strip():
            d["gamee_ref"] = str(self.gamee_ref).strip()
        if self.telegram_referral_ref is not None:
            d["telegram_referral_ref"] = int(self.telegram_referral_ref)
        if self.gamee_preexisting:
            d["gamee_preexisting"] = True
        pu = normalize_gamee_proxy_url(self.proxy_url)
        if pu:
            d["proxy_url"] = pu
        if self.created_at:
            d["created_at"] = self.created_at
        return d

    @staticmethod
    def from_dict(d: dict[str, Any], index: int, accounts_yaml_dir: Path) -> AccountRecord:
        label = str(d.get("label") or f"account_{index + 1}").strip()
        init = str(d.get("init_data", "")).strip()
        ts_raw = d.get("telethon_session")
        ts = str(ts_raw).strip() if ts_raw is not None else ""
        if not init and not ts:
            raise ValueError(f"Аккаунт «{label}»: укажите init_data или telethon_session")
        if init and ts:
            raise ValueError(
                f"Аккаунт «{label}»: только одно из полей — init_data или telethon_session"
            )
        gamee_preexisting = bool(d.get("gamee_preexisting"))
        gr_raw = d.get("gamee_ref")
        gamee_ref = str(gr_raw).strip() if gr_raw is not None and str(gr_raw).strip() else None
        tr_raw = d.get("telegram_referral_ref")
        telegram_referral_ref: int | None = None
        if tr_raw is not None and str(tr_raw).strip():
            try:
                telegram_referral_ref = int(str(tr_raw).strip())
                if telegram_referral_ref <= 0:
                    telegram_referral_ref = None
            except ValueError as e:
                raise ValueError(
                    f"Аккаунт «{label}»: telegram_referral_ref должно быть целым числом"
                ) from e
        if gamee_preexisting:
            gamee_ref = None
            telegram_referral_ref = None
        pr_raw = d.get("proxy_url")
        proxy_url = normalize_gamee_proxy_url(pr_raw) if pr_raw is not None else None
        iu = d.get("install_uuid")
        if iu is None or str(iu).strip() == "":
            # UUID4 (random) — реальные устройства генерируют рандомный UUID при первом запуске.
            # В отличие от UUID5(init_data), это НЕ детерминировано — нельзя восстановить корреляцию.
            install = str(uuid.uuid4())
        else:
            install = str(iu).strip()
        created_at = d.get("created_at")
        if created_at is None:
            # Если поле отсутствует — это новый аккаунт; ставим текущую дату ISO.
            from datetime import datetime, timezone
            created_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        return AccountRecord(
            label=label,
            init_data=init,
            install_uuid=install,
            telethon_session=ts if ts else None,
            gamee_ref=gamee_ref,
            telegram_referral_ref=telegram_referral_ref,
            gamee_preexisting=gamee_preexisting,
            proxy_url=proxy_url,
            created_at=str(created_at).strip() if created_at else None,
        )


def load_accounts(path: Path) -> list[AccountRecord]:
    data = read_yaml_mapping(path)
    items = data.get("accounts")
    if not items:
        return []
    if not isinstance(items, list):
        raise ValueError("accounts.yaml: поле accounts должно быть списком")
    base = path.parent.resolve()
    out: list[AccountRecord] = []
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        out.append(AccountRecord.from_dict(item, i, base))
    return out


def _write_yaml_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = yaml.safe_dump(payload, allow_unicode=True, sort_keys=False)
    fd, tmp_name = tempfile.mkstemp(
        prefix=path.name + ".",
        suffix=".tmp",
        dir=str(path.parent),
        text=True,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(data)
        Path(tmp_name).replace(path)
    except Exception:
        try:
            Path(tmp_name).unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _telethon_session_key(record: AccountRecord, base_dir: Path) -> str | None:
    if not record.telethon_session:
        return None
    p = Path(record.telethon_session)
    if not p.is_absolute():
        p = (base_dir / p).resolve()
    else:
        p = p.resolve()
    return str(p).casefold()


def _ensure_account_append_is_safe(path: Path, record: AccountRecord) -> None:
    existing = load_accounts(path)
    want_label = record.label.strip().casefold()
    for acc in existing:
        if acc.label.strip().casefold() == want_label:
            raise ValueError(f"Аккаунт «{record.label}» уже существует")
    new_session_key = _telethon_session_key(record, path.parent.resolve())
    if new_session_key is None:
        return
    for acc in existing:
        cur_key = _telethon_session_key(acc, path.parent.resolve())
        if cur_key == new_session_key:
            raise ValueError(
                f"Telethon-сессия для «{record.label}» конфликтует с аккаунтом «{acc.label}»"
            )


def save_accounts_template(path: Path, accounts: list[AccountRecord]) -> None:
    payload = {"accounts": [a.to_dict() for a in accounts]}
    _write_yaml_atomic(path, payload)


def _delete_telethon_session_files(session_path: Path) -> None:
    """Удаляет .session и типичные хвосты SQLite (-wal, -shm, -journal), если есть."""
    candidates = [
        session_path,
        Path(str(session_path) + "-journal"),
        Path(str(session_path) + "-wal"),
        Path(str(session_path) + "-shm"),
    ]
    for p in candidates:
        try:
            if p.is_file():
                p.unlink()
        except OSError:
            pass


def remove_account_by_label(path: Path, label: str) -> tuple[bool, Path | None]:
    """
    Удаляет аккаунт из accounts.yaml.
    Если у записи был telethon_session — удаляет файл сессии на диске (перелогин с нуля).
    Возвращает (успех, путь к .session что удаляли или None).
    """
    want = label.strip()
    if not want:
        return False, None
    data = read_yaml_mapping(path)
    items = data.get("accounts")
    if not isinstance(items, list):
        return False, None
    base = path.parent.resolve()
    session_file: Path | None = None
    new_items: list[Any] = []
    found = False
    for x in items:
        if isinstance(x, dict) and str(x.get("label", "")).strip() == want:
            found = True
            ts = x.get("telethon_session")
            if ts and str(ts).strip():
                p = Path(str(ts).strip())
                if not p.is_absolute():
                    p = (base / p).resolve()
                else:
                    p = p.resolve()
                session_file = p
            continue
        new_items.append(x)
    if not found:
        return False, None
    data["accounts"] = new_items
    _write_yaml_atomic(path, data)
    if session_file is not None:
        _delete_telethon_session_files(session_file)
    return True, session_file


def set_account_gamee_registration_state(
    path: Path,
    label: str,
    *,
    brand_new_user: bool,
) -> None:
    """
    После loginUsingTelegram: brand_new_user=True — новая регистрация в Gamee; иначе аккаунт уже был,
    рефы в YAML удаляем и ставим gamee_preexisting (рефы больше не применяются).
    """
    want = label.strip()
    if not want:
        return
    data = read_yaml_mapping(path)
    items = data.get("accounts")
    if not isinstance(items, list):
        return
    changed = False
    for x in items:
        if not isinstance(x, dict) or str(x.get("label", "")).strip() != want:
            continue
        if brand_new_user:
            if "gamee_preexisting" in x:
                del x["gamee_preexisting"]
                changed = True
        else:
            x["gamee_preexisting"] = True
            x.pop("gamee_ref", None)
            x.pop("telegram_referral_ref", None)
            changed = True
        break
    if not changed:
        return
    data["accounts"] = items
    _write_yaml_atomic(path, data)


def set_account_proxy_url(path: Path, label: str, raw: str | None) -> bool:
    """Записывает proxy_url для аккаунта (нормализованный URL или снятие прокси при пустом вводе)."""
    want_label = label.strip()
    if not want_label:
        return False
    want = normalize_gamee_proxy_url(raw) if raw and str(raw).strip() else None
    data = read_yaml_mapping(path)
    items = data.get("accounts")
    if not isinstance(items, list):
        return False
    changed = False
    for x in items:
        if not isinstance(x, dict):
            continue
        if str(x.get("label", "")).strip() != want_label:
            continue
        changed = True
        if want:
            x["proxy_url"] = want
        else:
            x.pop("proxy_url", None)
        break
    if not changed:
        return False
    data["accounts"] = items
    _write_yaml_atomic(path, data)
    return True


def append_account(path: Path, record: AccountRecord) -> None:
    _ensure_account_append_is_safe(path, record)
    data = read_yaml_mapping(path)
    items = data.get("accounts")
    if items is None:
        items = []
    if not isinstance(items, list):
        raise ValueError("accounts.yaml: поле accounts должно быть списком")
    items.append(record.to_dict())
    data["accounts"] = items
    _write_yaml_atomic(path, data)
