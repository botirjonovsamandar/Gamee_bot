"""Headless V8 JS sandbox for challenge resolution and environment emulation.

Uses PyMiniRacer (V8 engine) with a DOM/browser polyfill preload to emulate
a Telegram Android WebView environment. No page rendering — only script
execution for integrity checks, Cloudflare challenge computation, and
Telegram.WebApp context synthesis.

Thread-safe: each ``TelegramWebViewJSRuntime`` instance owns its own V8
isolate. Instances are designed to be long-lived (one per account session).
"""
from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_PRELOAD_JS_PATH = Path(__file__).with_name("js_preload.js")

# Telegram WebApp JS SDK version to report.
_TG_WEBAPP_VERSION = "8.0"


@lru_cache(maxsize=1)
def _load_preload_js() -> str:
    """Load the polyfill source once and cache it."""
    return _PRELOAD_JS_PATH.read_text(encoding="utf-8")


@dataclass(frozen=True, slots=True)
class JSRuntimeConfig:
    """Configuration injected into the V8 sandbox before any script runs."""

    user_agent: str = (
        "Mozilla/5.0 (Linux; Android 14; K) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Version/4.0 Chrome/131.0.0.0 Mobile Safari/537.36"
    )
    screen_width: int = 393
    screen_height: int = 852
    device_pixel_ratio: float = 2.75
    hardware_concurrency: int = 8
    device_memory: int = 8
    webgl_renderer: str = "Adreno (TM) 730"
    webgl_vendor: str = "Qualcomm"
    language: str = "en-US"
    languages: tuple[str, ...] = ("en-US", "en")
    platform: str = "Linux armv8l"
    # Battery state (рандомизируется per-session чтобы не было всегда charging+99%)
    battery_charging: bool = False
    battery_level: float = 0.78
    # Timezone offset в минутах относительно UTC (положительный для запада, отрицательный для востока).
    # Например для Москвы (UTC+3) timezone_offset_minutes = -180.
    # JS Date.getTimezoneOffset() возвращает именно этот формат (Java-style).
    timezone_offset_minutes: int = -180  # дефолт Moscow


@dataclass(frozen=True, slots=True)
class TelegramWebAppConfig:
    """Data for ``window.Telegram.WebApp`` synthesis."""

    init_data: str = ""
    init_data_unsafe: dict[str, Any] = field(default_factory=dict)
    version: str = _TG_WEBAPP_VERSION
    platform: str = "android"
    color_scheme: str = "dark"
    start_param: str | None = None


@dataclass(frozen=True, slots=True)
class JSEvalResult:
    """Result of a JS evaluation in the sandbox."""

    value: Any = None
    error: str | None = None
    elapsed_ms: float = 0.0

    @property
    def ok(self) -> bool:
        return self.error is None


