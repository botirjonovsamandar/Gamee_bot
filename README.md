# Gamee Gold Fest Bot

## Web UI (React)

Локальная web-панель работает поверх того же Python-бота: backend остаётся в Python, а браузер получает таблицу, статусы и логи через WebSocket.

Первый запуск / сборка web UI:

```powershell
cd web
npm install
npm run build
cd ..
python web_main.py
```

Если PowerShell блокирует `npm.ps1`, используй:

```powershell
npm.cmd run build
```

Открыть:

```text
http://127.0.0.1:8000
```

Development mode:

Terminal 1:

```powershell
python web_main.py
```

Terminal 2:

```powershell
cd web
npm run dev
```

В dev-режиме Vite открывается отдельно:

```text
http://127.0.0.1:5173
```

Backend остаётся на `http://127.0.0.1:8000`; Vite проксирует `/api` и `/ws` к нему.

В web UI есть:

- realtime таблица аккаунтов;
- realtime логи в отдельном прокручиваемом окне;
- фильтры логов;
- переключатель белого/чёрного фона с сохранением выбора в браузере;
- start/stop фона, ручные действия по аккаунту, прокси, удаление аккаунта и массовый промокод.

Desktop-клиент для Telegram Mini App `Gamee`: синхронизация аккаунтов, ходы по доске, ежедневные награды, сезонные награды, промокоды и лог событий в одном окне.

Проект рассчитан на Windows и сейчас имеет два интерфейса: desktop UI на PySide6 и локальный web UI на React/FastAPI. Оба используют одну и ту же бизнес-логику Gamee/Telegram.

## Что Делает Софт

- авторизует Telegram-аккаунты через Telethon;
- получает `tgWebAppData` / `initData` для Mini App `gamee`;
- логинится в Gamee через `user.authentication.loginUsingTelegram`;
- синхронизирует состояние аккаунта (`user.getAssets`, `luckyGame.board.get`);
- автоматически забирает ежедневные награды после дневного reset по времени UZ;
- автоматически забирает сезонные награды;
- выполняет ходы по доске, пока хватает энергии и не достигнуты лимиты;
- умеет массово отправлять промокод `telegram.checkTask.code` на все аккаунты и показывает точную причину отказа API;
- показывает статус, энергию, золото, сезонку и живой лог в desktop/web UI;
- в web UI обновляет данные в реальном времени через WebSocket.

## Что Под Капотом

### Стек

| Слой | Технология | Назначение |
|---|---|---|
| Desktop UI | PySide6 / Qt 6 | Главное окно, таблица аккаунтов, диалоги настроек |
| Web UI | React / Vite / Tailwind | Браузерная панель, realtime таблица, логи, actions |
| Web backend | FastAPI / Uvicorn / WebSocket | `/api/*`, `/ws/events`, отдача React build |
| Telegram MTProto | Telethon | Вход по телефону, хранение `.session`, запрос WebView для Mini App |
| HTTP/TLS | curl_cffi | Запросы к `api2.gamee.com`, cookie jar, Android Chrome impersonation |
| Конфиг | PyYAML | `config.yaml`, `accounts.yaml` |
| Уведомления | requests | Отправка сообщений через Telegram Bot API |
| JS sandbox | py_mini_racer | Опциональный V8 runtime внутри `CurlCffiGameeTransport` для WebView-полифиллов и challenge scripts |

### Важные Модули

| Файл | Назначение |
|---|---|
| `gamee_bot/client.py` | Gamee JSON-RPC клиент: login, assets, board, daily, season, promo code |
| `gamee_bot/worker.py` | Фоновый supervisor и per-account loop |
| `gamee_bot/daily_schedule.py` | Расписание daily reward: reset в `17:00 UZ` и ключ дня клейма |
| `gamee_bot/gamee_transport.py` | Transport abstraction и curl_cffi backend |
| `gamee_bot/web/runtime.py` | FastAPI runtime, REST actions и web-потоки |

