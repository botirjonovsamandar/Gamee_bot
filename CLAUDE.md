# CLAUDE.md — Project Context & Rules

## Project Overview

**Gamee-Gold-Fest-Bot** is a Windows desktop + local web client for the Gamee Telegram Mini App. It manages multiple Telegram accounts from a PySide6 GUI or React/FastAPI panel and automates sync, daily rewards, season rewards, board moves and promo code entry.

Current UX is one-click full automation: the main button `Запустить всё` starts sync + claims + board moves for all accounts. `Остановить всё` stops the background worker. Manual account buttons still exist in code but are hidden in the main toolbar.

## Core Stack

| Layer | Technology |
|---|---|
| Language | Python 3.13+ |
| GUI | PySide6 / Qt 6 |
| Web UI | React / Vite / Tailwind |
| Web backend | FastAPI / Uvicorn / WebSocket |
| Telegram MTProto | Telethon |
| HTTP/TLS | curl_cffi with Android Chrome impersonation |
| JS Sandbox | py_mini_racer, optional inside `CurlCffiGameeTransport` |
| Config | PyYAML (`config.yaml`, `accounts.yaml`) |
| Notifications | Telegram Bot API via `requests` |
| Proxy | `httpx[socks]`, curl_cffi proxies |

## Current Architecture

```text
main.py -> PySide6 QApplication
  -> MainWindow (gamee_bot/ui/main_window.py)
     -> BotWorker (gamee_bot/worker.py)
        -> per-account thread
           -> GameeClient (gamee_bot/client.py)
              -> GameeTransport (gamee_bot/gamee_transport.py)
                 -> CurlCffiGameeTransport
                    -> curl_cffi.Session
                    -> optional TelegramWebViewJSRuntime
                    -> optional InputTelemetryGenerator
     -> EnterCodeThread for mass promo codes
     -> SettingsDialog / AddAccountDialog

web_main.py -> FastAPI app
  -> WebRuntime (gamee_bot/web/runtime.py)
     -> BotWorker / AccountActionThread / EnterCodeThread
     -> AppStateStore
        -> /api/state
        -> /ws/events
  -> React build from web/dist or Vite dev proxy
```

## Key Modules

| File | Responsibility |
|---|---|
| `gamee_bot/client.py` | Gamee JSON-RPC client: auth, assets, board moves, daily/season, promo codes |
| `gamee_bot/gamee_transport.py` | Transport abstraction, curl_cffi backend, `telegram_webview` fail-fast stub |
| `gamee_bot/http_profile.py` | Deterministic Android Chrome/WebView HTTP profile, Client Hints, header ordering, H2/TLS settings |
| `gamee_bot/worker.py` | Background supervisor, per-account threads, energy wait, full-auto sync/claim/play loop |
| `gamee_bot/daily_schedule.py` | Daily reward schedule helpers: UZ reset time, claim day keys and next reset |
| `gamee_bot/behavior_profile.py` | Per-account timing variance, budget variance, quiet-hours offsets, burst planning |
| `gamee_bot/input_telemetry.py` | Synthetic touch/scroll/tap telemetry used by the transport when available |
| `gamee_bot/js_runtime.py` | V8 sandbox wrapper and Telegram WebApp/browser polyfills |
| `gamee_bot/js_preload.js` | DOM/navigator/window/canvas/WebGL/Telegram.WebApp preload |
| `gamee_bot/telethon_bridge.py` | Telethon session management and Mini App `initData` retrieval |
| `gamee_bot/tma_auth.py` | Telegram Mini App `initData` parsing and structural validation |
| `gamee_bot/config.py` | YAML config loading, defaults, mode normalization, save helpers |
| `gamee_bot/account_store.py` | `accounts.yaml` CRUD |
| `gamee_bot/ui/main_window.py` | Main GUI, table, toolbar, bottom log, one-click worker start |
| `gamee_bot/ui/settings_dialog.py` | Credentials, compliance, transport and notification settings |
| `gamee_bot/cookie_storage.py` | Per-account cookie persistence for the curl_cffi transport |
| `gamee_bot/web/runtime.py` | FastAPI runtime state, worker lifecycle, account actions and mass promo endpoint |
| `web/src/App.tsx` | React dashboard, realtime account table, logs, controls and promo form |

