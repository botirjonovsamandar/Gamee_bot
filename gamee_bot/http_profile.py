"""Стабильный «браузерный» профиль на аккаунт: TLS (curl_cffi) и HTTP-заголовки согласованы.

Профиль выбирается детерминированно из `label` (один и тот же label → тот же Chrome/язык).

Header ordering strictly matches Chrome Android WebView byte-order as observed
in production traffic captures. HTTP/2 SETTINGS frames are configured to match
Android Chrome's BoringSSL stack.
"""
from __future__ import annotations

import hashlib
import re
from collections import OrderedDict
from dataclasses import dataclass
from functools import lru_cache
from random import Random
from typing import Any

# curl_cffi: только поддерживаемые имена; второе число — major для UA / sec-ch-ua.
_CHROME_ANDROID: tuple[tuple[str, int], ...] = (
    # weighted distribution: новые версии чаще
    ("chrome131_android", 131),  # latest stable x3
    ("chrome131_android", 131),
    ("chrome131_android", 131),
    ("chrome133_android", 133),
    ("chrome133_android", 133),
    ("chrome126_android", 126),
    ("chrome126_android", 126),
    ("chrome124_android", 124),
    ("chrome124_android", 124),
    ("chrome120_android", 120),
    ("chrome119_android", 119),
    ("chrome116_android", 116),
)

_ACCEPT_LANGUAGE = (
    "ru,en-US;q=0.9,en;q=0.8,uk;q=0.7",
    "ru,en;q=0.9,en-US;q=0.8",
    "ru;q=0.9,en-US;q=0.8,en;q=0.7",
    "ru,uk;q=0.9,en-US;q=0.8,en;q=0.7",
    "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
)

_PRIORITY = (
    "u=1, i",
    "u=1",
    "i",
)

# Пул экранов реальных Android-устройств (width, height, dpr).
# Размеры CSS-пикселей viewport'а Telegram Mini App (без status bar).
_SCREEN_POOL: tuple[tuple[int, int, float], ...] = (
    # Samsung
    (384, 824, 3.0),     # Galaxy S22/S23/S24 Ultra
    (360, 800, 3.0),     # Galaxy S22, A54
    (360, 780, 2.0),     # Galaxy A34
    (385, 854, 2.625),   # Galaxy S23+
    (320, 692, 3.0),     # Galaxy A52
    (376, 836, 3.0),     # Galaxy Z Flip5
    (768, 1080, 2.5),    # Galaxy Z Fold5 (раскрытый)
    # Xiaomi/POCO
    (393, 852, 2.75),    # Xiaomi 12 Pro
    (393, 873, 2.75),    # Xiaomi 14 Pro
    (393, 874, 2.625),   # Xiaomi 13
    (393, 851, 2.75),    # Redmi Note 13 Pro
    (393, 873, 2.625),   # POCO F5
    (393, 851, 2.75),    # POCO X5 Pro
    (393, 873, 2.5),     # Redmi Note 12
    (412, 892, 2.5),     # Redmi 10C
    # Pixel
    (412, 915, 2.625),   # Pixel 7/8 Pro
    (412, 892, 2.625),   # Pixel 8
    (412, 869, 2.625),   # Pixel 6a / Pixel 7a
    (411, 914, 2.6),     # Pixel 6
    # OnePlus
    (412, 892, 3.5),     # OnePlus 11/12
    (412, 919, 2.625),   # OnePlus 9 Pro
    (412, 869, 3.0),     # OnePlus 10 Pro
    # realme/Honor/vivo
    (393, 873, 2.75),    # realme 11 Pro
    (412, 919, 2.625),   # realme GT 5 Pro
    (393, 852, 2.625),   # Honor Magic 5
    (412, 892, 2.625),   # Honor 90
    (393, 873, 2.5),     # vivo X90
    (412, 919, 2.625),   # vivo X100
    # Sony
    (393, 851, 2.75),    # Sony Xperia 1 V
    (412, 915, 2.625),   # Sony Xperia 1 IV
)

