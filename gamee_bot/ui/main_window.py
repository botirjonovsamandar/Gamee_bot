from __future__ import annotations

import gc
from collections import defaultdict
from datetime import datetime
from pathlib import Path
import time
import traceback
from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QElapsedTimer, Qt, QThread, QTimer
from PySide6.QtGui import QAction, QBrush, QColor, QPalette, QShowEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from gamee_bot.account_store import load_accounts, remove_account_by_label, set_account_proxy_url
from gamee_bot.config import (
    BACKGROUND_MODE_FULL_AUTO,
    BACKGROUND_MODE_MANUAL_ONLY,
    AppConfig,
    TELETHON_CREDENTIALS_REQUIRED_MSG,
    background_mode_label,
    gamee_proxy_table_summary,
    load_config,
    telethon_credentials_ready,
)
from gamee_bot.ui.account_action_thread import AccountActionThread
from gamee_bot.ui.add_account_dialog import AddAccountDialog
from gamee_bot.ui.app_style import apply_app_style
from gamee_bot.ui.edit_proxy_dialog import EditAccountProxyDialog
from gamee_bot.ui.enter_code_dialog import EnterCodeDialog
from gamee_bot.ui.enter_code_thread import EnterCodeThread
from gamee_bot.ui.enter_draw_thread import EnterDrawThread
from gamee_bot.ui.settings_dialog import SettingsDialog
from gamee_bot.regen import format_daily_checkin_countdown, format_next_live_countdown
from gamee_bot.gamee_transport import (
    gamee_transport_backend_blocker_message,
    normalize_gamee_transport_backend,
)
from gamee_bot.telethon_bridge import clear_init_cache
from gamee_bot.worker import BotWorker