## Build, Run & Check Commands

```powershell
# Setup
py -3 -m venv .venv
.\.venv\Scripts\python -m pip install --upgrade pip
.\.venv\Scripts\python -m pip install -r requirements.txt

# Run GUI
.\.venv\Scripts\python main.py

# Build and run local web UI
cd web
npm install
npm.cmd run build
cd ..
.\.venv\Scripts\python web_main.py

# Quick import check
.\.venv\Scripts\python -c "from gamee_bot.config import load_config; print('OK')"

# Verify curl_cffi version
.\.venv\Scripts\python -c "import curl_cffi; print(curl_cffi.__version__)"

# Verify HTTP profile
.\.venv\Scripts\python -c "from gamee_bot.http_profile import gamee_http_profile_for_label as p; h=p('test').ordered_api_headers(); print(p('test').impersonate, list(h.keys())[:5])"

# Syntax/import sanity check
.\.venv\Scripts\python -m compileall gamee_bot main.py
```

There is no maintained test suite. Validation is usually done by import/syntax checks, launching `main.py`, checking the GUI log, and monitoring real account stability.

## Coding Rules

- Use Python 3.13+ and `from __future__ import annotations` in Python modules.
- Type all function signatures; prefer `X | None` over `Optional[X]`.
- Use `dataclass(frozen=True, slots=True)` for immutable value objects.
- Business logic is synchronous/threaded. Telethon async calls are wrapped through `run_telethon_locked()` / `asyncio.run()` patterns.
- Preserve Russian UI strings and Russian comments unless explicitly asked to translate.
- Keep changes minimal and local. Do not rewrite architecture or introduce a new framework.
- Do not create `tests/` unless explicitly asked.
- Do not expose or print contents of `config.yaml`, `accounts.yaml` or `sessions/`.
- Never revert unrelated dirty worktree changes.

## Runtime Behavior

### One-Click Full Auto

- `MainWindow._start_worker()` forces `self._cfg.compliance.background_mode = BACKGROUND_MODE_FULL_AUTO` for the current run.
- The GUI button text is `Запустить всё` / `Остановить всё`.
- Hidden manual buttons are not the primary UX.
- There is no runtime duration cap; the worker runs until `Остановить всё` or process exit.
- Bottom log is part of the expected UX and should remain visible.

### Energy And Regen Wait

Current constants in `gamee_bot/worker.py`:

| Constant | Value |
|---|---|
| `ENERGY_COST_PER_MOVE` | `5` |
| `MIN_ENERGY_TO_PLAY_OPTIONS` | `(10, 15, 20)` |
| `ENERGY_REGEN_MINUTES` | `10` |
| `POST_NEXT_LIVE_POLL_SLACK_SEC` | `120` |
| `_REGEN_WAIT_JITTER_SEC` | `60.0` |

Behavior:

- The start threshold is deterministic per account label via `play_energy_threshold_for_label(label)`.
- The worker starts a play series only when energy is at least `10`, `15` or `20`, depending on the account.
- Once a series starts, it plays while energy is `>= 5` and limits allow it.
- When energy is below threshold, do not poll every few seconds.
- If `nextLiveAddedTimestamp` is available, sleep until the nearest `+1`, add missing lives at `10 minutes` each, then add `2-3 minutes` safety slack.
- If `nextLiveAddedTimestamp` is unavailable, fallback to `missing_lives * 10 minutes + 2-3 minutes`.

### Daily Reward Schedule

- Daily reward timing is centralized in `gamee_bot/daily_schedule.py`.
- The reset is `17:00 UZ` (`UTC+5`).
- Before reset, preview rows show waiting state and the worker logs the next availability once per account/day.
- After reset, `_apply_daily_checkin()` checks and claims daily during the normal account loop.
- Claimed daily state is keyed by the UZ claim day, so the worker does not repeatedly claim before the next reset.

### Promo Code Behavior

- Mass promo code uses `telegram.checkTask.code` through `GameeClient.submit_check_task_code()`.
- `taskId` must be positive; desktop validates via `QSpinBox`, web runtime validates before starting `EnterCodeThread`, and the client validates before building JSON-RPC params.
- Default `taskId` comes from `config.yaml -> gamee.check_task_id`; if absent, `load_config()` uses `2950`.
- JSON-RPC error details should preserve `code`, `reason`, `name` and `message` from `error.data`.
- `completed: false` must be treated as a failed promo response, never as `OK`.
- On JSON-RPC entity/session expiry, the client does one forced relogin and retries the promo once.

