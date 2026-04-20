from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable, Protocol

from curl_cffi import requests as curl_requests

from gamee_bot.http_profile import GameeHttpClientProfile
from gamee_bot.proxy_url import validate_proxy_url_for_httpx


GAMEE_TRANSPORT_BACKEND_CURL_CFFI_RAW_HTTP = "curl_cffi_raw_http"
GAMEE_TRANSPORT_BACKEND_TELEGRAM_WEBVIEW = "telegram_webview"


def normalize_gamee_transport_backend(raw: str | None) -> str:
    s = str(raw or "").strip().lower().replace("-", "_")
    if not s:
        return GAMEE_TRANSPORT_BACKEND_CURL_CFFI_RAW_HTTP
    aliases = {
        "curl": GAMEE_TRANSPORT_BACKEND_CURL_CFFI_RAW_HTTP,
        "curl_cffi": GAMEE_TRANSPORT_BACKEND_CURL_CFFI_RAW_HTTP,
        "raw_http": GAMEE_TRANSPORT_BACKEND_CURL_CFFI_RAW_HTTP,
        "curl_cffi_raw_http": GAMEE_TRANSPORT_BACKEND_CURL_CFFI_RAW_HTTP,
        "telegram_webview": GAMEE_TRANSPORT_BACKEND_TELEGRAM_WEBVIEW,
        "telegram_webview_stub": GAMEE_TRANSPORT_BACKEND_TELEGRAM_WEBVIEW,
    }
    return aliases.get(s, s)


def gamee_transport_backend_blocker_message(raw: str | None) -> str | None:
    backend = normalize_gamee_transport_backend(raw)
    if backend == GAMEE_TRANSPORT_BACKEND_CURL_CFFI_RAW_HTTP:
        return None
    if backend == GAMEE_TRANSPORT_BACKEND_TELEGRAM_WEBVIEW:
        return (
            "Выбран transport_backend='telegram_webview', но browser-backed runtime "
            "пока не реализован. Сейчас доступен только transport_backend='curl_cffi_raw_http'."
        )
    return (
        f"Неизвестный transport_backend={raw!r}. "
        "Допустимые значения: 'curl_cffi_raw_http' (доступен сейчас) и "
        "'telegram_webview' (зарезервирован для будущего runtime)."
    )


@dataclass(frozen=True, slots=True)
class GameeTransportCapabilities:
    """Короткое описание возможностей backend-а транспорта."""

    supports_cookie_jar: bool
    runtime_kind: str
    embedded_webview_ready: bool
    notes: str = ""


@dataclass(frozen=True, slots=True)
class GameeTransportRequest:
    """Нормализованный запрос транспорта.

    `purpose` оставлен как расширяемый маркер: будущий backend может различать
    bootstrap/navigation/API-шаги и обрабатывать их по-разному.
    """

    method: str
    url: str
    headers: dict[str, str]
    timeout: tuple[float, float]
    data: bytes | None = None
    allow_redirects: bool = False
    purpose: str = "api"


@dataclass(frozen=True, slots=True)
class TelegramMiniAppBrowserContext:
    """Снимок состояния browser-backed Telegram Mini App runtime."""

    has_window_telegram_webapp: bool
    init_data_raw: str | None
    user_agent: str | None
    platform: str = "telegram_mini_app"
    init_data_validation_hash: str | None = None
    input_telemetry_events: list[dict[str, Any]] | None = None
    notes: str = ""


class TelegramMiniAppBrowserContextProvider(Protocol):
    """Источник browser/webview context для будущего production backend-а."""

    def current_context(self) -> TelegramMiniAppBrowserContext:
        ...


class GameeTransportError(OSError):
    """Сетевой/транспортный сбой без HTTP-статуса."""


class GameeTransportHTTPError(RuntimeError):
    """HTTP-ошибка нормализована независимо от backend-а."""

    def __init__(self, message: str, response: "GameeTransportResponse") -> None:
        super().__init__(message)
        self.response = response