### Архитектура

```text
main.py
  -> MainWindow
     -> BotWorker
        -> per-account thread
           -> GameeClient
              -> CurlCffiGameeTransport
                 -> curl_cffi.Session
                 -> optional TelegramWebViewJSRuntime
                 -> optional InputTelemetryGenerator
                 -> cookie_storage

MainWindow
  -> SettingsDialog
  -> AddAccountDialog
  -> EnterCodeDialog / EnterCodeThread

web_main.py
  -> FastAPI app
     -> WebRuntime
        -> BotWorker
        -> AccountActionThread / EnterCodeThread
     -> AppStateStore
        -> /api/state
        -> /ws/events

web/
  -> React UI
     -> REST actions
     -> WebSocket live updates
```

### Как Идёт Работа По Одному Аккаунту

1. Telethon получает `RequestAppWebViewRequest` для `gamee/start`.
2. Из URL извлекается `tgWebAppData`.
3. `tma_auth.py` проверяет, что строка похожа на валидный `initData`.
4. `GameeClient` делает warmup страницы `https://prizes.gamee.com/`.
5. Затем отправляет JSON-RPC batch в `https://api2.gamee.com/`.
6. После логина получает JWT-токен и использует его для дальнейших вызовов.
7. В фоне поток аккаунта циклически делает sync, claim и play.

### Какие API Методы Используются

- `app.telegram.get`
- `user.authentication.loginUsingTelegram`
- `user.getAssets`
- `luckyGame.board.get`
- `luckyGame.board.play`
- `dailyCheckin.getInformation`
- `dailyCheckin.claim`
- `rewardedProgress.getAll`
- `rewardedProgress.claim`
- `telegram.checkTask.code`
- `user.linkTelegramReferral`

### Что Важно Про Сеть

- транспорт по умолчанию: `curl_cffi_raw_http`;
- backend выбирается в `config.yaml` и в `Настройки -> Режим и лимиты -> Transport backend`;
- рабочий вариант сейчас только `curl_cffi_raw_http`;
- `telegram_webview` присутствует как каркас и в GUI помечен как недоступный;
- API и navigation headers строятся через `gamee_http_profile_for_label(...)`, а не inline dict;
- каждый аккаунт получает стабильный Android Chrome/WebView profile по своему `label`;
- `X-Requested-With: org.telegram.messenger`, `x-bot-header: gamee`, mobile User-Agent и Client Hints отправляются через профиль;
- для корректной работы нужен `curl_cffi >= 0.15.0`.

### Как Хранится Состояние

- база данных не используется;
- настройки лежат в `config.yaml`;
- список аккаунтов лежит в `accounts.yaml`;
- Telethon-сессии лежат в папке `sessions/` рядом с `accounts.yaml`;
- `initData` кэшируется в памяти, чтобы не дёргать Telegram без нужды.

## Требования

- Windows;
- Python 3.13+;
- Node.js/npm для сборки `web/`;
- доступ в интернет до Telegram и `api2.gamee.com`;
- установленный `curl_cffi >= 0.15.0`.

## Установка

