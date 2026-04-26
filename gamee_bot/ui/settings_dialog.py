from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt

from datetime import datetime

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from gamee_bot.config import (
    BACKGROUND_MODE_FULL_AUTO,
    BACKGROUND_MODE_MANUAL_ONLY,
    BACKGROUND_MODE_READ_ONLY,
    background_mode_label,
    read_full_config_yaml,
    save_config_sections,
)
from gamee_bot.gamee_transport import (
    GAMEE_TRANSPORT_BACKEND_CURL_CFFI_RAW_HTTP,
    GAMEE_TRANSPORT_BACKEND_TELEGRAM_WEBVIEW,
    gamee_transport_backend_blocker_message,
    normalize_gamee_transport_backend,
)
from gamee_bot.notify import TelegramNotifier
from gamee_bot.telegram_messages import (
    format_board_move_message,
    format_daily_claim_message,
    format_season_claim_message,
    format_summary_message,
)


class SettingsDialog(QDialog):
    """Настройки без правки YAML вручную."""

    def __init__(self, config_path: Path, parent=None) -> None:
        super().__init__(parent)
        self._config_path = config_path.resolve()
        self.setWindowTitle("Настройки")
        self.resize(580, 540)

        self._raw = read_full_config_yaml(self._config_path)

        tabs = QTabWidget()
        tabs.addTab(self._tab_general(), "Общие")
        tabs.addTab(self._tab_compliance(), "Режим и лимиты")
        tabs.addTab(self._tab_notify(), "Уведомления")

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._on_save)
        buttons.rejected.connect(self.reject)

        root = QVBoxLayout(self)
        root.addWidget(tabs)
        root.addWidget(buttons)

    def _small_hint(self, text: str) -> QLabel:
        lab = QLabel(text)
        lab.setWordWrap(True)
        lab.setObjectName("hintLabel")
        return lab

    def _settings_card(self, title: str) -> QGroupBox:
        box = QGroupBox(title)
        box.setObjectName("settingsCard")
        return box

    def _tab_general(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setSpacing(0)
        lay.setContentsMargins(4, 8, 4, 8)

        lead = QLabel("Подключение")
        lead.setObjectName("settingsLead")
        sub = QLabel("API Telegram (обязательно) и рефка Gamee, если нужна.")
        sub.setObjectName("settingsMicro")
        sub.setWordWrap(True)
        lay.addWidget(lead)
        lay.addWidget(sub)

        api_box = self._settings_card("Telegram API")
        api_l = QVBoxLayout(api_box)
        api_l.setSpacing(10)
        hint_api = QLabel(
            '<span style="line-height:1.5">Один раз на всё приложение. Создайте приложение на '
            '<a href="https://my.telegram.org/auth" style="color:#8ab4f8">my.telegram.org</a> → '
            "<b>API development tools</b> и скопируйте два значения ниже.</span>"
        )
        hint_api.setOpenExternalLinks(True)
        hint_api.setWordWrap(True)
        hint_api.setObjectName("hintLabel")
        api_l.addWidget(hint_api)
        api_l.addWidget(
            self._small_hint(
                "Telethon эмулирует профиль реального Android-устройства (Samsung, Xiaomi, Pixel и др.) "
                "для совместимости с протоколом Telegram Mini Apps."
            )
        )

        form = QFormLayout()
        form.setSpacing(12)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        form.setHorizontalSpacing(14)
        self._th_api_id = QLineEdit()
        self._th_api_id.setPlaceholderText("Число, например 12345678")
        self._th_api_hash = QLineEdit()
        self._th_api_hash.setPlaceholderText("Строка из пару десятков символов")
        form.addRow("App api_id:", self._th_api_id)
        form.addRow("App api_hash:", self._th_api_hash)
        api_l.addLayout(form)
        lay.addWidget(api_box)

        ref_box = self._settings_card("Рефка Gamee")
        ref_l = QVBoxLayout(ref_box)
        ref_l.setSpacing(10)
        ref_l.addWidget(
            self._small_hint(
                "Реф с "
                '<a href="https://t.me/gamee/start">t.me/gamee/start</a> '
                "— целиком ссылку или пусто."
            )
        )
        self._th_gamee_ref = QLineEdit()
        self._th_gamee_ref.setPlaceholderText("Ссылка с рефом или пусто")
        ref_l.addWidget(self._th_gamee_ref)
        ref_l.addWidget(
            self._small_hint(
                "<b>User ID в Telegram</b> — только цифры. Необязательно."
            )
        )
        self._th_telegram_referral_ref = QLineEdit()
        self._th_telegram_referral_ref.setPlaceholderText("Только цифры или пусто")
        ref_l.addWidget(self._th_telegram_referral_ref)
        lay.addWidget(ref_box)

        lay.addStretch()

        th = self._raw.get("telethon") or {}
        api_id_raw = th.get("api_id", 0)
        api_hash_raw = str(th.get("api_hash", "") or "")
        self._th_api_id.setText(str(int(api_id_raw)) if api_id_raw else "")
        self._th_api_hash.setText(api_hash_raw)
        ref = th.get("gamee_ref")
        if ref is None:
            ref = th.get("mini_app_start_param")
        self._th_gamee_ref.setText(str(ref).strip() if ref else "")
        tr = th.get("telegram_referral_ref")
        self._th_telegram_referral_ref.setText(
            str(int(tr)) if tr is not None and str(tr).strip() else ""
        )
        return w

    def _tab_compliance(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setSpacing(10)
        lay.setContentsMargins(4, 8, 4, 8)

        lead = QLabel("Manual-first и guardrails")
        lead.setObjectName("settingsLead")
        lay.addWidget(lead)
        lay.addWidget(
            self._small_hint(
                "Raw HTTP + TLS impersonation не равны реальному WebView/браузеру. Поэтому фон ограничен чтением: "
                "чувствительные write-операции остаются только ручными и по явной команде пользователя."
            )
        )

        comp = self._raw.get("compliance") or {}
        gamee = self._raw.get("gamee") or {}
        backend_raw = str(gamee.get("transport_backend", "") or "")
        backend = normalize_gamee_transport_backend(backend_raw)
        backend_blocker = gamee_transport_backend_blocker_message(backend_raw)

        backend_box = self._settings_card("Transport backend")
        backend_l = QVBoxLayout(backend_box)
        backend_l.addWidget(
            self._small_hint(
                "Сетевой backend выбирается явно через config и влияет на запуск всех сетевых действий."
            )
        )
        self._transport_backend = QComboBox()
        self._transport_backend.addItem(
            "curl_cffi_raw_http (доступен)",
            GAMEE_TRANSPORT_BACKEND_CURL_CFFI_RAW_HTTP,
        )
        self._transport_backend.addItem(
            "telegram_webview (stub, пока недоступен)",
            GAMEE_TRANSPORT_BACKEND_TELEGRAM_WEBVIEW,
        )
        backend_idx = max(0, self._transport_backend.findData(backend))
        self._transport_backend.setCurrentIndex(backend_idx)
        backend_l.addWidget(self._transport_backend)
        lay.addWidget(backend_box)

        mode_box = self._settings_card("Фоновый режим")
        mode_l = QVBoxLayout(mode_box)
        mode_l.addWidget(
            self._small_hint(
                "Фон разрешён только для чтения состояния. Клеймы, промокоды и игровые сессии не переводятся в скрытую автоматизацию."
            )
        )
        self._bg_mode = QComboBox()
        self._bg_mode.addItem(background_mode_label(BACKGROUND_MODE_MANUAL_ONLY), BACKGROUND_MODE_MANUAL_ONLY)
        self._bg_mode.addItem(background_mode_label(BACKGROUND_MODE_READ_ONLY), BACKGROUND_MODE_READ_ONLY)
        self._bg_mode.addItem(background_mode_label(BACKGROUND_MODE_FULL_AUTO), BACKGROUND_MODE_FULL_AUTO)
        want_mode = str(comp.get("background_mode", BACKGROUND_MODE_MANUAL_ONLY) or "")
        idx = max(0, self._bg_mode.findData(want_mode))
        self._bg_mode.setCurrentIndex(idx)
        mode_l.addWidget(self._bg_mode)
        lay.addWidget(mode_box)

        markers_box = self._settings_card("Явные маркеры клиента")
        markers_l = QVBoxLayout(markers_box)
        markers_l.addWidget(
            self._small_hint(
                "Сетевой слой использует заголовки, стандартные для Telegram Android WebView (<code>X-Requested-With: org.telegram.messenger</code>, мобильный User-Agent)."
            )
        )
        markers_l.addWidget(
            self._small_hint(
                "Telethon-клиент использует профиль реального устройства через <code>device_model/app_version</code> "
                "для полного паритета с официальным Android-клиентом Telegram."
            )
        )
        markers_l.addWidget(
            self._small_hint(
                "Текущий стек — Telethon + raw HTTP. Это не настоящий Telegram WebView/браузер, поэтому browser-only проверки, fingerprint среды и input-telemetry здесь не эмулируются."
            )
        )
        if backend_blocker:
            markers_l.addWidget(
                self._small_hint(
                    f"Выбран transport backend <code>{backend}</code>, но он недоступен: {backend_blocker}"
                )
            )
        else:
            markers_l.addWidget(
                self._small_hint(
                    f"Активный transport backend: <code>{backend}</code>"
                )
            )
        lay.addWidget(markers_box)

        limits_box = self._settings_card("Лимиты активности")
        limits_form = QFormLayout(limits_box)
        limits_form.setSpacing(12)
        limits_form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        self._daily_move_budget = QSpinBox()
        self._daily_move_budget.setRange(0, 1000)
        self._daily_move_budget.setSpecialValueText("без лимита")
        self._daily_move_budget.setValue(max(0, int(comp.get("daily_move_budget", 0))))

        self._max_moves_session = QSpinBox()
        self._max_moves_session.setRange(1, 200)
        self._max_moves_session.setValue(max(1, int(comp.get("max_moves_per_session", 8))))

        self._error_cooldown = QSpinBox()
        self._error_cooldown.setRange(5, 3600)
        self._error_cooldown.setSuffix(" сек")
        self._error_cooldown.setValue(max(5, int(comp.get("error_cooldown_seconds", 30))))

        self._stop_after_error_streak = QSpinBox()
        self._stop_after_error_streak.setRange(1, 20)
        self._stop_after_error_streak.setValue(max(1, int(comp.get("stop_after_error_streak", 3))))

        limits_form.addRow("Дневной бюджет ходов:", self._daily_move_budget)
        limits_form.addRow("Ходов за ручную сессию:", self._max_moves_session)
        limits_form.addRow("Cooldown после серии ошибок:", self._error_cooldown)
        limits_form.addRow("Стоп после ошибок подряд:", self._stop_after_error_streak)
        lay.addWidget(limits_box)

        bootstrap_box = self._settings_card("Быстрый первый проход")
        bootstrap_l = QVBoxLayout(bootstrap_box)
        self._fast_bootstrap_enabled = QCheckBox("Быстро слить доступную энергию при запуске фона")
        self._fast_bootstrap_enabled.setChecked(bool(comp.get("fast_bootstrap_enabled", True)))
        bootstrap_l.addWidget(self._fast_bootstrap_enabled)
        bootstrap_l.addWidget(
            self._small_hint(
                "Первый проход после «Запустить всё» идёт быстрее: без длинных burst-пауз и без дневного бюджета. "
                "После слива энергии аккаунт засыпает до случайного порога из списка ниже."
            )
        )

        bootstrap_form = QFormLayout()
        bootstrap_form.setSpacing(12)
        bootstrap_form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        def _seconds_spin(value: float, *, maximum: float = 60.0) -> QDoubleSpinBox:
            s = QDoubleSpinBox()
            s.setRange(0.0, maximum)
            s.setDecimals(1)
            s.setSingleStep(0.1)
            s.setSuffix(" сек")
            s.setValue(float(value))
            return s

        self._bootstrap_stagger_min = _seconds_spin(
            float(comp.get("bootstrap_account_stagger_min_seconds", 0.1))
        )
        self._bootstrap_stagger_max = _seconds_spin(
            float(comp.get("bootstrap_account_stagger_max_seconds", 0.4))
        )
        self._bootstrap_move_delay_min = _seconds_spin(
            float(comp.get("bootstrap_move_delay_min_seconds", 6.0))
        )
        self._bootstrap_move_delay_max = _seconds_spin(
            float(comp.get("bootstrap_move_delay_max_seconds", 7.5))
        )
        targets = comp.get("steady_energy_targets", [10, 15, 20])
        if isinstance(targets, (list, tuple)):
            targets_text = ",".join(str(int(x)) for x in targets if str(x).strip())
        else:
            targets_text = str(targets or "10,15,20")
        self._steady_energy_targets = QLineEdit()
        self._steady_energy_targets.setPlaceholderText("Например: 10,15,20")
        self._steady_energy_targets.setText(targets_text)

        bootstrap_form.addRow("Старт аккаунтов от:", self._bootstrap_stagger_min)
        bootstrap_form.addRow("Старт аккаунтов до:", self._bootstrap_stagger_max)
        bootstrap_form.addRow("Пауза между ходами от:", self._bootstrap_move_delay_min)
        bootstrap_form.addRow("Пауза между ходами до:", self._bootstrap_move_delay_max)
        bootstrap_form.addRow("Пороги возврата:", self._steady_energy_targets)
        bootstrap_l.addLayout(bootstrap_form)
        lay.addWidget(bootstrap_box)

        quiet_box = self._settings_card("Quiet Hours")
        quiet_l = QVBoxLayout(quiet_box)
        self._quiet_enabled = QCheckBox("Ограничивать фон в локальные тихие часы")
        self._quiet_enabled.setChecked(bool(comp.get("quiet_hours_enabled", False)))
        quiet_l.addWidget(self._quiet_enabled)
        quiet_row = QHBoxLayout()
        self._quiet_start = QSpinBox()
        self._quiet_start.setRange(0, 23)
        self._quiet_start.setValue(max(0, min(23, int(comp.get("quiet_hours_start_hour", 0)))))
        self._quiet_end = QSpinBox()
        self._quiet_end.setRange(0, 23)
        self._quiet_end.setValue(max(0, min(23, int(comp.get("quiet_hours_end_hour", 8)))))
        quiet_row.addWidget(QLabel("С"))
        quiet_row.addWidget(self._quiet_start)
        quiet_row.addWidget(QLabel("до"))
        quiet_row.addWidget(self._quiet_end)
        quiet_row.addStretch()
        quiet_wrap = QWidget()
        quiet_wrap.setLayout(quiet_row)
        quiet_l.addWidget(quiet_wrap)
        quiet_l.addWidget(
            self._small_hint(
                "В quiet hours фон не делает сетевые действия. Ручной sync разрешён, но ручная серия ходов всё равно подчиняется лимитам."
            )
        )
        lay.addWidget(quiet_box)

        confirm_box = self._settings_card("Подтверждения и прозрачность")
        confirm_l = QVBoxLayout(confirm_box)
        self._confirm_mass_code = QCheckBox("Подтверждать массовый промокод для всех аккаунтов")
        self._confirm_mass_code.setChecked(bool(comp.get("require_confirm_mass_code", True)))
        self._confirm_play_session = QCheckBox("Подтверждать ручную серию ходов")
        self._confirm_play_session.setChecked(bool(comp.get("require_confirm_play_session", True)))
        confirm_l.addWidget(self._confirm_mass_code)
        confirm_l.addWidget(self._confirm_play_session)
        confirm_l.addWidget(
            self._small_hint(
                "Мы не пытаемся эмулировать браузерную среду, input-telemetry или официальный Telegram/WebView. "
                "Режимы выше только уменьшают нагрузку и делают поведение явным для пользователя."
            )
        )
        lay.addWidget(confirm_box)

        lay.addStretch()
        return w

    def _tab_notify(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.addWidget(
            QLabel(
                "Какие события дублировать в Telegram. Токен и Chat ID можно не указывать — "
                "тогда уведомления не отправляются."
            )
        )
        tg = self._raw.get("telegram") or {}
        form = QFormLayout()
        self._tg_token = QLineEdit()
        self._tg_token.setPlaceholderText("От @BotFather")
        self._tg_chat = QLineEdit()
        self._tg_chat.setPlaceholderText("Ваш chat_id")
        self._tg_token.setText(str(tg.get("bot_token", "")))
        self._tg_chat.setText(str(tg.get("chat_id", "")))
        self._tg_notify_move = QCheckBox("О каждом ходе по доске")
        self._tg_notify_move.setChecked(bool(tg.get("notify_on_move", True)))
        self._tg_notify_daily = QCheckBox("О получении ежедневной награды")
        self._tg_notify_daily.setChecked(bool(tg.get("notify_on_daily_claim", True)))
        self._tg_notify_season = QCheckBox("О клейме сезонного пропуска")
        self._tg_notify_season.setChecked(bool(tg.get("notify_on_season_claim", True)))
        checks_col = QVBoxLayout()
        checks_col.addWidget(self._tg_notify_move)
        checks_col.addWidget(self._tg_notify_daily)
        checks_col.addWidget(self._tg_notify_season)
        checks_wrap = QWidget()
        checks_wrap.setLayout(checks_col)
        self._tg_summary = QSpinBox()
        self._tg_summary.setRange(0, 86400)
        self._tg_summary.setSuffix(" сек (0 = выкл.)")
        self._tg_summary.setValue(max(0, int(tg.get("summary_interval_seconds", 3600))))
        form.addRow("Токен бота:", self._tg_token)
        form.addRow("Chat ID:", self._tg_chat)
        form.addRow("События:", checks_wrap)
        form.addRow("Сводка раз в (0 = выкл.):", self._tg_summary)
        lay.addLayout(form)

        test_box = QGroupBox("Проверка уведомлений")
        test_l = QVBoxLayout(test_box)
        test_l.addWidget(
            self._small_hint(
                "Отправка в Telegram сейчас, без сохранения настроек. "
                "Нужны заполненные токен и Chat ID выше."
            )
        )
        test_form = QFormLayout()
        self._tg_test_format = QComboBox()
        self._tg_test_format.addItem("Ход по доске", "move")
        self._tg_test_format.addItem("Ежедневная награда", "daily")
        self._tg_test_format.addItem("Сезонный пропуск", "season")
        self._tg_test_format.addItem("Периодическая сводка", "summary")
        test_form.addRow("Формат:", self._tg_test_format)
        self._tg_test_btn = QPushButton("Отправить тестовое сообщение")
        self._tg_test_btn.clicked.connect(self._on_send_test_telegram)
        test_form.addRow("", self._tg_test_btn)
        test_l.addLayout(test_form)
        lay.addWidget(test_box)

        lay.addStretch()
        return w

    def _on_send_test_telegram(self) -> None:
        token = self._tg_token.text().strip()
        chat = self._tg_chat.text().strip()
        if not token or not chat:
            QMessageBox.warning(
                self,
                "Тест уведомления",
                "Укажите токен бота и Chat ID в полях выше.",
            )
            return
        g = self._raw.get("gamee") or {}
        gm = int(g.get("gold_micro_divisor", 1_000_000))
        ed = int(g.get("gold_estimate_usd_micro_divisor", 1_000_000_000_000))
        ts = datetime.now().strftime("%d.%m.%Y %H:%M:%S")
        kind = self._tg_test_format.currentData()
        notifier = TelegramNotifier(token, chat)
        try:
            if kind == "move":
                text = format_board_move_message(
                    label="demo",
                    move_idx=1,
                    dice_display="4",
                    rewards_line="⭐ 10, 💰 50, 🎟️ 2",
                    energy_before=8,
                    energy_after=7,
                    gold_before=14000,
                    gold_after=14050,
                    tickets_before=450,
                    tickets_after=452,
                    xp_gained=15,
                    time_local=ts,
                    gold_micro_divisor=gm,
                    gold_estimate_usd_micro_divisor=ed,
                )
            elif kind == "daily":
                text = format_daily_claim_message(
                    label="demo",
                    rewards_line="⚡ 25, 💰 100",
                    streak=3,
                    streak_total=14,
                )
            elif kind == "season":
                text = format_season_claim_message(
                    label="demo",
                    rewards_line="💰 500, ⭐ 20",
                )
            else:
                text = format_summary_message(
                    [
                        {
                            "label": "demo",
                            "energy": 8,
                            "gold": 14000,
                            "status": "ожидание (тест)",
                        },
                        {
                            "label": "demo-2",
                            "energy": 5,
                            "gold": 9000,
                            "status": "типичная строка",
                        },
                    ],
                    gm,
                    ed,
                )
            ok = notifier.send(text, silent=False)
        finally:
            notifier.close()
        if ok:
            QMessageBox.information(
                self,
                "Тест уведомления",
                "Сообщение отправлено. Проверь чат с ботом.",
            )
        else:
            QMessageBox.warning(
                self,
                "Тест уведомления",
                "Не удалось отправить. Проверь токен, Chat ID и что написал боту /start.",
            )

    def _on_save(self) -> None:
        aid = self._th_api_id.text().strip()
        ah = self._th_api_hash.text().strip()
        if not aid or not ah:
            QMessageBox.warning(
                self,
                "Ключи Telegram",
                "Укажите api_id и api_hash с my.telegram.org — без них программа не работает.",
            )
            return
        try:
            api_id_int = int(aid)
            if api_id_int <= 0:
                raise ValueError
        except ValueError:
            QMessageBox.warning(self, "api_id", "api_id должно быть положительным числом.")
            return
        ref = self._th_gamee_ref.text().strip()
        tr_line = self._th_telegram_referral_ref.text().strip()
        telegram_referral_ref = None
        if tr_line:
            try:
                telegram_referral_ref = int(tr_line)
                if telegram_referral_ref <= 0:
                    raise ValueError
            except ValueError:
                QMessageBox.warning(
                    self,
                    "User ID в Telegram",
                    "Нужно целое число или пустое поле.",
                )
                return
        th = {
            "api_id": api_id_int,
            "api_hash": ah,
            "gamee_ref": ref if ref else None,
            "telegram_referral_ref": telegram_referral_ref,
        }

        steady_targets: list[int] = []
        for part in self._steady_energy_targets.text().replace(";", ",").split(","):
            part = part.strip()
            if not part:
                continue
            try:
                n = int(part)
            except ValueError:
                QMessageBox.warning(
                    self,
                    "Пороги возврата",
                    "Пороги возврата должны быть числами через запятую, например 10,15,20.",
                )
                return
            if n < 5:
                QMessageBox.warning(
                    self,
                    "Пороги возврата",
                    "Минимальный порог возврата — 5 энергии, иначе ход невозможен.",
                )
                return
            steady_targets.append(n)
        if not steady_targets:
            QMessageBox.warning(
                self,
                "Пороги возврата",
                "Укажите хотя бы один порог возврата, например 10,15,20.",
            )
            return

        bs_stagger_min = float(self._bootstrap_stagger_min.value())
        bs_stagger_max = float(self._bootstrap_stagger_max.value())
        bs_move_min = float(self._bootstrap_move_delay_min.value())
        bs_move_max = float(self._bootstrap_move_delay_max.value())
        if bs_stagger_min > bs_stagger_max:
            bs_stagger_min, bs_stagger_max = bs_stagger_max, bs_stagger_min
        if bs_move_min > bs_move_max:
            bs_move_min, bs_move_max = bs_move_max, bs_move_min

        summary_sec = self._tg_summary.value()
        gamee = {
            "transport_backend": str(
                self._transport_backend.currentData()
                or GAMEE_TRANSPORT_BACKEND_CURL_CFFI_RAW_HTTP
            ),
        }
        telegram = {
            "bot_token": self._tg_token.text().strip(),
            "chat_id": self._tg_chat.text().strip(),
            "notify_on_move": self._tg_notify_move.isChecked(),
            "notify_on_daily_claim": self._tg_notify_daily.isChecked(),
            "notify_on_season_claim": self._tg_notify_season.isChecked(),
            "summary_interval_seconds": int(summary_sec),
        }
        compliance = {
            "background_mode": str(self._bg_mode.currentData() or BACKGROUND_MODE_MANUAL_ONLY),
            "session_duration_minutes": 0,
            "quiet_hours_enabled": self._quiet_enabled.isChecked(),
            "quiet_hours_start_hour": int(self._quiet_start.value()),
            "quiet_hours_end_hour": int(self._quiet_end.value()),
            "daily_move_budget": int(self._daily_move_budget.value()),
            "max_moves_per_session": int(self._max_moves_session.value()),
            "fast_bootstrap_enabled": self._fast_bootstrap_enabled.isChecked(),
            "bootstrap_account_stagger_min_seconds": bs_stagger_min,
            "bootstrap_account_stagger_max_seconds": bs_stagger_max,
            "bootstrap_move_delay_min_seconds": bs_move_min,
            "bootstrap_move_delay_max_seconds": bs_move_max,
            "steady_energy_targets": steady_targets,
            "error_cooldown_seconds": int(self._error_cooldown.value()),
            "stop_after_error_streak": int(self._stop_after_error_streak.value()),
            "require_confirm_mass_code": self._confirm_mass_code.isChecked(),
            "require_confirm_play_session": self._confirm_play_session.isChecked(),
        }

        try:
            save_config_sections(
                self._config_path,
                gamee=gamee,
                telegram=telegram,
                telethon=th,
                compliance=compliance,
            )
        except OSError as e:
            QMessageBox.critical(self, "Сохранение", str(e))
            return

        self.accept()