# ── HTTP/2 SETTINGS matching Chrome Android BoringSSL ──────────────────
# Captured from Chrome 131 on Android 14 (Samsung Galaxy S24 Ultra).
# Format: "id:value" pairs separated by semicolons.
# SETTINGS_HEADER_TABLE_SIZE=65536, SETTINGS_ENABLE_PUSH=0 (WebView never accepts push),
# SETTINGS_INITIAL_WINDOW_SIZE=6291456, SETTINGS_MAX_HEADER_LIST_SIZE=262144,
# SETTINGS_MAX_CONCURRENT_STREAMS=1000 (Chrome default).
CHROME_ANDROID_H2_SETTINGS = "1:65536;2:0;3:1000;4:6291456;6:262144"

# HTTP/2 WINDOW_UPDATE (connection-level): Chrome sends 15663105 (15MB).
CHROME_ANDROID_H2_WINDOW_UPDATE = 15663105

# HTTP/2 pseudo-header order for Chrome: :method, :authority, :scheme, :path
CHROME_ANDROID_H2_PSEUDO_ORDER = "masp"

# HTTP/2 stream settings: weight=256, exclusive=1 (Chrome default)
CHROME_ANDROID_H2_STREAM_WEIGHT = 256
CHROME_ANDROID_H2_STREAM_EXCLUSIVE = 1

# ── TLS Signature Algorithms (Chrome Android BoringSSL order) ──────────
# This matches the exact order Chrome 131 Android sends in ClientHello.
CHROME_ANDROID_TLS_SIG_ALGS = [
    "ecdsa_secp256r1_sha256",
    "rsa_pss_rsae_sha256",
    "rsa_pkcs1_sha256",
    "ecdsa_secp384r1_sha384",
    "rsa_pss_rsae_sha384",
    "rsa_pkcs1_sha384",
    "rsa_pss_rsae_sha512",
    "rsa_pkcs1_sha512",
]