Рекомендуемый вариант через виртуальное окружение:

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python -m pip install --upgrade pip
.\.venv\Scripts\python -m pip install -r requirements.txt
```

Если запускаешь без `.venv`, убедись, что зависимости ставятся именно в тот Python, которым потом стартует приложение:

```powershell
py -3 -m pip install --upgrade pip
py -3 -m pip install -r requirements.txt
```

Проверить версию `curl_cffi` можно так:

```powershell
py -3 -c "import curl_cffi; print(curl_cffi.__version__)"
```

## Первый Запуск Desktop UI

```powershell
py -3 main.py
```

или:

```powershell
.\.venv\Scripts\python main.py
```

При первом запуске приложение автоматически создаст:

- `config.yaml`
- `accounts.yaml`

## Первый Запуск Web UI

Установи Python-зависимости:

```powershell
.\.venv\Scripts\python -m pip install -r requirements.txt
```

Собери React UI:

```powershell
cd web
npm install
npm.cmd run build
cd ..
```

Запусти локальную web-панель:

```powershell
.\.venv\Scripts\python web_main.py
```

Открой:

```text
http://127.0.0.1:8000
```

## Основные Файлы

### `config.yaml`

Основные секции:

- `gamee`: адрес API, transport backend, currency ids и делители;
- `telethon`: `api_id`, `api_hash`, глобальная реф-ссылка Gamee, `telegram_referral_ref`;
- `compliance`: лимиты, quiet hours, budget на ходы, cooldown после ошибок;
- `telegram`: bot token, chat id и параметры уведомлений;
- `paths`: путь до `accounts.yaml`;
- `ui`: заголовок окна.

Минимально важный пример:

```yaml
gamee:
  api_url: https://api2.gamee.com/
  transport_backend: curl_cffi_raw_http

telethon:
  api_id: 12345678
  api_hash: "your_api_hash"
  gamee_ref: null
  telegram_referral_ref: null

compliance:
  background_mode: full_auto
  session_duration_minutes: 0  # без лимита длительности
  quiet_hours_enabled: false
  quiet_hours_start_hour: 0
  quiet_hours_end_hour: 8
  daily_move_budget: 0  # 0 = без дневного лимита ходов
  max_moves_per_session: 8
  fast_bootstrap_enabled: true
  bootstrap_account_stagger_min_seconds: 0.1
  bootstrap_account_stagger_max_seconds: 0.4
  bootstrap_move_delay_min_seconds: 6.0
  bootstrap_move_delay_max_seconds: 7.5
  steady_energy_targets: [10, 15, 20]
  error_cooldown_seconds: 30
  stop_after_error_streak: 3

telegram:
  bot_token: ""
  chat_id: ""
  notify_on_move: true
  notify_on_daily_claim: true
  notify_on_season_claim: true
  summary_interval_seconds: 3600