class TelegramWebViewJSRuntime:
    """Isolated V8 sandbox emulating a Telegram Android WebView.

    Lifecycle::

        rt = TelegramWebViewJSRuntime.create(
            js_config=JSRuntimeConfig(user_agent=profile.user_agent),
            tg_config=TelegramWebAppConfig(init_data=session.init_data),
        )
        result = rt.eval("navigator.userAgent")
        result = rt.eval_challenge(challenge_js_code)
        rt.close()

    The sandbox is bootstrapped with:
    1. ``js_preload.js`` — full DOM/navigator/window polyfill
    2. Dynamic config injection — user_agent, screen dimensions, etc.
    3. ``window.Telegram.WebApp`` synthesis with real initData
    """

    def __init__(self) -> None:
        self._ctx: Any = None
        self._lock = threading.Lock()
        self._closed = False
        self._bootstrap_done = False

    @classmethod
    def create(
        cls,
        *,
        js_config: JSRuntimeConfig | None = None,
        tg_config: TelegramWebAppConfig | None = None,
    ) -> "TelegramWebViewJSRuntime":
        """Factory: create runtime and bootstrap the environment."""
        rt = cls()
        rt._bootstrap(js_config or JSRuntimeConfig(), tg_config)
        return rt

    def _bootstrap(
        self,
        js_config: JSRuntimeConfig,
        tg_config: TelegramWebAppConfig | None,
    ) -> None:
        """Initialize V8 isolate and inject environment polyfills."""
        from py_mini_racer import MiniRacer

        self._ctx = MiniRacer()

        # 1. Load and inject preload JS with config substitutions
        preload = _load_preload_js()
        preload = preload.replace(
            '__SANDBOX_USER_AGENT__',
            json.dumps(js_config.user_agent),
        )
        self._ctx.eval(preload)

        # 2. Inject dynamic config overrides
        self._inject_config(js_config)

        # 3. Synthesize window.Telegram.WebApp if config provided
        if tg_config is not None:
            self._inject_telegram_webapp(tg_config)

        self._bootstrap_done = True
        log.debug("JS sandbox bootstrapped (UA=%s)", js_config.user_agent[:60])

    def _inject_config(self, cfg: JSRuntimeConfig) -> None:
        """Override polyfill defaults with per-account config values."""
        assert self._ctx is not None
        overrides = f"""
(function() {{
  navigator.userAgent = {json.dumps(cfg.user_agent)};
  navigator.appVersion = {json.dumps(cfg.user_agent.replace("Mozilla/", ""))};
  navigator.platform = {json.dumps(cfg.platform)};
  navigator.language = {json.dumps(cfg.language)};
  navigator.languages = {json.dumps(list(cfg.languages))};
  navigator.hardwareConcurrency = {cfg.hardware_concurrency};
  navigator.deviceMemory = {cfg.device_memory};

  screen.width = {cfg.screen_width};
  screen.height = {cfg.screen_height};
  screen.availWidth = {cfg.screen_width};
  screen.availHeight = {cfg.screen_height - 27};

  window.innerWidth = {cfg.screen_width};
  window.innerHeight = {cfg.screen_height};
  window.outerWidth = {cfg.screen_width};
  window.outerHeight = {cfg.screen_height};
  window.devicePixelRatio = {cfg.device_pixel_ratio};

  if (window.visualViewport) {{
    window.visualViewport.width = {cfg.screen_width};
    window.visualViewport.height = {cfg.screen_height};
  }}

  window.navigator = navigator;
  window.screen = screen;

  // Per-account timezone (Date.getTimezoneOffset returns UTC offset in minutes,
  // negative for east of UTC like Moscow=-180, positive for west).
  (function(orig) {{
    Date.prototype.getTimezoneOffset = function() {{ return {cfg.timezone_offset_minutes}; }};
  }})(Date.prototype.getTimezoneOffset);

  // Per-account WebGL renderer/vendor. UNMASKED_RENDERER_WEBGL=37446, UNMASKED_VENDOR_WEBGL=37445.
  if (window.__SANDBOX_WEBGL_PARAMS__ === undefined) {{
    window.__SANDBOX_WEBGL_PARAMS__ = {{
      37446: {json.dumps(cfg.webgl_renderer)},
      37445: {json.dumps(cfg.webgl_vendor)},
      "RENDERER": {json.dumps(cfg.webgl_renderer)},
      "VENDOR": {json.dumps(cfg.webgl_vendor)},
    }};
  }}

  // Battery realism: разные значения per-session (без хардкода charging=true)
  navigator.getBattery = function() {{
    return Promise.resolve({{
      charging: {str(cfg.battery_charging).lower()},
      chargingTime: {str(cfg.battery_charging).lower()} ? Infinity : {0 if cfg.battery_charging else 7200 + (int(cfg.battery_level * 18000) % 3600)},
      dischargingTime: {str(cfg.battery_charging).lower()} ? Infinity : {3600 + int((1 - cfg.battery_level) * 14400)},
      level: {cfg.battery_level},
      addEventListener: function() {{}},
      removeEventListener: function() {{}}
    }});
  }};
}})();
"""
        self._ctx.eval(overrides)

    def _inject_telegram_webapp(self, tg: TelegramWebAppConfig) -> None:
        """Synthesize a production-grade window.Telegram.WebApp object."""
        assert self._ctx is not None

        init_data_unsafe = dict(tg.init_data_unsafe) if tg.init_data_unsafe else {}

        # Parse initData query string into initDataUnsafe if not provided
        if tg.init_data and not init_data_unsafe:
            try:
                from urllib.parse import parse_qsl
                pairs = parse_qsl(tg.init_data, keep_blank_values=True)
                for k, v in pairs:
                    if k == "user":
                        try:
                            init_data_unsafe[k] = json.loads(v)
                        except json.JSONDecodeError:
                            init_data_unsafe[k] = v
                    elif k == "auth_date":
                        try:
                            init_data_unsafe[k] = int(v)
                        except ValueError:
                            init_data_unsafe[k] = v
                    else:
                        init_data_unsafe[k] = v
            except Exception:
                pass

        webapp_js = f"""
(function() {{
  var _tgInitData = {json.dumps(tg.init_data)};
  var _tgInitDataUnsafe = {json.dumps(init_data_unsafe)};
  var _tgVersion = {json.dumps(tg.version)};
  var _tgPlatform = {json.dumps(tg.platform)};
  var _tgColorScheme = {json.dumps(tg.color_scheme)};

  var webapp = window.Telegram.WebApp;
  webapp.initData = _tgInitData;
  webapp.initDataUnsafe = _tgInitDataUnsafe;
  webapp.version = _tgVersion;
  webapp.platform = _tgPlatform;
  webapp.colorScheme = _tgColorScheme;

  webapp.isVersionAtLeast = function(v) {{
    return parseFloat(_tgVersion) >= parseFloat(v);
  }};

  // Ensure Telegram.WebApp.ready() triggers no errors
  webapp.ready = function() {{}};

  // TelegramGameProxy for game iframes
  window.TelegramGameProxy = window.TelegramGameProxy || {{
    receiveEvent: function() {{}},
    onEvent: function() {{}},
    shareScore: function() {{}},
  }};

  window.Telegram.WebApp = webapp;
}})();
"""
        self._ctx.eval(webapp_js)

    def eval(self, code: str, *, timeout_ms: int = 5000) -> JSEvalResult:
        """Execute JS code in the sandbox and return the result.

        Thread-safe. Returns JSEvalResult with .value or .error.
        """
        if self._closed or self._ctx is None:
            return JSEvalResult(error="Runtime is closed")

        with self._lock:
            t0 = time.monotonic()
            try:
                value = self._ctx.eval(code, timeout=timeout_ms)
                elapsed = (time.monotonic() - t0) * 1000
                return JSEvalResult(value=value, elapsed_ms=elapsed)
            except Exception as e:
                elapsed = (time.monotonic() - t0) * 1000
                err = str(e).strip() or repr(e)
                log.debug("JS eval error (%.1fms): %s", elapsed, err[:200])
                return JSEvalResult(error=err, elapsed_ms=elapsed)

    def eval_challenge(self, challenge_script: str, *, timeout_ms: int = 10000) -> JSEvalResult:
        """Execute a challenge/integrity script with extended timeout.

        Wraps the script in a try/catch to capture errors without crashing
        the sandbox.
        """
        wrapped = f"""
(function() {{
  try {{
    {challenge_script}
  }} catch(e) {{
    return "CHALLENGE_ERROR:" + e.message;
  }}
}})();
"""
        result = self.eval(wrapped, timeout_ms=timeout_ms)
        if result.ok and isinstance(result.value, str) and result.value.startswith("CHALLENGE_ERROR:"):
            return JSEvalResult(
                error=result.value[len("CHALLENGE_ERROR:"):],
                elapsed_ms=result.elapsed_ms,
            )
        return result

    def get_environment_snapshot(self) -> dict[str, Any]:
        """Extract key environment values for debugging/validation."""
        snapshot_js = """
(function() {
  return JSON.stringify({
    userAgent: navigator.userAgent,
    platform: navigator.platform,
    webdriver: navigator.webdriver,
    hardwareConcurrency: navigator.hardwareConcurrency,
    deviceMemory: navigator.deviceMemory,
    language: navigator.language,
    innerWidth: window.innerWidth,
    innerHeight: window.innerHeight,
    devicePixelRatio: window.devicePixelRatio,
    colorDepth: screen.colorDepth,
    tg_version: (window.Telegram && window.Telegram.WebApp) ? window.Telegram.WebApp.version : null,
    tg_platform: (window.Telegram && window.Telegram.WebApp) ? window.Telegram.WebApp.platform : null,
    tg_initData_length: (window.Telegram && window.Telegram.WebApp && window.Telegram.WebApp.initData) ? window.Telegram.WebApp.initData.length : 0,
  });
})();
"""
        result = self.eval(snapshot_js)
        if result.ok and isinstance(result.value, str):
            try:
                return json.loads(result.value)
            except json.JSONDecodeError:
                pass
        return {"error": result.error or "unknown"}

    def close(self) -> None:
        """Release V8 resources."""
        self._closed = True
        self._ctx = None

    @property
    def is_alive(self) -> bool:
        return not self._closed and self._ctx is not None


def build_js_runtime_for_session(
    *,
    user_agent: str,
    init_data: str,
    screen_width: int = 393,
    screen_height: int = 852,
) -> TelegramWebViewJSRuntime:
    """Convenience factory: create a runtime bound to a specific account session."""
    js_cfg = JSRuntimeConfig(
        user_agent=user_agent,
        screen_width=screen_width,
        screen_height=screen_height,
    )
    tg_cfg = TelegramWebAppConfig(
        init_data=init_data,
        platform="android",
    )
    return TelegramWebViewJSRuntime.create(js_config=js_cfg, tg_config=tg_cfg)