def rotate_install_uuid(base_uuid: str, period_days: int = 45) -> str:
    """Имитация "переустановки приложения" раз в N дней.

    Реальные пользователи иногда переустанавливают Telegram → новый install_uuid.
    Детерминированно по (base_uuid, epoch//period_days).
    """
    if not base_uuid:
        return base_uuid
    import time
    epoch = int(time.time() // (period_days * 86400))
    if epoch == 0:
        return base_uuid
    digest = hashlib.sha256(f"{base_uuid}|epoch{epoch}".encode("utf-8")).digest()
    # Формат UUID4: ....-....-4...-....-............ (version 4, variant)
    h = digest.hex()
    return f"{h[0:8]}-{h[8:12]}-4{h[13:16]}-{h[16:20]}-{h[20:32]}"


# WebGL renderer/vendor pool — реальные значения из Android устройств
_WEBGL_POOL: tuple[tuple[str, str], ...] = (
    ("Adreno (TM) 740", "Qualcomm"),     # Snapdragon 8 Gen 2 (S23/S24)
    ("Adreno (TM) 730", "Qualcomm"),     # Snapdragon 8 Gen 1 (S22)
    ("Adreno (TM) 750", "Qualcomm"),     # Snapdragon 8 Gen 3 (S24 Ultra)
    ("Adreno (TM) 660", "Qualcomm"),     # Snapdragon 888
    ("Adreno (TM) 650", "Qualcomm"),     # Snapdragon 865
    ("Mali-G715 MP7", "ARM"),            # Tensor G3 (Pixel 8)
    ("Mali-G710 MC10", "ARM"),           # Tensor G2 (Pixel 7)
    ("Mali-G78 MP20", "ARM"),            # Exynos 2100/2200
    ("Mali-G77 MP11", "ARM"),            # Exynos 990
    ("PowerVR Rogue GE8320", "Imagination Technologies"),  # бюджетные MediaTek
    ("Mali-G57 MC2", "ARM"),             # Helio G88
)


def webgl_profile_for_label(label: str) -> tuple[str, str]:
    """Стабильный per-label WebGL profile (renderer, vendor)."""
    digest = hashlib.sha256((label or "_default").encode("utf-8")).digest()
    idx = digest[2] % len(_WEBGL_POOL)
    return _WEBGL_POOL[idx]


@dataclass(frozen=True, slots=True)
class GameeHttpClientProfile:
    """Один согласованный набор: impersonate (TLS) + заголовки как у Chrome Android WebView."""

    impersonate: str
    user_agent: str
    sec_ch_ua: str
    sec_ch_ua_mobile: str
    sec_ch_ua_platform: str
    sec_ch_ua_full_version: str
    sec_ch_ua_full_version_list: str
    accept_language: str
    priority: str
    client_language: str
    screen_width: int
    screen_height: int
    device_pixel_ratio: float

    def build_extra_fp(self) -> dict[str, Any]:
        """Build ExtraFingerprints kwargs matching Chrome Android BoringSSL."""
        return {
            "tls_min_version": 6,  # CurlSslVersion.TLSv1_2
            "tls_grease": True,    # Chrome uses GREASE
            "tls_permute_extensions": True,  # Chrome randomizes extension order
            "tls_cert_compression": "brotli",
            "tls_signature_algorithms": list(CHROME_ANDROID_TLS_SIG_ALGS),
            "http2_stream_weight": CHROME_ANDROID_H2_STREAM_WEIGHT,
            "http2_stream_exclusive": CHROME_ANDROID_H2_STREAM_EXCLUSIVE,
        }

    def ordered_api_headers(
        self,
        *,
        install_uuid: str = "",
        auth_token: str = "",
    ) -> OrderedDict[str, str]:
        """Build API POST headers in strict Chrome Android byte-order.

        Chrome Android WebView sends headers in this exact order:
        1. content-length (added by curl)
        2. sec-ch-ua
        3. sec-ch-ua-full-version
        4. sec-ch-ua-full-version-list
        5. sec-ch-ua-mobile
        6. sec-ch-ua-platform
        7. content-type
        8. user-agent
        9. x-bot-header (required by Gamee API)
        10. x-install-uuid (если есть, ротируется по дате)
        11. x-requested-with
        12. accept
        13. client-language
        14. origin
        15. sec-fetch-site
        16. sec-fetch-mode
        17. sec-fetch-dest
        18. referer
        19. accept-encoding
        20. accept-language
        21. priority
        22. authorization (if present)
        """
        h = OrderedDict()
        # content-length is added by curl_cffi automatically; we don't set it
        h["sec-ch-ua"] = self.sec_ch_ua
        h["sec-ch-ua-full-version"] = self.sec_ch_ua_full_version
        h["sec-ch-ua-full-version-list"] = self.sec_ch_ua_full_version_list
        h["sec-ch-ua-mobile"] = self.sec_ch_ua_mobile
        h["sec-ch-ua-platform"] = self.sec_ch_ua_platform
        h["content-type"] = "text/plain;charset=UTF-8"
        h["user-agent"] = self.user_agent
        h["x-bot-header"] = "gamee"
        if install_uuid:
            # Ротация раз в 45 дней — имитация "переустановки приложения".
            h["x-install-uuid"] = rotate_install_uuid(install_uuid, period_days=45)
        h["x-requested-with"] = "org.telegram.messenger"
        # NET-2: x-request-id — реальные tracing-aware клиенты часто его шлют
        import uuid as _uuid_mod
        h["x-request-id"] = _uuid_mod.uuid4().hex[:16]
        h["accept"] = "*/*"
        h["client-language"] = self.client_language
        h["origin"] = "https://prizes.gamee.com"
        h["sec-fetch-site"] = "same-site"
        h["sec-fetch-mode"] = "cors"
        h["sec-fetch-dest"] = "empty"
        h["referer"] = "https://prizes.gamee.com/"
        h["accept-encoding"] = "gzip, deflate, br, zstd"
        h["accept-language"] = self.accept_language
        h["priority"] = self.priority
        if auth_token:
            h["authorization"] = f"Bearer {auth_token}"
        return h

    def ordered_navigation_headers(self) -> OrderedDict[str, str]:
        """Build GET navigation headers in strict Chrome Android byte-order.

        Chrome Android WebView navigation order:
        1. sec-ch-ua
        2. sec-ch-ua-full-version
        3. sec-ch-ua-full-version-list
        4. sec-ch-ua-mobile
        5. sec-ch-ua-platform
        6. upgrade-insecure-requests
        7. user-agent
        8. x-requested-with
        9. accept
        10. sec-fetch-site
        11. sec-fetch-mode
        12. sec-fetch-dest
        13. sec-fetch-user
        14. accept-encoding
        15. accept-language
        """
        h = OrderedDict()
        h["sec-ch-ua"] = self.sec_ch_ua
        h["sec-ch-ua-full-version"] = self.sec_ch_ua_full_version
        h["sec-ch-ua-full-version-list"] = self.sec_ch_ua_full_version_list
        h["sec-ch-ua-mobile"] = self.sec_ch_ua_mobile
        h["sec-ch-ua-platform"] = self.sec_ch_ua_platform
        h["upgrade-insecure-requests"] = "1"
        h["user-agent"] = self.user_agent
        h["x-requested-with"] = "org.telegram.messenger"
        h["accept"] = (
            "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,"
            "image/apng,*/*;q=0.8"
        )
        h["sec-fetch-site"] = "none"
        h["sec-fetch-mode"] = "navigate"
        h["sec-fetch-dest"] = "document"
        h["sec-fetch-user"] = "?1"
        h["accept-encoding"] = "gzip, deflate, br, zstd"
        h["accept-language"] = self.accept_language
        return h


@lru_cache(maxsize=1)
def _supported_chrome_android() -> tuple[tuple[str, int], ...]:
    """Пул профилей, который реально поддерживает установленный curl_cffi."""
    try:
        from curl_cffi.requests.impersonate import BrowserType

        supported = {
            item.value if hasattr(item, "value") else str(item)
            for item in BrowserType
        }
        filtered = tuple(item for item in _CHROME_ANDROID if item[0] in supported)
        if filtered:
            return filtered
        dynamic_android: list[tuple[str, int]] = []
        for name in sorted(supported):
            m = re.fullmatch(r"chrome(\d+)_android", name)
            if m is None:
                continue
            dynamic_android.append((name, int(m.group(1))))
        if dynamic_android:
            return tuple(dynamic_android)
    except Exception:
        pass
    return _CHROME_ANDROID


def gamee_http_profile_for_label(label: str) -> GameeHttpClientProfile:
    """Уникальный, но постоянный для данного label профиль (псевдослучайный из реалистичного пула)."""
    key = (label or "").strip() or "_default"
    digest = hashlib.sha256(key.encode("utf-8")).digest()
    rng = Random(int.from_bytes(digest[:16], "big"))

    imp, major = rng.choice(_supported_chrome_android())
    scr_w, scr_h, scr_dpr = rng.choice(_SCREEN_POOL)
    accept = rng.choice(_ACCEPT_LANGUAGE)
    priority = rng.choice(_PRIORITY)

    # Реалистичные patch-базы по major-версиям (из реальных Chrome release notes).
    _PATCH_BASE: dict[int, int] = {
        116: 5845, 119: 6045, 120: 6099, 121: 6167, 122: 6261,
        124: 6367, 125: 6422, 126: 6478, 127: 6533, 128: 6613,
        129: 6668, 130: 6723, 131: 6778, 132: 6834, 133: 6943,
    }
    patch_base = _PATCH_BASE.get(major, 6099 + (major - 120) * 50)
    patch_mid = patch_base + ((digest[4] * 256 + digest[5]) % 200)
    patch_low = digest[6] % 256
    full_ver = f"{major}.0.{patch_mid}.{patch_low}"

    ua = (
        "Mozilla/5.0 (Linux; Android 14; K) AppleWebKit/537.36 "
        f"(KHTML, like Gecko) Version/4.0 Chrome/{full_ver} Mobile Safari/537.36"
    )
    if major >= 120:
        sec_ch_ua = (
            f'"Not(A:Brand";v="8", "Chromium";v="{major}", "Google Chrome";v="{major}"'
        )
        not_full = '"Not(A:Brand";v="8.0.0.0"'
    else:
        sec_ch_ua = (
            f'"Not:A-Brand";v="99", "Chromium";v="{major}", "Google Chrome";v="{major}"'
        )
        not_full = '"Not:A-Brand";v="99.0.0.0"'
    full_list = (
        f'"Chromium";v="{full_ver}", "Google Chrome";v="{full_ver}", {not_full}'
    )

    # Извлекаем основной язык из Accept-Language (например "ru" из "ru,en-US;q=0.9").
    _primary = accept.split(",")[0].split(";")[0].split("-")[0].strip() if accept else "en"

    return GameeHttpClientProfile(
        impersonate=imp,
        user_agent=ua,
        sec_ch_ua=sec_ch_ua,
        sec_ch_ua_mobile="?1",
        sec_ch_ua_platform='"Android"',
        sec_ch_ua_full_version=f'"{full_ver}"',
        sec_ch_ua_full_version_list=full_list,
        accept_language=accept,
        priority=priority,
        client_language=_primary,
        screen_width=scr_w,
        screen_height=scr_h,
        device_pixel_ratio=scr_dpr,
    )