```

### `accounts.yaml`

Файл создаётся и поддерживается через GUI. Для каждой записи используются поля:

- `label`: имя аккаунта в таблице;
- `telethon_session`: путь к `.session`, если аккаунт добавлен через вход по телефону;
- `init_data`: готовая строка, если аккаунт добавлен вручную;
- `install_uuid`: UUID установки;
- `proxy_url`: прокси для Gamee API;
- `gamee_ref`: реферальная ссылка Gamee;
- `telegram_referral_ref`: numeric user id для `user.linkTelegramReferral`;
- `gamee_preexisting`: служебный флаг, что аккаунт уже был зарегистрирован в Gamee.

### `sessions/`

Каталог с Telethon `.session` файлами. Для аккаунтов, добавленных через телефон, создаётся отдельная сессия на каждый label.

## Настройка Перед Работой

Открой `Настройки...` в верхнем меню.

### Вкладка `Общие`

Тут задаются:

- `api_id` и `api_hash` Telegram;
- глобальная Gamee реф-ссылка;
- optional `User ID в Telegram` для реферальной привязки.

Без `api_id` и `api_hash` нельзя добавлять аккаунты через телефон и нельзя запускать фоновую работу.

### Вкладка `Режим и лимиты`

Тут находятся:

- `Transport backend`;
- `Фоновый режим`;
- дневной бюджет ходов;
- максимум ходов за одну сессию;
- быстрый первый проход;
- stagger запуска аккаунтов;
- пауза между ходами в bootstrap;
- steady energy targets (`10`, `15`, `20` и т.п.);
- cooldown после ошибок;
- stop-after-error-streak;
- quiet hours;
- подтверждение массового промокода;
- подтверждение ручной серии ходов.

Важно:

- в текущем GUI кнопка `Запустить всё` в главном окне запускает полный автомат для текущей сессии;
- то есть по факту стартует sync + награды + ходы для всех аккаунтов;
- лимита по длительности фоновой сессии больше нет: фон работает до `Остановить всё`;
- сохранённый `background_mode` всё равно остаётся в конфиге и используется как часть общих лимитов/настроек.
- в bootstrap ходы идут до `energy < 5`, без дневного бюджета и без длинных burst-пауз;
- после каждого броска софт ждёт анимацию кубика `6-7.5с`, а если выпала награда/коробка, добавляет ещё `3-4.5с`.

### Вкладка `Уведомления`

Можно включить Telegram-уведомления о:

- ходах;
- ежедневных наградах;
- сезонных наградах;
- периодической сводке.

Там же есть кнопка тестовой отправки сообщения.

## Как Добавить Аккаунт

Открой `Добавить аккаунт...` в верхнем меню.

Есть два режима добавления.

### 1. Вход По Телефону

Шаги:

1. Введи название аккаунта.
2. Введи номер телефона.
3. При желании укажи:
   - Gamee реф-ссылку;
   - `User ID в Telegram`;
   - прокси для Gamee API.
4. Нажми `Отправить код`.
5. На втором шаге введи код из Telegram.
6. Если включён 2FA, введи пароль.
7. Нажми `Войти и сохранить`.

Что произойдёт:

- Telethon создаст/обновит `.session` файл;
- приложение сразу запросит `initData` для Mini App Gamee;
- аккаунт будет записан в `accounts.yaml`.

### 2. Готовая Строка

Если у тебя уже есть `initData` / `tgWebAppData`:

1. Выбери вкладку `Готовая строка`.
2. Укажи название аккаунта.
3. Вставь строку целиком.
4. При необходимости укажи прокси.
5. Нажми `Сохранить аккаунт`.

Программа сама:

- попытается вытащить `tgWebAppData` из URL, если ты вставил не чистую строку, а ссылку;
- проверит базовую структуру `user=...&hash=...`;
- запишет аккаунт в `accounts.yaml`.

## Как Пользоваться Софтом

### Desktop UI

В окне есть:

- таблица аккаунтов;
- кнопка `Запустить всё`;
- кнопка `Остановить всё`;
- кнопка `Ввести код`;
- кнопка `Удалить выбранный...`;
- кнопка `Прокси выбранного...`;
- нижний лог событий.

### Web UI

В браузере есть:

- кнопки `Запустить всё` и `Остановить всё`;
- переключатель `Белый фон` / `Чёрный фон`;
- realtime таблица аккаунтов;
- отдельное окно логов с внутренним скроллом;
- фильтр по аккаунтам/статусу;
- фильтры логов: все, info, ходы, daily, season, ошибки, fatal;
- drawer выбранного аккаунта: sync, claim daily, play session, proxy, delete;
- массовая отправка промокода.

### Таблица Аккаунтов

Колонки показывают:

- аккаунт;
- прокси;
- энергия;
- золото;
- статус;
- последний ход;
- ежедневная награда;
- сезонные награды;
- заработано за текущую сессию приложения.

### Кнопка `Запустить Всё`

Это основной сценарий работы.

После нажатия приложение:

- запускает фоновый worker;
- поднимает поток на каждый аккаунт;
- логинит аккаунты в Gamee при необходимости;
- синхронизирует состояние;
- забирает daily reward, когда наступил reset `17:00 UZ`;
- проверяет сезонные награды;
- делает ходы, пока есть энергия и позволяют лимиты;
- пишет действия в лог текущего интерфейса.

### Кнопка `Остановить Всё`

Полностью останавливает фоновую работу. Уже начатый сетевой запрос может завершиться, после чего поток аккаунта остановится.

### Кнопка `Ввести Код`

Открывает диалог промокода. Код отправляется последовательно на все аккаунты через `telegram.checkTask.code`.

Для промокода важны два значения:

- сам код;
- `taskId` задания Gamee.

Desktop-диалог берёт `taskId` по умолчанию из `gamee.check_task_id` в `config.yaml`. Если ключа нет, используется дефолт `2950`. В desktop UI `taskId` можно сменить прямо в диалоге, а в web UI — во втором поле блока `Промокод всем`.

Если Gamee меняет активное задание, старый `taskId` может дать отказ даже при правильном коде. В логе теперь показываются `code`, `reason` и другие детали JSON-RPC ошибки, а ответ `completed: false` считается отказом, а не успешным `OK`.

### Кнопка `Прокси Выбранного...`

Позволяет сменить или убрать прокси только для выбранного аккаунта.

### Кнопка `Удалить Выбранный...`

Удаляет запись аккаунта из `accounts.yaml` и очищает связанные session-файлы, если они есть.

## Прокси

Прокси используется только для запросов к Gamee API. Форматы, которые ожидает GUI:

- `host:port:user:pass`
- `user:pass@host:port`
- `http://user:pass@host:port`
- `socks5://user:pass@host:port`