class GameeTransportResponse:
    """Единый ответ транспорта поверх конкретного HTTP/backend-клиента."""

    def __init__(
        self,
        *,
        status_code: int,
        text: str | None = None,
        text_loader: Callable[[], str] | None = None,
        json_loader: Callable[[], Any] | None = None,
    ) -> None:
        self.status_code = int(status_code)
        self._text = text
        self._text_loader = text_loader
        self._json_loader = json_loader
        self._json_cache: Any = _JSON_UNSET

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 400

    @property
    def text(self) -> str:
        if self._text is None:
            if self._text_loader is None:
                self._text = ""
            else:
                raw = self._text_loader()
                self._text = str(raw or "")
        return self._text

    def json(self) -> Any:
        if self._json_cache is _JSON_UNSET:
            if self._json_loader is not None:
                self._json_cache = self._json_loader()
            else:
                self._json_cache = json.loads(self.text)
        return self._json_cache

    def raise_for_status(self) -> None:
        if self.ok:
            return
        raise GameeTransportHTTPError(f"HTTP Error {self.status_code}", self)


class GameeTransport(Protocol):
    backend_name: str
    proxy_url: str | None
    capabilities: GameeTransportCapabilities

    def close(self) -> None:
        ...

    def send(self, request: GameeTransportRequest) -> GameeTransportResponse:
        ...


class CurlCffiGameeTransport:
    """Transport adapter: curl_cffi HTTP + headless V8 JS sandbox.

    The JS sandbox provides a ``window.Telegram.WebApp`` environment and
    can execute Cloudflare/integrity challenge scripts without a real browser.
    """

    backend_name = GAMEE_TRANSPORT_BACKEND_CURL_CFFI_RAW_HTTP
    capabilities = GameeTransportCapabilities(
        supports_cookie_jar=True,
        runtime_kind="raw_http_with_js_sandbox",
        embedded_webview_ready=False,
        notes="curl_cffi HTTP + V8 JS sandbox for challenge resolution and env emulation.",
    )

    def __init__(
        self,
        *,
        proxy_url: str | None = None,
        http_profile: GameeHttpClientProfile,
        init_data: str = "",
    ) -> None:
        self.proxy_url = proxy_url
        if proxy_url:
            validate_proxy_url_for_httpx(proxy_url)
        proxies: dict[str, str] | None = None
        if proxy_url:
            proxies = {"http": proxy_url, "https": proxy_url}

        # Build ExtraFingerprints for TLS/HTTP2 alignment
        extra_fp = None
        try:
            from curl_cffi.requests import ExtraFingerprints
            extra_fp = ExtraFingerprints(**http_profile.build_extra_fp())
        except Exception:
            pass

        self._client = curl_requests.Session(
            impersonate=http_profile.impersonate,
            extra_fp=extra_fp,
        )

        # Apply HTTP/2 SETTINGS, WINDOW_UPDATE, and pseudo-header order
        try:
            from curl_cffi import CurlOpt
            from gamee_bot.http_profile import (
                CHROME_ANDROID_H2_SETTINGS,
                CHROME_ANDROID_H2_WINDOW_UPDATE,
                CHROME_ANDROID_H2_PSEUDO_ORDER,
            )
            self._client.curl_options[CurlOpt.HTTP2_SETTINGS] = CHROME_ANDROID_H2_SETTINGS.encode()
            self._client.curl_options[CurlOpt.HTTP2_WINDOW_UPDATE] = CHROME_ANDROID_H2_WINDOW_UPDATE
            self._client.curl_options[CurlOpt.HTTP2_PSEUDO_HEADERS_ORDER] = CHROME_ANDROID_H2_PSEUDO_ORDER.encode()
        except Exception:
            pass

        if proxies:
            self._client.proxies.update(proxies)

        # Initialize V8 JS sandbox
        self._js_runtime: Any = None
        try:
            from gamee_bot.js_runtime import (
                JSRuntimeConfig,
                TelegramWebAppConfig,
                TelegramWebViewJSRuntime,
            )
            self._js_runtime = TelegramWebViewJSRuntime.create(
                js_config=JSRuntimeConfig(user_agent=http_profile.user_agent),
                tg_config=TelegramWebAppConfig(
                    init_data=init_data,
                    platform="android",
                ),
            )
        except Exception:
            # Non-fatal: sandbox is an enhancement, not a hard requirement
            import logging
            logging.getLogger(__name__).debug(
                "JS sandbox init failed — running without challenge resolution",
                exc_info=True,
            )

        # Initialize input telemetry generator
        self._telemetry_gen: Any = None
        try:
            from gamee_bot.input_telemetry import InputTelemetryGenerator
            self._telemetry_gen = InputTelemetryGenerator(
                screen_width=393,
                screen_height=852,
                device_pixel_ratio=2.75,
            )
        except Exception:
            import logging
            logging.getLogger(__name__).debug(
                "Telemetry generator init failed",
                exc_info=True,
            )

    @property
    def js_runtime(self) -> Any:
        """Access the V8 JS sandbox (may be None if init failed)."""
        return self._js_runtime

    @property
    def telemetry_generator(self) -> Any:
        """Access the input telemetry generator (may be None if init failed)."""
        return self._telemetry_gen

    def close(self) -> None:
        self._client.close()
        if self._js_runtime is not None:
            self._js_runtime.close()
            self._js_runtime = None

    def send(self, request: GameeTransportRequest) -> GameeTransportResponse:
        try:
            raw = self._client.request(
                request.method,
                request.url,
                headers=request.headers,
                data=request.data,
                timeout=request.timeout,
                allow_redirects=request.allow_redirects,
            )
        except OSError as e:
            raise GameeTransportError(str(e)) from e
        return GameeTransportResponse(
            status_code=int(getattr(raw, "status_code", 0) or 0),
            text_loader=lambda raw_response=raw: raw_response.text or "",
            json_loader=lambda raw_response=raw: raw_response.json(),
        )