### Rate And Timing Constants

Current code values:

| Constant / Function | Value | File |
|---|---|---|
| `_HTTP_REQUEST_MAX_PARALLEL` | `12` | `client.py` |
| `_HTTP_REQUEST_START_GAP_SEC` | `0.12` | `client.py` |
| `_HTTP_REQUEST_START_JITTER_SEC` | `0.18` | `client.py` |
| `_account_stagger_delay()` | random `0-0.4s` | `worker.py` |
| `_supervisor_poll_delay()` | random `3-15s` | `worker.py` |
| `_ERROR_RETRY_IDLE_SEC` | `5.0` | `worker.py` |
| `_SEASON_API_MAX_PARALLEL` | `5` | `worker.py` |
| `_SEASON_SYNC_MIN_INTERVAL_SEC` | `45.0` | `worker.py` |
| `_IDLE_JITTER_SEC` | `3.0` | `worker.py` |
| `_CACHE_TTL_SEC` | `45 * 60` | `telethon_bridge.py` |

## Transport Rules

- Working backend: `curl_cffi_raw_http`.
- `telegram_webview` is a reserved/future backend and must fail fast with a clear message.
- Backend is selected by `config.yaml -> gamee.transport_backend` and the settings dialog.
- `GameeClient` must use `GameeTransport`; do not call curl_cffi directly from new business logic.
- `CurlCffiGameeTransport` owns the curl session, optional V8 runtime, optional telemetry generator and cookie persistence.

## HTTP/TLS Profile Rules

- Use Android curl_cffi impersonation profiles only (`*_android`). Never introduce desktop profiles.
- Keep Android WebView User-Agent format:
  ```text
  Mozilla/5.0 (Linux; Android 14; K) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/{major}.0.0.0 Mobile Safari/537.36
  ```
- API and navigation headers must be built through `GameeHttpClientProfile.ordered_api_headers()` and `ordered_navigation_headers()`.
- Do not construct ad-hoc API header dicts inline.
- Client Hints are mandatory for API calls: `sec-ch-ua`, `sec-ch-ua-full-version`, `sec-ch-ua-full-version-list`, `sec-ch-ua-mobile: ?1`, `sec-ch-ua-platform: "Android"`.
- `X-Requested-With: org.telegram.messenger` is required on API/navigation requests.
- `x-bot-header: gamee` is required on API POST requests; missing it can make `loginUsingTelegram` return `-32603 Server error`.
- Do not add bot-specific User-Agent strings or unrelated custom automation markers.
- Header/browser profiles are deterministic per account label via `gamee_http_profile_for_label(label)`.
- HTTP/2 settings and ExtraFingerprints live in `http_profile.py`; keep them centralized there.
- Keep JSON-RPC request `id` values equal to method names; Gamee rejects randomized IDs with `-32700 Parse error: Invalid [id] format`.

## Telethon And Credentials

- Telethon credentials live in `config.yaml -> telethon`.
- `ensure_config_file()` currently writes public Telegram Android defaults on first config creation.
- Users can override `api_id` and `api_hash` through settings.
- `load_config()` requires non-empty credentials; if a config has `api_id=0` or empty hash, it raises a clear error.
- `RequestAppWebViewRequest` platform should remain Android unless explicitly changing the Telegram Mini App strategy.

## State Files

- `config.yaml`: local settings, credentials and notification config.
- `accounts.yaml`: account labels, initData/session paths, proxy refs and account metadata.
- `sessions/`: Telethon SQLite session files.
- `.env*`, `.session*`, `.sqlite`, `.db`, `.har`, `.log`, `logs/`, `tmp/`, `temp/`: local secrets, captures or generated diagnostics.
- These files are local state/secrets and are intentionally ignored by git.

## Documentation Rules

- Keep `README.md`, `CLAUDE.md` and `config.yaml.example` consistent when changing user-visible behavior or config keys.
- If constants change, update the tables above and any matching README sections.
- Do not document intended behavior that the code does not currently implement.