В диалоге добавления аккаунта и в редактировании прокси есть кнопка `✓` для быстрой проверки.

## Логи

Desktop UI показывает лог в нижней панели. Web UI показывает лог в отдельном фиксированном окне: страница не растягивается от большого количества строк, список логов прокручивается внутри блока.

Лог показывает:

- старт и остановку фоновой сессии;
- успешную синхронизацию аккаунтов;
- каждую ежедневную награду;
- сезонные клеймы;
- каждый ход по доске;
- ошибки логина, прокси и API.

Если что-то пошло не так, первым делом смотри именно туда.

## Как Работает Автоматизация В Фоне

По каждому аккаунту worker делает примерно такой цикл:

1. берёт запись из `accounts.yaml`;
2. строит `GameeSession`;
3. логинится через `loginUsingTelegram`, если токен истёк;
4. получает текущее состояние аккаунта;
5. забирает daily reward, если она доступна;
6. забирает сезонные награды;
7. делает серию ходов, пока хватает энергии;
8. после броска ждёт анимацию кубика и, при награде/коробке, дополнительную анимацию результата;
9. пишет статус в таблицу и в лог;
10. уходит в sleep до расчётного регена энергии или в короткий retry после ошибки.

Скорость регулируется:

- ограничителем параллельных HTTP-запросов в `GameeClient`;
- небольшими паузами между стартом потоков;
- cooldown после серий ошибок;
- quiet hours;
- дневным лимитом ходов и лимитом ходов за сессию;
- отдельной bootstrap-паузой после броска, чтобы не пропускать визуальную анимацию результата.

### Порог Энергии И Сон До Регена

Фоновый worker не опрашивает API каждые несколько секунд, когда энергии мало.

Текущая логика:

- стоимость одного хода: `5` энергии;
- при каждом `Запустить всё` сначала идёт быстрый первый проход;
- в bootstrap аккаунт быстро делает sync/claim/play и сливает доступную энергию до `energy < 5`;
- после bootstrap аккаунт выбирает следующий steady target из `steady_energy_targets`, обычно `10`, `15` или `20`;
- после старта серии ходы идут до конца, пока энергия остаётся `>= 5`;
- если сервер отдаёт `nextLiveAddedTimestamp`, он используется как время ближайшего `+1` энергии;
- недостающая энергия сверх ближайшего `+1` считается как `10 минут` за каждую единицу;
- после расчётного момента добавляется безопасный запас `2-3 минуты`;
- если `nextLiveAddedTimestamp` нет, используется fallback: `missing_lives * 10 минут + 2-3 минуты`.

Это снижает лишний polling и даёт аккаунту просыпаться примерно тогда, когда энергии уже достаточно для выбранного порога.

### Daily Reward И Reset В 17:00 UZ

Daily reward не дёргается весь день подряд. В `gamee_bot/daily_schedule.py` задан reset `17:00` по часовому поясу Uzbekistan (`UTC+5`).

Поведение worker:

- до `17:00 UZ` строка аккаунта показывает ожидание reset;
- в лог один раз на аккаунт пишется, когда daily reward станет доступна;
- после `17:00 UZ` проверка и клейм daily идут в обычном account loop до проверки порога энергии;
- energy sleep не перескакивает через reset: аккаунт просыпается к `17:00 UZ`;
- `claimedToday` из snapshot не закрывает день сам по себе: бот пробует `dailyCheckin.claim` напрямую и запоминает день только после успешного claim.

## Уведомления В Telegram

Если заполнены `bot_token` и `chat_id`, приложение может отправлять:

- сообщения о каждом ходе;
- сообщения о daily reward;
- сообщения о сезонных наградах;
- периодическую сводку по аккаунтам.

Нотификации настраиваются во вкладке `Уведомления`.

## Полезные Команды

Установка Python-зависимостей:

```powershell
py -3 -m pip install -r requirements.txt
```

Запуск desktop UI:

```powershell
py -3 main.py
```

Сборка web UI:

```powershell
cd web
npm install
npm.cmd run build
cd ..
```

Запуск web UI:

```powershell
py -3 web_main.py
```

Проверить, что `curl_cffi` достаточно новый:

```powershell
py -3 -c "import curl_cffi; print(curl_cffi.__version__)"
```

Проверить git identity перед `commit`/`push`:

```powershell
git config --global user.name
git config --global user.email
```

## Типичные Проблемы

### `loginUsingTelegram: сервер отклонил запрос (code=-32603...)`

Проверь:

- что установлен `curl_cffi >= 0.15.0`;
- что используется рабочий `transport_backend: curl_cffi_raw_http`;
- что аккаунт может получить валидный `initData` через Telethon;
- что нет сломанного прокси.

### `Parse error: Invalid [id] format`

Gamee валидирует JSON-RPC `id`. В рабочих запросах `id` должен оставаться равным имени метода, например `user.authentication.loginUsingTelegram`. Нельзя рандомизировать `id` в payload.

### Промокод Не Применяется

Проверь:

- правильный ли `taskId` указан для текущего промокода;
- не остался ли дефолт `gamee.check_task_id: 2950`, если Gamee выдал новое задание;
- не был ли код уже использован на этом аккаунте;
- нет ли в логе JSON-RPC ошибки вроде invalid code, expired entity или already claimed.

При JSON-RPC признаке протухшей сессии клиент делает один fresh login и повторяет `telegram.checkTask.code`. Если после этого в логе остаётся отказ, проблема обычно в самом коде, `taskId` или статусе задания.

### Не Получается Отправить Код В Telegram При Добавлении Аккаунта

Проверь:

- верные `api_id` и `api_hash`;
- номер телефона с кодом страны;
- отсутствие проблем с сетью до Telegram.

### Аккаунт Стоит В Ошибке Или Сильно Тормозит

Проверь:

- лог текущего UI;
- корректность прокси;
- quiet hours;
- лимиты `daily_move_budget`, `max_moves_per_session`;
- нет ли частых ошибок авторизации или сетевых блокировок.

## Безопасность

Не коммить в git:

- `config.yaml`
- `accounts.yaml`
- папку `sessions/`
- локальные `.env*`, `.session`, `.sqlite`, `.db`, `.har` и `.log` файлы

В этих файлах лежат локальные сессии, прокси и чувствительные данные аккаунтов.

## Кратко По Ежедневному Сценарию

1. Установи зависимости.
2. Запусти `main.py` для desktop UI или `web_main.py` для web UI.
3. Открой `Настройки...` и впиши `api_id` / `api_hash`.
4. Добавь аккаунты через `Добавить аккаунт...`.
5. При необходимости проверь прокси.
6. Нажми `Запустить всё`.
7. Следи за таблицей и нижним логом.
8. Для промокода используй `Ввести код` и проверь актуальный `taskId`.
9. Для остановки нажми `Остановить всё`.