class TelegramMiniAppWebViewTransport:
    """Boilerplate для будущего backend-а, работающего через реальный WebView/browser runtime.

    Здесь намеренно нет реализации. Предполагаемая зона ответственности будущего backend-а:
    - управлять реальным browser/webview context;
    - выполнять bootstrap/navigation в одном профиле хранилища/cookie jar;
    - сохранять DOM/storage/service worker state между шагами;
    - отображать тот же interface `send()`, чтобы `GameeClient` не знал о деталях runtime.
    """

    backend_name = GAMEE_TRANSPORT_BACKEND_TELEGRAM_WEBVIEW
    capabilities = GameeTransportCapabilities(
        supports_cookie_jar=True,
        runtime_kind="telegram_webview",
        embedded_webview_ready=False,
        notes="Только каркас. Реальный browser-backed backend ещё не реализован.",
    )

    def __init__(
        self,
        *,
        profile_name: str = "telegram_mini_app_webview",
        context_provider: TelegramMiniAppBrowserContextProvider | None = None,
    ) -> None:
        self.proxy_url = None
        self._profile_name = profile_name
        self._context_provider = context_provider

    def close(self) -> None:
        return None

    def send(self, request: GameeTransportRequest) -> GameeTransportResponse:
        context_note = ""
        if self._context_provider is not None:
            try:
                ctx = self._context_provider.current_context()
                context_note = (
                    f" Current context: has_window_telegram_webapp={ctx.has_window_telegram_webapp},"
                    f" platform={ctx.platform!r}."
                )
            except Exception:
                context_note = " Context provider failed to produce browser runtime state."
        raise NotImplementedError(
            "TelegramMiniAppWebViewTransport is a boilerplate only. "
            f"Implement browser-backed handling for purpose={request.purpose!r} in profile {self._profile_name!r}."
            + context_note
        )


def build_default_gamee_transport(
    *,
    backend_name: str | None = None,
    proxy_url: str | None = None,
    http_profile: GameeHttpClientProfile,
    init_data: str = "",
) -> GameeTransport:
    backend = normalize_gamee_transport_backend(backend_name)
    if backend == GAMEE_TRANSPORT_BACKEND_CURL_CFFI_RAW_HTTP:
        return CurlCffiGameeTransport(proxy_url=proxy_url, http_profile=http_profile, init_data=init_data)
    if backend == GAMEE_TRANSPORT_BACKEND_TELEGRAM_WEBVIEW:
        raise NotImplementedError(
            "Выбран transport_backend='telegram_webview', но backend ещё не реализован. "
            "Переключите конфиг на transport_backend='curl_cffi_raw_http'."
        )
    raise ValueError(
        f"Неизвестный transport_backend={backend_name!r}. "
        "Используйте 'curl_cffi_raw_http' (доступен) или 'telegram_webview' (пока только каркас)."
    )


_JSON_UNSET = object()