class MainWindow(QMainWindow):
    _DAILY_CLAIM_FLASH_SEC = 30.0

    def __init__(self, config_path: Path) -> None:
        super().__init__()
        self._config_path = config_path.resolve()
        self._cfg: AppConfig | None = None
        self._worker: BotWorker | None = None
        self._regen_meta: dict[str, tuple[str | None, int]] = {}
        self._logged_cfg_error_once = False
        self._telethon_ready = False
        self._enter_code_thread: EnterCodeThread | None = None
        self._enter_draw_thread: EnterDrawThread | None = None
        self._manual_action_thread: AccountActionThread | None = None
        self._known_account_labels: set[str] = set()
        self._session_gold_earned: defaultdict[str, int] = defaultdict(int)
        self._session_tickets_earned: defaultdict[str, int] = defaultdict(int)
        self._session_xp_earned: defaultdict[str, int] = defaultdict(int)
        self._manual_moves_used_today: dict[str, tuple[str, int]] = {}
        self._daily_meta: dict[str, tuple[str, str | None, int, int]] = {}
        self._daily_claim_flash: dict[str, dict[str, Any]] = {}
        self._banned_row_labels: set[str] = set()
        self._worker_table_pending: list[Any] | None = None
        self._last_rows_by_label: dict[str, dict[str, Any]] = {}
        self._worker_table_coalesce = QTimer(self)
        self._worker_table_coalesce.setSingleShot(True)
        self._worker_table_coalesce.setInterval(75)
        self._worker_table_coalesce.timeout.connect(self._flush_worker_table_pending)
        self._load_config_silent()
        title = self._cfg.window_title if self._cfg else "Manager"
        self.setWindowTitle(title)
        self.resize(1350, 750)
        self._build_ui()
        self._build_menu()
        self._apply_title()
        self._sync_bot_buttons()
        self._update_worker_status_label()
        self._update_mode_notice()

    def open_settings_dialog(self) -> None:
        dlg = SettingsDialog(self._config_path, self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        self._load_config_silent()
        self._apply_title()
        self._update_mode_notice()
        if self._config_error:
            QMessageBox.warning(self, "Конфиг", self._config_error)
        elif self._telethon_ready:
            self._log.append("Настройки сохранены.")
            QTimer.singleShot(0, self._fill_table_with_placeholders)
        else:
            self._log.append("Настройки сохранены. Укажите api_id и api_hash Telegram — без них нельзя добавить аккаунт или запускать фоновые действия.")
            QTimer.singleShot(0, self._fill_table_with_placeholders)

    def _load_config_silent(self) -> None:
        try:
            self._cfg = load_config(self._config_path)
        except Exception as e:
            self._cfg = None
            self._config_error = str(e)
            self._telethon_ready = False
            self._logged_cfg_error_once = False
        else:
            self._config_error = ""
            self._telethon_ready = telethon_credentials_ready(self._cfg)
            self._logged_cfg_error_once = False

    def _apply_title(self) -> None:
        if self._cfg:
            self.setWindowTitle(self._cfg.window_title)

    def _build_menu(self) -> None:
        m = self.menuBar()
        act_settings = QAction("Настройки…", self)
        act_settings.triggered.connect(self.open_settings_dialog)
        m.addAction(act_settings)
        add_acc = QAction("Добавить аккаунт…", self)
        add_acc.triggered.connect(self._add_account)
        m.addAction(add_acc)
        act_code = QAction("Ввести код…", self)
        act_code.triggered.connect(self._on_enter_code)
        m.addAction(act_code)

    def _sync_bot_buttons(self) -> None:
        manual_busy = self._manual_action_thread is not None and self._manual_action_thread.isRunning()
        code_busy = self._enter_code_thread is not None and self._enter_code_thread.isRunning()
        draw_busy = self._enter_draw_thread is not None and self._enter_draw_thread.isRunning()
        running = self._worker is not None and self._worker.isRunning()
        self._btn_start_bot.setEnabled(not running and not manual_busy and not code_busy and not draw_busy)
        self._btn_stop_bot.setEnabled(running)
        self._btn_enter_code.setEnabled(not manual_busy and not code_busy and not draw_busy)
        self._btn_enter_draw.setEnabled(not running and not manual_busy and not code_busy and not draw_busy)
        sel = self._selected_account_label() is not None
        manual_enabled = sel and not running and not manual_busy and not code_busy and not draw_busy
        self._btn_sync_now.setEnabled(manual_enabled)
        self._btn_claim_daily.setEnabled(manual_enabled)
        self._btn_play_session.setEnabled(manual_enabled)

    def _update_worker_status_label(self) -> None:
        running = self._worker is not None and self._worker.isRunning()
        manual_busy = self._manual_action_thread is not None and self._manual_action_thread.isRunning()
        draw_busy = self._enter_draw_thread is not None and self._enter_draw_thread.isRunning()
        mode = background_mode_label(
            self._cfg.compliance.background_mode if self._cfg is not None else BACKGROUND_MODE_MANUAL_ONLY
        )
        if (manual_busy or draw_busy) and not running:
            self._worker_status.setText("● Выполняется ручное действие")
            self._worker_status.setToolTip("Идёт явная user-triggered операция.")
            self._worker_status.setObjectName("workerStatusRunning")
        elif running:
            self._worker_status.setText(f"● Фон запущен · {mode}")
            self._worker_status.setToolTip("Идёт фоновый цикл только в разрешённом режимом объёме.")
            self._worker_status.setObjectName("workerStatusRunning")
        else:
            self._worker_status.setText(f"○ Остановлено · {mode}")
            self._worker_status.setToolTip('Нажмите «Запустить всё».')
            self._worker_status.setObjectName("workerStatusStopped")
        self._worker_status.style().unpolish(self._worker_status)
        self._worker_status.style().polish(self._worker_status)

    def _update_mode_notice(self) -> None:
        if self._cfg is None:
            self._mode_notice.setText("")
            return
        c = self._cfg.compliance
        backend_raw = self._cfg.gamee.transport_backend
        backend = normalize_gamee_transport_backend(backend_raw)
        backend_blocker = gamee_transport_backend_blocker_message(backend_raw)
        quiet = "вкл." if c.quiet_hours_enabled else "выкл."
        if backend_blocker:
            backend_note = f"backend: {backend} (недоступен: {backend_blocker})"
        else:
            backend_note = f"backend: {backend}"
        bootstrap = "fast bootstrap: вкл." if c.fast_bootstrap_enabled else "fast bootstrap: выкл."
        self._mode_notice.setText(
            f"Фоновый режим: {background_mode_label(c.background_mode)}; quiet hours: {quiet}; "
            f"{bootstrap}; {backend_note}."
        )

    def _ensure_transport_backend_ready(self, title: str) -> bool:
        if self._cfg is None:
            return False
        backend_raw = self._cfg.gamee.transport_backend
        blocker = gamee_transport_backend_blocker_message(backend_raw)
        if blocker is None:
            return True
        backend = normalize_gamee_transport_backend(backend_raw)
        msg = (
            f"{blocker}\n\n"
            f"Текущее значение config: gamee.transport_backend={backend_raw!r}"
            f" (нормализовано: {backend!r})."
        )
        QMessageBox.warning(self, title, msg)
        self._log.append(f"[transport] {blocker}")
        return False

    @staticmethod
    def _day_key_local() -> str:
        return datetime.now().strftime("%Y-%m-%d")

    def _moves_used_today(self, label: str) -> int:
        key = self._day_key_local()
        day, used = self._manual_moves_used_today.get(label, (key, 0))
        if day != key:
            self._manual_moves_used_today[label] = (key, 0)
            return 0
        return int(used)

    def _remaining_manual_moves(self, label: str) -> int:
        if self._cfg is None:
            return 0
        daily_budget = int(self._cfg.compliance.daily_move_budget)
        if daily_budget <= 0:
            return int(self._cfg.compliance.max_moves_per_session)
        used = self._moves_used_today(label)
        return max(0, daily_budget - used)

    def _consume_manual_moves(self, label: str, delta: int) -> None:
        if delta <= 0:
            return
        if self._cfg is not None and int(self._cfg.compliance.daily_move_budget) <= 0:
            return
        key = self._day_key_local()
        used = self._moves_used_today(label)
        self._manual_moves_used_today[label] = (key, used + int(delta))

    def _add_account(self) -> None:
        self._load_config_silent()
        if self._cfg is None or self._config_error:
            QMessageBox.critical(self, "Конфиг", self._config_error or "Нет конфига")
            return
        if not self._telethon_ready:
            QMessageBox.warning(self, "Telegram API", TELETHON_CREDENTIALS_REQUIRED_MSG)
            return
        dlg = AddAccountDialog(self._cfg, self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        w = self._worker
        if w is not None and w.isRunning():
            w.wake_idle()
        QTimer.singleShot(0, self._fill_table_with_placeholders)

    def _on_enter_code(self) -> None:
        self._load_config_silent()
        if self._cfg is None or self._config_error:
            QMessageBox.critical(self, "Конфиг", self._config_error or "Нет конфига")
            return
        if self._worker is not None and self._worker.isRunning():
            QMessageBox.information(self, "Код", "Остановите фоновый режим перед массовым промокодом.")
            return
        if self._manual_action_thread is not None and self._manual_action_thread.isRunning():
            QMessageBox.information(self, "Код", "Дождитесь завершения текущего ручного действия.")
            return
        if self._enter_draw_thread is not None and self._enter_draw_thread.isRunning():
            QMessageBox.information(self, "Код", "Дождитесь завершения Draw XP.")
            return
        if not self._telethon_ready:
            QMessageBox.warning(self, "Telegram API", TELETHON_CREDENTIALS_REQUIRED_MSG)
            return
        if not self._ensure_transport_backend_ready("Код"):
            return
        if self._enter_code_thread is not None and self._enter_code_thread.isRunning():
            QMessageBox.information(self, "Код", "Уже идёт отправка промокода.")
            return
        default_tid = self._cfg.gamee.check_task_id
        dlg = EnterCodeDialog(default_tid, self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        task_id = dlg.task_id()
        code = dlg.code()
        if not code:
            QMessageBox.warning(self, "Код", "Введите непустой код.")
            return
        try:
            accounts = load_accounts(self._cfg.accounts_path)
        except Exception as e:
            QMessageBox.critical(self, "accounts.yaml", str(e))
            return
        if (
            self._cfg.compliance.require_confirm_mass_code
            and accounts
        ):
            ans = QMessageBox.question(
                self,
                "Массовый промокод",
                f"Отправить код на {len(accounts)} аккаунтов? Это последовательная массовая операция.\n\n"
                "Важно: проект использует Telethon + raw HTTP, а не реальный Telegram WebView/браузер; "
                "запросы эмулируют стандартный протокол Telegram Android WebView.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if ans != QMessageBox.StandardButton.Yes:
                return
        self._log.append(
            f"——— Промокод для всех аккаунтов (taskId={task_id}) ———"
        )
        th = EnterCodeThread(self._cfg, code, task_id, self)
        self._enter_code_thread = th
        th.log_line.connect(self._log.append)
        th.finished.connect(self._on_enter_code_thread_finished)
        self._btn_enter_code.setEnabled(False)
        th.start()

    def _on_enter_code_thread_finished(self) -> None:
        if self.sender() is self._enter_code_thread:
            self._enter_code_thread = None
        self._sync_bot_buttons()

    def _on_enter_draw_all(self) -> None:
        self._load_config_silent()
        if self._cfg is None or self._config_error:
            QMessageBox.critical(self, "Конфиг", self._config_error or "Нет конфига")
            return
        if self._worker is not None and self._worker.isRunning():
            QMessageBox.information(self, "Draw XP", "Остановите фоновый режим перед добавлением XP в draw.")
            return
        if self._manual_action_thread is not None and self._manual_action_thread.isRunning():
            QMessageBox.information(self, "Draw XP", "Дождитесь завершения текущего ручного действия.")
            return
        if self._enter_code_thread is not None and self._enter_code_thread.isRunning():
            QMessageBox.information(self, "Draw XP", "Дождитесь завершения массового промокода.")
            return
        if self._enter_draw_thread is not None and self._enter_draw_thread.isRunning():
            QMessageBox.information(self, "Draw XP", "Draw XP уже выполняется.")
            return
        if not self._telethon_ready:
            QMessageBox.warning(self, "Telegram API", TELETHON_CREDENTIALS_REQUIRED_MSG)
            return
        if not self._ensure_transport_backend_ready("Draw XP"):
            return
        try:
            accounts = load_accounts(self._cfg.accounts_path)
        except Exception as e:
            QMessageBox.critical(self, "accounts.yaml", str(e))
            return
        if not accounts:
            QMessageBox.information(self, "Draw XP", "Нет аккаунтов в accounts.yaml.")
            return
        ans = QMessageBox.question(
            self,
            "Draw XP",
            f"Добавить доступный XP в Lucky Draw для {len(accounts)} аккаунтов?\n\n"
            "Будут пропущены locked/purchase-only draw и аккаунты, где нечего добавить.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if ans != QMessageBox.StandardButton.Yes:
            return
        self._log.append("——— Draw XP для всех аккаунтов ———")
        th = EnterDrawThread(self._cfg, self)
        self._enter_draw_thread = th
        th.log_line.connect(self._log.append)
        th.finished.connect(self._on_enter_draw_thread_finished)
        self._sync_bot_buttons()
        self._update_worker_status_label()
        th.start()

    def _on_enter_draw_thread_finished(self) -> None:
        if self.sender() is self._enter_draw_thread:
            self._enter_draw_thread = None
        self._sync_bot_buttons()
        self._update_worker_status_label()

    def _rows_for_display(self) -> list[dict[str, Any]]:
        merged = {k: dict(v) for k, v in self._last_rows_by_label.items()}
        try:
            if self._cfg is not None:
                ordered_labels = [a.label for a in load_accounts(self._cfg.accounts_path)]
            else:
                ordered_labels = []
        except Exception:
            ordered_labels = []
        rows: list[dict[str, Any]] = []
        for label in ordered_labels:
            row = merged.pop(label, None)
            if row is not None:
                rows.append(row)
        for row in merged.values():
            rows.append(row)
        return rows

    def _on_manual_row_ready(self, row: object) -> None:
        if not isinstance(row, dict):
            return
        label = str(row.get("label", "")).strip()
        if not label:
            return
        self._last_rows_by_label[label] = dict(row)
        self._on_table(self._rows_for_display())

    def _on_manual_move_earned(
        self, label: str, gold_delta: int, tickets_delta: int, xp_delta: int
    ) -> None:
        self._consume_manual_moves(label, 1)
        self._on_session_earnings_move(label, gold_delta, tickets_delta, xp_delta)

    def _on_manual_action_finished(self) -> None:
        if self.sender() is self._manual_action_thread:
            self._manual_action_thread = None
        self._sync_bot_buttons()
        self._update_worker_status_label()

    def _start_manual_action(self, action: str) -> None:
        self._load_config_silent()
        if self._cfg is None or self._config_error:
            QMessageBox.critical(self, "Конфиг", self._config_error or "Нет конфига")
            return
        if self._worker is not None and self._worker.isRunning():
            QMessageBox.information(self, "Ручное действие", "Сначала остановите фоновый режим.")
            return
        if self._enter_code_thread is not None and self._enter_code_thread.isRunning():
            QMessageBox.information(self, "Ручное действие", "Дождитесь завершения массового промокода.")
            return
        if self._enter_draw_thread is not None and self._enter_draw_thread.isRunning():
            QMessageBox.information(self, "Ручное действие", "Дождитесь завершения Draw XP.")
            return
        if self._manual_action_thread is not None and self._manual_action_thread.isRunning():
            QMessageBox.information(self, "Ручное действие", "Уже выполняется другое ручное действие.")
            return
        if not self._ensure_transport_backend_ready("Ручное действие"):
            return
        label = self._selected_account_label()
        if not label:
            QMessageBox.information(self, "Ручное действие", "Сначала выберите аккаунт в таблице.")
            return
        move_limit = 0
        if action == "play_session":
            remaining = self._remaining_manual_moves(label)
            move_limit = min(self._cfg.compliance.max_moves_per_session, remaining)
            remaining_text = (
                "без лимита"
                if int(self._cfg.compliance.daily_move_budget) <= 0
                else str(remaining)
            )
            if move_limit <= 0:
                QMessageBox.information(
                    self,
                    "Ручная серия ходов",
                    f"Для «{label}» дневной бюджет ходов уже исчерпан.",
                )
                return
            if self._cfg.compliance.require_confirm_play_session:
                ans = QMessageBox.question(
                    self,
                    "Ручная серия ходов",
                    f"Запустить серию ходов для «{label}»?\n"
                    f"Лимит этой сессии: {move_limit}\n"
                    f"Остаток дневного бюджета: {remaining_text}\n\n"
                    "Важно: действие выполняется через Telethon + raw HTTP, а не через реальный Telegram WebView/браузер; "
                    "явные маркеры клиента сохраняются.",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No,
                )
                if ans != QMessageBox.StandardButton.Yes:
                    return
        names = {
            "sync": "Sync now",
            "claim_daily": "Claim daily",
            "enter_draw": "Draw XP",
            "play_session": "Play session",
        }
        self._log.append(f"[{label}] {names.get(action, action)} — старт.")
        th = AccountActionThread(
            self._cfg,
            label,
            action,
            move_limit=move_limit,
            parent=self,
        )
        self._manual_action_thread = th
        th.log_line.connect(self._log.append)
        th.row_ready.connect(self._on_manual_row_ready)
        th.move_earned.connect(self._on_manual_move_earned)
        th.finished.connect(self._on_manual_action_finished)
        self._sync_bot_buttons()
        self._update_worker_status_label()
        th.start()

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setSpacing(10)
        layout.setContentsMargins(16, 16, 16, 16)

        title = QLabel("Аккаунты")
        title.setObjectName("panelTitle")
        layout.addWidget(title)

        self._worker_status = QLabel("○ Фон остановлен")
        self._worker_status.setObjectName("workerStatusStopped")
        layout.addWidget(self._worker_status)

        self._mode_notice = QLabel("")
        self._mode_notice.setObjectName("hintLabel")
        self._mode_notice.setWordWrap(True)
        layout.addWidget(self._mode_notice)

        self._total_gold_summary = QLabel("Всего по аккаунтам: 💰 0")
        self._total_gold_summary.setObjectName("totalGoldSummary")
        self._total_gold_summary.setToolTip(
            "Сумма золота по всем строкам таблицы и оценка в USD (как на prizes.gamee.com)."
        )
        layout.addWidget(self._total_gold_summary)

        bar = QHBoxLayout()
        self._btn_start_bot = QPushButton("Запустить всё")
        self._btn_start_bot.setToolTip("Запускает синхронизацию, клеймы и ходы для всех аккаунтов.")
        self._btn_start_bot.clicked.connect(self._start_worker)
        self._btn_stop_bot = QPushButton("Остановить всё")
        self._btn_stop_bot.setObjectName("btnStop")
        self._btn_stop_bot.setToolTip("Останавливает фоновый цикл полностью.")
        self._btn_stop_bot.setEnabled(False)
        self._btn_stop_bot.clicked.connect(self._stop_worker)
        bar.addWidget(self._btn_start_bot)
        bar.addWidget(self._btn_stop_bot)
        self._btn_sync_now = QPushButton("Sync now")
        self._btn_sync_now.setToolTip("Ручная синхронизация выбранного аккаунта без фонового цикла.")
        self._btn_sync_now.clicked.connect(lambda: self._start_manual_action("sync"))
        bar.addWidget(self._btn_sync_now)
        self._btn_sync_now.hide()
        self._btn_claim_daily = QPushButton("Claim daily")
        self._btn_claim_daily.setToolTip("Ручной клейм ежедневной награды для выбранного аккаунта.")
        self._btn_claim_daily.clicked.connect(lambda: self._start_manual_action("claim_daily"))
        bar.addWidget(self._btn_claim_daily)
        self._btn_claim_daily.hide()
        self._btn_play_session = QPushButton("Play session")
        self._btn_play_session.setToolTip("Ручная ограниченная серия ходов для выбранного аккаунта.")
        self._btn_play_session.clicked.connect(lambda: self._start_manual_action("play_session"))
        bar.addWidget(self._btn_play_session)
        self._btn_play_session.hide()
        self._btn_enter_code = QPushButton("Ввести код")
        self._btn_enter_code.setToolTip(
            "Промокод prizes.gamee.com для всех аккаунтов (telegram.checkTask.code)."
        )
        self._btn_enter_code.clicked.connect(self._on_enter_code)
        bar.addWidget(self._btn_enter_code)
        self._btn_enter_draw = QPushButton("Draw XP")
        self._btn_enter_draw.setToolTip(
            "Добавляет доступный XP аккаунтов в подходящую Lucky Draw раздачу вручную."
        )
        self._btn_enter_draw.clicked.connect(self._on_enter_draw_all)
        bar.addWidget(self._btn_enter_draw)
        bar.addStretch()
        self._btn_delete = QPushButton("Удалить выбранный…")
        self._btn_delete.setObjectName("btnStop")
        self._btn_delete.setEnabled(False)
        self._btn_delete.clicked.connect(self._delete_selected_account)
        bar.addWidget(self._btn_delete)
        self._btn_proxy = QPushButton("Прокси выбранного…")
        self._btn_proxy.setObjectName("btnToolbar")
        self._btn_proxy.setEnabled(False)
        self._btn_proxy.clicked.connect(self._edit_selected_proxy)
        bar.addWidget(self._btn_proxy)
        layout.addLayout(bar)

        self._table = QTableWidget(0, 9)
        self._table.setObjectName("accountsTable")
        self._table.setAlternatingRowColors(True)
        self._table.setShowGrid(False)
        vh = self._table.verticalHeader()
        vh.setVisible(True)
        vh.setDefaultSectionSize(34)
        self._apply_table_header_palette()
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._table.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._table.itemSelectionChanged.connect(self._update_toolbar_selection_state)
        self._table.currentCellChanged.connect(
            lambda _r, _c, _pr, _pc: self._update_toolbar_selection_state()
        )
        self._table.setHorizontalHeaderLabels(
            [
                "Аккаунт",
                "Прокси",
                "Энергия",
                "Золото",
                "Статус",
                "Последний ход",
                "Ежедневная награда",
                "Сезонные награды",
                "Заработано за сессию",
            ]
        )
        hdr = self._table.horizontalHeader()
        for c in range(9):
            hdr.setSectionResizeMode(c, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(8, QHeaderView.Stretch)
        layout.addWidget(self._table, 4)

        self._log = QTextEdit()
        self._log.setReadOnly(True)
        doc = self._log.document()
        if doc is not None:
            doc.setMaximumBlockCount(2000)
        self._log.setPlaceholderText("События будут здесь…")
        self._log.setMinimumHeight(180)
        self._log.setMaximumHeight(260)
        layout.addWidget(self._log, 2)

        self._mode_notice.hide()

        self._regen_timer = QTimer(self)
        self._regen_timer.setInterval(1000)
        self._regen_timer.timeout.connect(self._tick_regen_cells)
        self._regen_timer.start()

        self._gc_timer = QTimer(self)
        self._gc_timer.setInterval(10_000)
        self._gc_timer.timeout.connect(lambda: gc.collect())
        self._gc_timer.start()

    def _apply_table_header_palette(self) -> None:
        """Убирает «белые» полосы у номеров строк (Windows / нативная тема)."""
        dark = QColor("#1e2a3d")
        muted = QColor("#8fa8c4")
        for hdr in (self._table.horizontalHeader(), self._table.verticalHeader()):
            pal = QPalette(hdr.palette())
            pal.setColor(QPalette.ColorRole.Window, dark)
            pal.setColor(QPalette.ColorRole.Button, dark)
            pal.setColor(QPalette.ColorRole.Base, dark)
            pal.setColor(QPalette.ColorRole.AlternateBase, dark)
            pal.setColor(QPalette.ColorRole.Text, muted)
            pal.setColor(QPalette.ColorRole.WindowText, muted)
            pal.setColor(QPalette.ColorRole.ButtonText, muted)
            hdr.setPalette(pal)
            hdr.setAutoFillBackground(True)
        row_bg = QColor("#151d28")
        row_alt = QColor("#1a2332")
        tpal = QPalette(self._table.palette())
        tpal.setColor(QPalette.ColorRole.Base, row_bg)
        tpal.setColor(QPalette.ColorRole.AlternateBase, row_alt)
        self._table.setPalette(tpal)
        self._table.setAutoFillBackground(True)
        vp = self._table.viewport()
        vp.setAutoFillBackground(True)
        vpp = QPalette(vp.palette())
        vpp.setColor(QPalette.ColorRole.Base, row_bg)
        vp.setPalette(vpp)

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        QTimer.singleShot(0, self._fill_table_with_placeholders)

    def _fill_table_with_placeholders(self) -> None:
        """Показать аккаунты из accounts.yaml с прочерками — без API-запросов."""
        if self._worker is not None and self._worker.isRunning():
            return
        self._load_config_silent()
        if self._cfg is None or self._config_error:
            return
        try:
            accounts = load_accounts(self._cfg.accounts_path)
        except Exception:
            return
        rows: list[dict[str, Any]] = []
        for acc in accounts:
            px_cell, px_tip = gamee_proxy_table_summary(acc.proxy_url)
            rows.append({
                "label": acc.label,
                "energy": 0,
                "gold": 0,
                "gold_estimated_usd": None,
                "status": "—",
                "last_error": "",
                "last_move_at": "",
                "regen_deadline_iso": None,
                "daily_claim_rewards_text": "",
                "daily_checkin_deadline_iso": None,
                "daily_checkin_streak": 0,
                "daily_checkin_streak_total": 0,
                "season_rewards_text": "—",
                "proxy_cell": px_cell,
                "proxy_tooltip": px_tip,
            })
        self._on_table(rows)

    def _enqueue_worker_table(self, rows: object) -> None:
        """Сливает лавину table_updated от десятков потоков — иначе очередь Qt раздувается (сбои под Windows)."""
        if not isinstance(rows, list):
            return
        self._worker_table_pending = rows
        if not self._worker_table_coalesce.isActive():
            self._worker_table_coalesce.start()

    def _flush_worker_table_pending(self) -> None:
        rows = self._worker_table_pending
        self._worker_table_pending = None
        if rows is not None:
            self._on_table(rows)

    def _start_worker(self) -> None:
        if self._worker and self._worker.isRunning():
            return
        self._load_config_silent()
        if self._cfg is None or self._config_error:
            QMessageBox.critical(self, "Конфиг", self._config_error or "Нет конфига")
            return
        if not self._telethon_ready:
            QMessageBox.warning(self, "Telegram API", TELETHON_CREDENTIALS_REQUIRED_MSG)
            return
        if self._manual_action_thread is not None and self._manual_action_thread.isRunning():
            QMessageBox.information(self, "Фон", "Дождитесь завершения текущего ручного действия.")
            return
        if self._enter_code_thread is not None and self._enter_code_thread.isRunning():
            QMessageBox.information(self, "Фон", "Дождитесь завершения массового промокода.")
            return
        if self._enter_draw_thread is not None and self._enter_draw_thread.isRunning():
            QMessageBox.information(self, "Фон", "Дождитесь завершения Draw XP.")
            return
        if not self._ensure_transport_backend_ready("Фон"):
            return
        self._cfg.compliance.background_mode = BACKGROUND_MODE_FULL_AUTO
        self._worker_table_pending = None
        self._worker = BotWorker(self._cfg, self)
        self._worker.table_updated.connect(
            self._enqueue_worker_table,
            Qt.ConnectionType.QueuedConnection,
        )
        self._worker.session_earnings_move.connect(self._on_session_earnings_move)
        self._worker.log_message.connect(self._log.append)
        self._worker.fatal_error.connect(self._on_fatal)
        self._worker.finished.connect(
            self._on_worker_thread_finished,
            Qt.ConnectionType.SingleShotConnection,
        )
        self._worker.start()
        self._sync_bot_buttons()
        self._update_worker_status_label()
        self._log.append(
            "Запущено всё: синхронизация, награды и ходы для всех аккаунтов."
        )

    def _stop_worker(self) -> None:
        w = self._worker
        if w is None:
            return
        self._worker_table_coalesce.stop()
        self._flush_worker_table_pending()
        if w.isRunning():
            w.stop()
        else:
            self._worker = None
            self._sync_bot_buttons()
            self._update_worker_status_label()

    def _on_worker_thread_finished(self) -> None:
        if self.sender() is not self._worker:
            return
        self._worker_table_coalesce.stop()
        self._flush_worker_table_pending()
        self._worker = None
        self._sync_bot_buttons()
        self._update_worker_status_label()
        self._log.append("Фон остановлен.")

    

    def _on_fatal(self, msg: str) -> None:
        self._log.append("КРИТИЧНО: " + msg)

    def _on_session_earnings_move(
        self, label: str, gold_delta: int, tickets_delta: int, xp_delta: int
    ) -> None:
        """Счётчики с момента запуска софта; не сбрасываются при остановке фона."""
        self._session_gold_earned[label] += gold_delta
        self._session_tickets_earned[label] += tickets_delta
        self._session_xp_earned[label] += xp_delta

    @staticmethod
    def _format_session_earned_cell(gold: int, tickets: int, xp: int) -> str:
        return f"💰 {gold} · 🎟️ {tickets} · ⭐ {xp}"

    @staticmethod
    def _format_gold_estimate_usd(v: float) -> str:
        """Доллары без принудительного округления до центов — обрезаем лишние нули справа."""
        s = f"{v:.12f}".rstrip("0").rstrip(".")
        return s if s else "0"

    @staticmethod
    def _row_payload_suggests_ban(r: dict) -> bool:
        """API Gamee: User is banned (-32020) — подсветка строки."""
        blob = f'{r.get("status", "")} {r.get("last_error", "")}'.lower()
        if "banned" in blob or "is banned" in blob:
            return True
        if "-32020" in blob:
            return True
        if "забан" in blob:
            return True
        return False

    _BAN_BG_BRUSH = QBrush(QColor(90, 32, 38))
    _BAN_FG_BRUSH = QBrush(QColor(255, 210, 210))
    _CLEAR_BRUSH = QBrush()

    @staticmethod
    def _ban_brushes() -> tuple[QBrush, QBrush]:
        return MainWindow._BAN_BG_BRUSH, MainWindow._BAN_FG_BRUSH

    @staticmethod
    def _paint_item_ban_state(item: QTableWidgetItem | None, banned: bool) -> None:
        if item is None:
            return
        if banned:
            bg = MainWindow._BAN_BG_BRUSH
            fg = MainWindow._BAN_FG_BRUSH
            item.setBackground(bg)
            item.setForeground(fg)
            item.setData(Qt.ItemDataRole.BackgroundRole, bg.color())
            item.setData(Qt.ItemDataRole.ForegroundRole, fg.color())
        else:
            clear = MainWindow._CLEAR_BRUSH
            item.setBackground(clear)
            item.setForeground(clear)
            item.setData(Qt.ItemDataRole.BackgroundRole, None)
            item.setData(Qt.ItemDataRole.ForegroundRole, None)

    @staticmethod
    def _format_energy_cell(energy: int, regen_iso: str | None) -> str:
        """⚡ текущая энергия и в скобках таймер +1 жизни (как «След. +1»)."""
        dl: datetime | None = None
        if regen_iso:
            try:
                dl = datetime.fromisoformat(regen_iso.replace("Z", "+00:00"))
            except ValueError:
                dl = None
        regen = format_next_live_countdown(dl)
        base = f"⚡ {energy}"
        if regen:
            return f"{base} ({regen})"
        return base

    @staticmethod
    def _daily_reward_cell_text(
        note: str,
        iso_raw: str | None,
        streak: int = 0,
        streak_total: int = 0,
    ) -> str:
        dl: datetime | None = None
        if iso_raw:
            try:
                dl = datetime.fromisoformat(iso_raw.replace("Z", "+00:00"))
            except ValueError:
                dl = None
        cd = format_daily_checkin_countdown(dl)
        parts: list[str] = []
        if streak_total > 0:
            parts.append(f"{streak}/{streak_total}")
        n = (note or "").strip()
        if n and n != "—":
            parts.append(n)
        if cd:
            parts.append(cd)
        return " · ".join(parts) if parts else "—"

    @staticmethod
    def _is_ephemeral_claim_note(note: str) -> bool:
        """Текст после успешного клейма ежедневки — показываем ограниченное время."""
        n = (note or "").strip()
        if not n or n in ("—", "можно забрать"):
            return False
        low = n.lower()
        if "ошибка" in low and n.strip().startswith("ежедн"):
            return False
        return True

    def _effective_daily_claim_note(self, label: str, raw_note: str) -> str:
        now = time.monotonic()
        n = (raw_note or "").strip()
        if not self._is_ephemeral_claim_note(n):
            self._daily_claim_flash.pop(label, None)
            return raw_note
        info = self._daily_claim_flash.get(label)
        if info is None or info.get("text") != n:
            self._daily_claim_flash[label] = {
                "text": n,
                "until": now + self._DAILY_CLAIM_FLASH_SEC,
            }
            return n
        if now >= float(info["until"]):
            return ""
        return n

    def _selected_account_label(self) -> str | None:
        row = self._table.currentRow()
        if row < 0:
            return None
        it = self._table.item(row, 0)
        if it is None:
            return None
        s = it.text().strip()
        return s if s else None

    def _update_toolbar_selection_state(self) -> None:
        sel = self._selected_account_label() is not None
        self._btn_delete.setEnabled(sel)
        self._btn_proxy.setEnabled(sel)
        self._sync_bot_buttons()

    def _edit_selected_proxy(self) -> None:
        label = self._selected_account_label()
        if not label:
            return
        self._load_config_silent()
        if self._cfg is None or self._config_error:
            QMessageBox.critical(self, "Конфиг", self._config_error or "Нет конфига")
            return
        path = self._cfg.accounts_path
        try:
            accounts = load_accounts(path)
        except Exception as e:
            QMessageBox.critical(self, "accounts.yaml", str(e))
            return
        acc = next((a for a in accounts if a.label == label), None)
        current = (acc.proxy_url or "") if acc is not None else ""
        dlg = EditAccountProxyDialog(label, current, self._cfg.gamee.api_url, self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        raw = dlg.proxy_input()
        try:
            ok = set_account_proxy_url(path, label, raw if raw else None)
        except OSError as e:
            QMessageBox.critical(self, "Сохранение", str(e))
            return
        if not ok:
            QMessageBox.warning(self, "Прокси", f"Аккаунт «{label}» в файле не найден.")
            return
        w = self._worker
        if w is not None and w.isRunning():
            w.wake_idle(label)
            self._log.append(
                f"Прокси для «{label}» обновлён: подхват при следующем проходе цикла этого аккаунта "
                f"(ожидание между шагами прерывается сигналом; при ошибке/исключении пауза и так короткая, "
                f"но запрос к API, уже начатый, не отменяется до таймаута)."
            )
        else:
            self._log.append(
                f"Прокси для «{label}»: {'обновлён' if raw.strip() else 'сброшен (прямой IP)'}."
            )
        QTimer.singleShot(0, self._fill_table_with_placeholders)

    def _delete_selected_account(self) -> None:
        label = self._selected_account_label()
        if not label:
            return
        self._load_config_silent()
        if self._cfg is None or self._config_error:
            QMessageBox.critical(self, "Конфиг", self._config_error or "Нет конфига")
            return
        ans = QMessageBox.question(
            self,
            "Удалить аккаунт",
            f"Удалить аккаунт «{label}» из софта?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if ans != QMessageBox.StandardButton.Yes:
            return
        path = self._cfg.accounts_path
        removed, session_path = remove_account_by_label(path, label)
        if not removed:
            QMessageBox.warning(
                self,
                "Удаление",
                f"Аккаунт «{label}» в файле не найден (возможно, уже удалён).",
            )
            self._update_toolbar_selection_state()
            QTimer.singleShot(0, self._fill_table_with_placeholders)
            return
        clear_init_cache(label)
        self._regen_meta.pop(label, None)
        self._daily_meta.pop(label, None)
        self._daily_claim_flash.pop(label, None)
        self._known_account_labels.discard(label)
        self._session_gold_earned.pop(label, None)
        self._session_tickets_earned.pop(label, None)
        self._session_xp_earned.pop(label, None)
        w = self._worker
        if w is not None and w.isRunning():
            w.wake_idle(label)
        log = f"Аккаунт «{label}» удалён из accounts.yaml."
        if session_path is not None:
            log += f" Сессия: {session_path.name} (и связанные файлы при наличии)."
        self._log.append(log)
        self._table.clearSelection()
        self._update_toolbar_selection_state()
        QTimer.singleShot(0, self._fill_table_with_placeholders)

    def _tick_regen_cells(self) -> None:
        if self._cfg is None:
            return
        for i in range(self._table.rowCount()):
            li = self._table.item(i, 0)
            if li is None:
                continue
            label = li.text().strip()
            meta = self._regen_meta.get(label)
            if meta is None:
                continue
            iso, energy = meta
            etxt = self._format_energy_cell(energy, iso)
            eit = self._table.item(i, 2)
            if eit is None:
                eit = QTableWidgetItem()
                eit.setTextAlignment(Qt.AlignCenter)
                self._table.setItem(i, 2, eit)
            eit.setText(etxt)
            ban = label in self._banned_row_labels
            self._paint_item_ban_state(eit, ban)
            dmeta = self._daily_meta.get(label)
            if dmeta is not None:
                dnote, diso, d_st, d_sttot = dmeta
                dnote_eff = self._effective_daily_claim_note(label, dnote)
                dtxt = self._daily_reward_cell_text(
                    dnote_eff, diso, d_st, d_sttot
                )
                dit = self._table.item(i, 6)
                if dit is None:
                    dit = QTableWidgetItem()
                    dit.setTextAlignment(Qt.AlignCenter)
                    self._table.setItem(i, 6, dit)
                dit.setText(dtxt)
                self._paint_item_ban_state(dit, ban)
            sit = self._table.item(i, 7)
            if sit is not None:
                self._paint_item_ban_state(sit, ban)

    def _on_table(self, rows: list) -> None:
        try:
            self._on_table_impl(rows)
        except Exception as e:
            self._log.append(
                "Ошибка отрисовки таблицы (интерфейс не закрываем): "
                f"{e}\n{traceback.format_exc()}"
            )

    @staticmethod
    def _ensure_table_item(
        table: QTableWidget, row: int, col: int
    ) -> QTableWidgetItem:
        """Return existing item or create a new one — avoids destroying+recreating C++ wrappers."""
        item = table.item(row, col)
        if item is not None:
            return item
        item = QTableWidgetItem()
        item.setTextAlignment(
            Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter
        )
        item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        table.setItem(row, col, item)
        return item

    @staticmethod
    def _ensure_vh_item(
        table: QTableWidget, row: int
    ) -> QTableWidgetItem:
        item = table.verticalHeaderItem(row)
        if item is not None:
            return item
        item = QTableWidgetItem()
        item.setTextAlignment(
            Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter
        )
        table.setVerticalHeaderItem(row, item)
        return item

    def _on_table_impl(self, rows: list) -> None:
        self._regen_meta.clear()
        self._daily_meta.clear()
        self._banned_row_labels.clear()
        self._last_rows_by_label = {}
        alive = {str(r.get("label", "")).strip() for r in rows if isinstance(r, dict)}
        alive.discard("")
        for k in list(self._daily_claim_flash.keys()):
            if k not in alive:
                self._daily_claim_flash.pop(k, None)
        self._known_account_labels &= alive

        tbl = self._table
        tbl.setUpdatesEnabled(False)
        old_blocked = tbl.blockSignals(True)
        try:
            tbl.setRowCount(len(rows))
            for i, r in enumerate(rows):
                if not isinstance(r, dict):
                    continue
                label = str(r.get("label", ""))
                if label:
                    self._last_rows_by_label[label] = dict(r)
                if label:
                    self._known_account_labels.add(label)

                energy = int(r.get("energy", 0) or 0)
                iso_raw = r.get("regen_deadline_iso")
                iso: str | None = iso_raw if isinstance(iso_raw, str) and iso_raw else None
                self._regen_meta[label] = (iso, energy)
                dc_note = str(r.get("daily_claim_rewards_text", "") or "").strip()
                if not dc_note:
                    dc_note = str(r.get("daily_checkin_note", "") or "").strip()
                dc_iso_r = r.get("daily_checkin_deadline_iso")
                dc_iso: str | None = (
                    dc_iso_r if isinstance(dc_iso_r, str) and dc_iso_r else None
                )
                st_cur = int(r.get("daily_checkin_streak", 0) or 0)
                st_tot = int(r.get("daily_checkin_streak_total", 0) or 0)
                self._daily_meta[label] = (dc_note, dc_iso, st_cur, st_tot)

                energy_text = (
                    self._format_energy_cell(energy, iso)
                    if self._cfg
                    else f"⚡ {energy}"
                )
                if not self._cfg and r.get("regen_eta"):
                    extra = str(r.get("regen_eta", "")).strip()
                    if extra:
                        energy_text = f"{energy_text} ({extra})"
                dc_note_eff = self._effective_daily_claim_note(label, dc_note)
                daily_text = self._daily_reward_cell_text(
                    dc_note_eff, dc_iso, st_cur, st_tot
                )
                season_txt = str(r.get("season_rewards_text", "") or "").strip() or "—"
                proxy_cell = str(r.get("proxy_cell", "") or "").strip() or "—"
                proxy_tip = str(r.get("proxy_tooltip", "") or "").strip()

                gold_i = int(r.get("gold", 0) or 0)
                est: float | None = None
                if self._cfg and gold_i > 0:
                    gm = self._cfg.gamee.gold_micro_divisor
                    ed = self._cfg.gamee.gold_estimate_usd_micro_divisor
                    if gm > 0 and ed > 0:
                        est = (gold_i * gm) / float(ed)
                if est is None:
                    est_raw = r.get("gold_estimated_usd")
                    try:
                        if est_raw is not None:
                            est = float(est_raw)
                    except (TypeError, ValueError):
                        est = None
                if est is not None and est <= 0:
                    est = None
                if est is not None:
                    gold_cell = f"💰 {gold_i} (${self._format_gold_estimate_usd(est)})"
                else:
                    gold_cell = f"💰 {gold_i}"
                vals = [
                    label,
                    proxy_cell,
                    energy_text,
                    gold_cell,
                    str(r.get("status", "")),
                    str(r.get("last_move_at", "")),
                    daily_text,
                    season_txt,
                    self._format_session_earned_cell(
                        self._session_gold_earned[label],
                        self._session_tickets_earned[label],
                        self._session_xp_earned[label],
                    ),
                ]
                banned = self._row_payload_suggests_ban(r)
                if banned and label:
                    self._banned_row_labels.add(label)
                for j, text in enumerate(vals):
                    item = self._ensure_table_item(tbl, i, j)
                    item.setText(text)
                    item.setToolTip("")
                    if j == 1 and proxy_tip:
                        item.setToolTip(proxy_tip)
                    if j == 3 and est is not None:
                        item.setToolTip(
                            f"Оценка конвертации золота в USD (как в приложении prizes.gamee.com): "
                            f"${self._format_gold_estimate_usd(est)}"
                        )
                    self._paint_item_ban_state(item, banned)
                vh = self._ensure_vh_item(tbl, i)
                vh.setText(str(i + 1))
                self._paint_item_ban_state(vh, banned)
        finally:
            tbl.blockSignals(old_blocked)
            tbl.setUpdatesEnabled(True)
        tbl.resizeColumnToContents(3)
        if tbl.columnWidth(3) < 140:
            tbl.setColumnWidth(3, 140)
        self._update_toolbar_selection_state()
        self._update_total_gold_summary(rows)

    def _update_total_gold_summary(self, rows: list) -> None:
        """Сумма золота и USD по всем строкам (данные как в колонке «Золото»)."""
        total_gold = 0
        for r in rows:
            if isinstance(r, dict):
                total_gold += int(r.get("gold", 0) or 0)
        if self._cfg is None:
            self._total_gold_summary.setText(
                f"Всего по аккаунтам: 💰 {total_gold} · оценка USD — (нет конфига)"
            )
            return
        gm = self._cfg.gamee.gold_micro_divisor
        ed = self._cfg.gamee.gold_estimate_usd_micro_divisor
        if total_gold <= 0 or gm <= 0 or ed <= 0:
            self._total_gold_summary.setText(f"Всего по аккаунтам: 💰 {total_gold}")
            return
        total_usd = (total_gold * gm) / float(ed)
        usd_s = self._format_gold_estimate_usd(total_usd)
        self._total_gold_summary.setText(
            f"Всего по аккаунтам: 💰 {total_gold} · ~ ${usd_s}"
        )

    def _wait_thread_finish(self, thread: QThread | None, timeout_ms: int) -> None:
        if thread is None or not thread.isRunning():
            return
        timer = QElapsedTimer()
        timer.start()
        while thread.isRunning() and timer.elapsed() < timeout_ms:
            QApplication.processEvents()
            thread.wait(100)

    def closeEvent(self, event) -> None:
        w = self._worker
        if w is not None and w.isRunning():
            w.stop()
        self._wait_thread_finish(self._worker, 30_000)
        self._worker = None
        if self._manual_action_thread is not None and self._manual_action_thread.isRunning():
            self._manual_action_thread.requestInterruption()
        self._wait_thread_finish(self._manual_action_thread, 30_000)
        self._manual_action_thread = None
        self._wait_thread_finish(self._enter_code_thread, 30_000)
        self._wait_thread_finish(self._enter_draw_thread, 30_000)
        self._enter_draw_thread = None
        super().closeEvent(event)


def run_app(config_path: Path, *, after_qapp: Callable[[], None] | None = None) -> int:
    app = QApplication([])
    if after_qapp is not None:
        after_qapp()
    apply_app_style(app)
    win = MainWindow(config_path)
    win.show()
    return app.exec()
