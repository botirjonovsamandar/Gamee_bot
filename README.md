# GameeBot

Клиент для мини-приложения Gamee (Telegram): доска, награды, настройки в одном окне.

## Установка

Нужен **Python 3**. В каталоге с проектом лучше работать через локальное виртуальное окружение:

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python -m pip install --upgrade pip
.\.venv\Scripts\python -m pip install -r requirements.txt
.\.venv\Scripts\python main.py
```

`curl_cffi` должен быть свежим, иначе профили `chrome131+` не поддерживаются и Gamee-запросы падают на этапе impersonation.

При первом запуске программа сама создаст `config.yaml` и `accounts.yaml`, если их ещё нет. Дальше — настройки в интерфейсе (Telegram API, аккаунты, при необходимости бот для уведомлений).

Файлы `config.yaml.example` и `accounts.yaml.example` в репозитории — только справочно, как выглядят поля.

**Не выкладывайте в git** `config.yaml`, `accounts.yaml` и каталог `sessions/` — там локальные токены и сессии.
