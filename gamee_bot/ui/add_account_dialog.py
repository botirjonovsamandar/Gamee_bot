from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QFont, QCloseEvent
from shiboken6 import isValid
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QStackedWidget,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from gamee_bot.account_store import (
    AccountRecord,
    append_account,
    load_accounts,
    safe_account_filename,
)
from gamee_bot.config import AppConfig, resolve_account_gamee_start_param
from gamee_bot.proxy_url import explain_proxy_formats_short
from gamee_bot.telethon_bridge import (
    clear_init_cache,
    parse_init_data_from_webview_url,
    run_telethon_locked,
    telethon_send_code,
    telethon_sign_in_and_fetch_init_data,
)
from gamee_bot.tma_auth import ensure_telegram_mini_app_init_data
from gamee_bot.ui.proxy_probe_thread import ProxyProbeThread


class _AsyncWorkerStr(QThread):
    """Одна залоченная asyncio.run на файл .session; возвращает str."""

    ok = Signal(str)
    fail = Signal(str)

    def __init__(self, session_path: str, coro_factory) -> None:
        super().__init__()
        self._session_path = str(Path(session_path).resolve())
        self._coro_factory = coro_factory

    def run(self) -> None:
        async def main() -> str:
            return await self._coro_factory()

        try:
            r = run_telethon_locked(self._session_path, main())
            self.ok.emit(r)
        except Exception as e:
            self.fail.emit(str(e))


class AddAccountDialog(QDialog):
    def __init__(self, cfg: AppConfig, parent=None) -> None:
        super().__init__(parent)
        self._cfg = cfg
        self._accounts_dir = cfg.accounts_path.parent.resolve()
        self._session_base_path: Path | None = None
        self._phone_code_hash: str | None = None
        self._active_workers: list[QThread] = []
        self._proxy_probe_thread: ProxyProbeThread | None = None
        self.added_label: str | None = None

        self.setWindowTitle("Добавить аккаунт")
        self.setMinimumSize(560, 420)
        self.resize(720, 520)

        root = QVBoxLayout(self)
        root.setSpacing(14)
        root.setContentsMargins(16, 16, 16, 14)
        tabs = QTabWidget()
        tabs.addTab(self._build_telethon_tab(), "Вход по телефону")
        tabs.addTab(self._build_init_tab(), "Готовая строка")
        self._telethon_tab_index = 0
        tabs.currentChanged.connect(self._on_main_tab_changed)
        root.addWidget(tabs)
        self._main_tabs = tabs

        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        bb.rejected.connect(self.reject)
        root.addWidget(bb)

    def _halt_background_for_close(self) -> None:
        """Потоки могли ещё крутиться: не доставлять сигналы в уже закрытый диалог (краш Qt)."""
        for w in list(self._active_workers):
            try:
                w.blockSignals(True)
            except RuntimeError:
                pass
        if self._proxy_probe_thread is not None:
            try:
                self._proxy_probe_thread.blockSignals(True)
            except RuntimeError:
                pass
            self._proxy_probe_thread = None

    def done(self, result: int) -> None:
        self._halt_background_for_close()
        super().done(result)

    def closeEvent(self, event: QCloseEvent) -> None:
        self._halt_background_for_close()
        super().closeEvent(event)

    def _set_busy(self, busy: bool) -> None:
        for w in (
            self._tl_btn_continue,
            self._tl_btn_back,
            self._btn_signin_save,
            self._init_save,
            self._tl_gamee_ref,
            self._tl_telegram_referral_ref,
            self._tl_proxy,
            self._init_proxy,
            self._btn_init_proxy_test,
            self._btn_tl_proxy_test,
        ):
            w.setEnabled(not busy)

    def _on_main_tab_changed(self, index: int) -> None:
        if index != self._telethon_tab_index:
            self._reset_telethon_wizard()

    def _reset_telethon_wizard(self) -> None:
        self._tl_stack.setCurrentIndex(0)
        self._phone_code_hash = None
        self._session_base_path = None
        self._tl_code.clear()
        self._tl_password.clear()
        self._tl_status.clear()

    def _build_init_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(4, 10, 4, 4)
        lay.setSpacing(14)

        card = QFrame()
        card.setObjectName("addAccountPanel")
        inner = QVBoxLayout(card)
        inner.setContentsMargins(22, 22, 22, 22)
        inner.setSpacing(16)

        self._init_label = QLineEdit()
        self._init_label.setPlaceholderText("Название в таблице")
        self._init_label.setMinimumHeight(40)

        self._init_data = QPlainTextEdit()
        self._init_data.setPlaceholderText("Вставьте одну длинную строку целиком")
        self._init_data.setMinimumHeight(260)
        mono = QFont("Cascadia Mono", 10)
        if not mono.exactMatch():
            mono = QFont("Consolas", 10)
        self._init_data.setFont(mono)

        form = QFormLayout()
        form.setSpacing(14)
        form.setHorizontalSpacing(12)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        name_l = QLabel("Аккаунт")
        name_l.setObjectName("hintLabel")
        form.addRow(name_l, self._init_label)
        data_l = QLabel("Строка")
        data_l.setObjectName("hintLabel")
        form.addRow(data_l, self._init_data)
        px_l = QLabel("Прокси (Gamee)")
        px_l.setObjectName("hintLabel")
        self._init_proxy = QLineEdit()
        self._init_proxy.setPlaceholderText("Пусто = прямой IP · см. форматы ниже")
        self._init_proxy.setMinimumHeight(36)
        init_px_row = QWidget()
        ipx_lay = QHBoxLayout(init_px_row)
        ipx_lay.setContentsMargins(0, 0, 0, 0)
        ipx_lay.setAlignment(Qt.AlignmentFlag.AlignVCenter)
        ipx_lay.addWidget(self._init_proxy, 1)
        self._btn_init_proxy_test = QPushButton("✓")
        self._btn_init_proxy_test.setObjectName("btnProxyProbe")
        self._btn_init_proxy_test.setFixedSize(34, 34)
        self._btn_init_proxy_test.setToolTip("Проверить прокси: запрос к API Gamee (как у бота)")
        self._btn_init_proxy_test.clicked.connect(self._on_test_init_proxy)
        ipx_lay.addWidget(self._btn_init_proxy_test)
        form.addRow(px_l, init_px_row)
        self._init_proxy_status = QLabel("")
        self._init_proxy_status.setWordWrap(True)
        self._init_proxy_status.setObjectName("hintLabel")
        form.addRow("", self._init_proxy_status)
        px_fmt = QLabel(explain_proxy_formats_short())
        px_fmt.setWordWrap(True)
        px_fmt.setObjectName("hintLabel")
        form.addRow("", px_fmt)
        inner.addLayout(form)

        lay.addWidget(card)

        self._init_save = QPushButton("Сохранить аккаунт")
        self._init_save.setObjectName("btnPrimaryWide")
        self._init_save.setMinimumHeight(44)
        self._init_save.clicked.connect(self._save_init_tab)
        lay.addWidget(self._init_save)
        lay.addStretch()
        return w

    def _normalize_pasted_init(self, text: str) -> str:
        text = text.strip()
        # Скопировали из чата/JSON с обрамляющими "…" — убираем одну пару, иначе логин сломается.
        if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
            text = text[1:-1].strip()
        if len(text) >= 2 and text.startswith("\u201c") and text.endswith("\u201d"):
            text = text[1:-1].strip()
        if not text:
            return ""
        extracted = parse_init_data_from_webview_url(text)
        if extracted:
            return extracted
        return text

    def _build_telethon_tab(self) -> QWidget:
        outer = QWidget()
        outer_lay = QVBoxLayout(outer)
        outer_lay.setContentsMargins(4, 10, 4, 4)
        outer_lay.setSpacing(0)

        self._tl_stack = QStackedWidget()

        # Шаг 1
        step1 = QWidget()
        s1 = QVBoxLayout(step1)
        s1.setContentsMargins(0, 0, 0, 0)
        s1.setSpacing(14)

        p1 = QFrame()
        p1.setObjectName("addAccountPanel")
        b1 = QVBoxLayout(p1)
        b1.setContentsMargins(24, 24, 24, 24)
        b1.setSpacing(18)

        t1 = QLabel("Вход в Telegram")
        t1.setObjectName("settingsLead")
        b1.addWidget(t1)

        form1 = QFormLayout()
        form1.setSpacing(14)
        form1.setHorizontalSpacing(14)
        self._tl_label = QLineEdit()
        self._tl_label.setPlaceholderText("Название в таблице")
        self._tl_label.setMinimumHeight(38)
        self._tl_phone = QLineEdit()
        self._tl_phone.setPlaceholderText("+1 5551234567")
        self._tl_phone.setToolTip(
            "Номер с кодом страны. Плюс можно не писать, пробелы и скобки не мешают."
        )
        self._tl_phone.setMinimumHeight(38)
        l_a = QLabel("Аккаунт")
        l_a.setObjectName("hintLabel")
        l_p = QLabel("Телефон")
        l_p.setObjectName("hintLabel")
        form1.addRow(l_a, self._tl_label)
        form1.addRow(l_p, self._tl_phone)
        b1.addLayout(form1)

        self._tl_btn_continue = QPushButton("Отправить код")
        self._tl_btn_continue.setObjectName("btnPrimaryWide")
        self._tl_btn_continue.setMinimumHeight(44)
        self._tl_btn_continue.clicked.connect(self._on_send_code)
        b1.addWidget(self._tl_btn_continue)

        s1.addWidget(p1)

        p_opt = QFrame()
        p_opt.setObjectName("addAccountPanel")
        b_opt = QVBoxLayout(p_opt)
        b_opt.setContentsMargins(24, 24, 24, 24)
        b_opt.setSpacing(14)

        t_opt = QLabel("Опционально")
        t_opt.setObjectName("settingsLead")
        b_opt.addWidget(t_opt)
        opt_sub = QLabel(
            "Реферальная ссылка Gamee, User ID в Telegram для рефа и прокси к API игры — "
            "можно не указывать. Пустая реф-ссылка берётся из общих настроек."
        )
        opt_sub.setObjectName("settingsMicro")
        opt_sub.setWordWrap(True)
        b_opt.addWidget(opt_sub)

        form_opt = QFormLayout()
        form_opt.setSpacing(14)
        form_opt.setHorizontalSpacing(14)
        self._tl_gamee_ref = QLineEdit()
        self._tl_gamee_ref.setPlaceholderText("Ссылка t.me/gamee/start?… или пусто")
        self._tl_gamee_ref.setMinimumHeight(38)
        l_r = QLabel("Реф-ссылка Gamee")
        l_r.setObjectName("hintLabel")
        form_opt.addRow(l_r, self._tl_gamee_ref)
        self._tl_telegram_referral_ref = QLineEdit()
        self._tl_telegram_referral_ref.setPlaceholderText("Только цифры, например 123456789")
        self._tl_telegram_referral_ref.setMinimumHeight(38)
        l_tid = QLabel("User ID в Telegram")
        l_tid.setObjectName("hintLabel")
        form_opt.addRow(l_tid, self._tl_telegram_referral_ref)

        self._tl_proxy = QLineEdit()
        self._tl_proxy.setPlaceholderText("Пусто = прямой IP · host:port:user:pass и др.")
        self._tl_proxy.setMinimumHeight(38)
        l_px = QLabel("Прокси (Gamee API)")
        l_px.setObjectName("hintLabel")
        tl_px_row = QWidget()
        tpx_lay = QHBoxLayout(tl_px_row)
        tpx_lay.setContentsMargins(0, 0, 0, 0)
        tpx_lay.setAlignment(Qt.AlignmentFlag.AlignVCenter)
        tpx_lay.addWidget(self._tl_proxy, 1)
        self._btn_tl_proxy_test = QPushButton("✓")
        self._btn_tl_proxy_test.setObjectName("btnProxyProbe")
        self._btn_tl_proxy_test.setFixedSize(34, 34)
        self._btn_tl_proxy_test.setToolTip("Проверить прокси: запрос к API Gamee (как у бота)")
        self._btn_tl_proxy_test.clicked.connect(self._on_test_tl_proxy)
        tpx_lay.addWidget(self._btn_tl_proxy_test)
        form_opt.addRow(l_px, tl_px_row)
        self._tl_proxy_status = QLabel("")
        self._tl_proxy_status.setWordWrap(True)
        self._tl_proxy_status.setObjectName("hintLabel")
        form_opt.addRow("", self._tl_proxy_status)
        tl_px_fmt = QLabel(explain_proxy_formats_short())
        tl_px_fmt.setWordWrap(True)
        tl_px_fmt.setObjectName("hintLabel")
        form_opt.addRow("", tl_px_fmt)
        b_opt.addLayout(form_opt)

        s1.addWidget(p_opt)

        self._tl_stack.addWidget(step1)

        # Шаг 2
        step2 = QWidget()
        s2 = QVBoxLayout(step2)
        s2.setContentsMargins(0, 0, 0, 0)
        p2 = QFrame()
        p2.setObjectName("addAccountPanel")
        b2 = QVBoxLayout(p2)
        b2.setContentsMargins(24, 24, 24, 24)
        b2.setSpacing(18)

        t2 = QLabel("Проверка")
        t2.setObjectName("settingsLead")
        b2.addWidget(t2)

        form2 = QFormLayout()
        form2.setSpacing(14)
        form2.setHorizontalSpacing(14)
        self._tl_code = QLineEdit()
        self._tl_code.setPlaceholderText("Код из Telegram")
        self._tl_code.setMinimumHeight(38)
        self._tl_password = QLineEdit()
        self._tl_password.setEchoMode(QLineEdit.EchoMode.Password)
        self._tl_password.setPlaceholderText("Пароль 2FA, если есть")
        self._tl_password.setMinimumHeight(38)
        l_c = QLabel("Код")
        l_c.setObjectName("hintLabel")
        l_pw = QLabel("2FA")
        l_pw.setObjectName("hintLabel")
        form2.addRow(l_c, self._tl_code)
        form2.addRow(l_pw, self._tl_password)
        b2.addLayout(form2)

        row2 = QHBoxLayout()
        row2.setSpacing(12)
        self._tl_btn_back = QPushButton("Назад")
        self._tl_btn_back.setObjectName("btnSecondary")
        self._tl_btn_back.setMinimumHeight(42)
        self._tl_btn_back.clicked.connect(self._telethon_go_step1)
        self._btn_signin_save = QPushButton("Войти и сохранить")
        self._btn_signin_save.setObjectName("btnPrimaryWide")
        self._btn_signin_save.setMinimumHeight(42)
        self._btn_signin_save.clicked.connect(self._on_signin_save)
        row2.addWidget(self._tl_btn_back)
        row2.addStretch()
        row2.addWidget(self._btn_signin_save)
        b2.addLayout(row2)

        s2.addWidget(p2)
        self._tl_stack.addWidget(step2)

        outer_lay.addWidget(self._tl_stack)

        self._tl_sess_info = QLabel("")
        self._tl_sess_info.setWordWrap(True)
        self._tl_sess_info.setObjectName("hintLabel")
        outer_lay.addWidget(self._tl_sess_info)

        self._tl_status = QLabel("")
        self._tl_status.setWordWrap(True)
        self._tl_status.setObjectName("hintLabel")
        outer_lay.addWidget(self._tl_status)

        return outer

    def _telethon_go_step1(self) -> None:
        self._phone_code_hash = None
        self._tl_code.clear()
        self._tl_password.clear()
        self._tl_stack.setCurrentIndex(0)
        self._tl_status.clear()

    def _session_path_for_label(self) -> Path | None:
        label = self._tl_label.text().strip()
        if not label:
            QMessageBox.warning(self, "Аккаунт", "Введите название аккаунта.")
            return None
        try:
            accounts = load_accounts(self._cfg.accounts_path)
        except Exception:
            accounts = []
        for acc in accounts:
            if acc.label.strip().casefold() == label.casefold():
                QMessageBox.warning(self, "Аккаунт", f"Аккаунт «{label}» уже существует.")
                return None
        sess_dir = self._accounts_dir / "sessions"
        sess_dir.mkdir(parents=True, exist_ok=True)
        fn = safe_account_filename(label)
        want = (sess_dir / f"{fn}.session").resolve()
        for acc in accounts:
            if not acc.telethon_session:
                continue
            cur = Path(acc.telethon_session)
            if not cur.is_absolute():
                cur = (self._accounts_dir / cur).resolve()
            else:
                cur = cur.resolve()
            if cur == want:
                QMessageBox.warning(
                    self,
                    "Сессия",
                    f"Файл сессии {want.name} уже используется аккаунтом «{acc.label}».",
                )
                return None
        return want

    def _wire_worker_str(self, w: _AsyncWorkerStr, on_ok, on_fail) -> None:
        self._active_workers.append(w)

        def cleanup() -> None:
            if w in self._active_workers:
                self._active_workers.remove(w)

        def wrapped_ok(s: str) -> None:
            cleanup()
            if not isValid(self):
                return
            on_ok(s)

        def wrapped_fail(m: str) -> None:
            cleanup()
            if not isValid(self):
                return
            on_fail(m)

        w.ok.connect(wrapped_ok)
        w.fail.connect(wrapped_fail)
        w.start()

    def _on_proxy_probe_finished(
        self, ok: bool, msg: str, status: QLabel, btn: QPushButton
    ) -> None:
        if not isValid(self) or not isValid(status) or not isValid(btn):
            return
        btn.setEnabled(True)
        self._proxy_probe_thread = None
        if ok:
            status.setText("✓ " + msg)
            status.setStyleSheet("color: #6bcf8e;")
        else:
            status.setText("✗ " + msg)
            status.setStyleSheet("color: #e8a0a0;")

    def _run_proxy_probe(self, raw: str, status: QLabel, btn: QPushButton) -> None:
        if self._proxy_probe_thread is not None and self._proxy_probe_thread.isRunning():
            return
        btn.setEnabled(False)
        status.setStyleSheet("")
        status.setText("Проверка прокси…")
        th = ProxyProbeThread(self._cfg.gamee.api_url, raw, self)
        self._proxy_probe_thread = th
        th.finished_probe.connect(
            lambda o, m, st=status, b=btn: self._on_proxy_probe_finished(o, m, st, b)
        )
        th.start()

    def _on_test_init_proxy(self) -> None:
        raw = self._init_proxy.text().strip()
        if not raw:
            self._init_proxy_status.setStyleSheet("color: #e8a0a0;")
            self._init_proxy_status.setText("Пустое поле — прямое соединение без прокси (нечего проверять).")
            return
        self._run_proxy_probe(raw, self._init_proxy_status, self._btn_init_proxy_test)

    def _on_test_tl_proxy(self) -> None:
        raw = self._tl_proxy.text().strip()
        if not raw:
            self._tl_proxy_status.setStyleSheet("color: #e8a0a0;")
            self._tl_proxy_status.setText("Пустое поле — прямое соединение без прокси (нечего проверять).")
            return
        self._run_proxy_probe(raw, self._tl_proxy_status, self._btn_tl_proxy_test)

    def _on_send_code(self) -> None:
        t = self._cfg.telethon
        api_id, api_hash = t.api_id, t.api_hash
        sp = self._session_path_for_label()
        if sp is None:
            return
        phone = self._tl_phone.text().strip()
        if not phone:
            QMessageBox.warning(self, "Телефон", "Введите номер телефона.")
            return
        self._session_base_path = sp
        self._phone_code_hash = None
        self._tl_sess_info.setText(f"Сессия: {sp.name}")
        if sp.exists() and sp.stat().st_size > 0:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            bak = sp.parent / f"{sp.name}.bak_{ts}"
            try:
                shutil.copy2(sp, bak)
            except OSError:
                pass
        self._set_busy(True)
        self._tl_status.setText("Отправка кода…")

        sp_resolved = str(sp.resolve())

        def make_send():
            return telethon_send_code(sp_resolved, api_id, api_hash, phone)

        def done_ok(phone_hash: str) -> None:
            self._phone_code_hash = phone_hash
            self._set_busy(False)
            self._tl_stack.setCurrentIndex(1)
            self._tl_code.clear()
            self._tl_password.clear()
            self._tl_code.setFocus()
            self._tl_status.clear()

        def done_fail(m: str) -> None:
            self._set_busy(False)
            QMessageBox.critical(self, "Отправка кода", m)

        self._wire_worker_str(_AsyncWorkerStr(sp_resolved, make_send), done_ok, done_fail)

    def _on_signin_save(self) -> None:
        sp = self._session_base_path or self._session_path_for_label()
        if sp is None:
            return
        self._session_base_path = sp
        phone = self._tl_phone.text().strip()
        code = self._tl_code.text().strip()
        label = self._tl_label.text().strip()
        if not phone or not code:
            QMessageBox.warning(self, "Вход", "Нужны телефон и код.")
            return
        if not self._phone_code_hash:
            QMessageBox.warning(
                self,
                "Вход",
                "Сначала на шаге с телефоном нажмите «Отправить код».",
            )
            return
        pwd = self._tl_password.text().strip()
        password = pwd if pwd else None
        self._set_busy(True)
        self._tl_status.setText("Вход в Telegram и проверка Gamee…")

        sp_resolved = str(sp.resolve())

        def make_login_fetch():
            ref_raw = self._tl_gamee_ref.text().strip() or None
            sp_eff = resolve_account_gamee_start_param(
                self._cfg, ref_raw, inherit_global_if_empty=True
            )
            return telethon_sign_in_and_fetch_init_data(
                sp_resolved,
                self._cfg.telethon,
                phone,
                code,
                self._phone_code_hash,
                password=password,
                gamee_start_param=sp_eff,
            )

        def done_ok(_init_data: str) -> None:
            try:
                rel = sp.relative_to(self._accounts_dir).as_posix()
            except ValueError:
                rel = str(sp)
            try:
                payload: dict = {"label": label, "telethon_session": rel}
                ref_save = self._tl_gamee_ref.text().strip()
                if ref_save:
                    payload["gamee_ref"] = ref_save
                elif self._cfg.telethon.gamee_start_param:
                    payload["gamee_ref"] = self._cfg.telethon.gamee_start_param
                tr_line = self._tl_telegram_referral_ref.text().strip()
                if tr_line:
                    try:
                        tr_id = int(tr_line)
                        if tr_id <= 0:
                            raise ValueError
                        payload["telegram_referral_ref"] = tr_id
                    except ValueError:
                        self._set_busy(False)
                        QMessageBox.warning(
                            self,
                            "User ID в Telegram",
                            "Нужно целое число или пустое поле.",
                        )
                        return
                elif self._cfg.telethon.telegram_referral_ref is not None:
                    payload["telegram_referral_ref"] = int(
                        self._cfg.telethon.telegram_referral_ref
                    )
                tl_px = self._tl_proxy.text().strip()
                if tl_px:
                    payload["proxy_url"] = tl_px
                rec = AccountRecord.from_dict(payload, 0, self._accounts_dir)
                append_account(self._cfg.accounts_path, rec)
            except Exception as e:
                self._set_busy(False)
                QMessageBox.critical(self, "accounts.yaml", str(e))
                return
            clear_init_cache(label)
            self._set_busy(False)
            QMessageBox.information(
                self,
                "Готово",
                f"Аккаунт «{label}» записан.\nСессия: {rel}",
            )
            self.added_label = label
            self.accept()

        def done_fail(m: str) -> None:
            self._set_busy(False)
            QMessageBox.critical(self, "Вход / Gamee", m)

        self._wire_worker_str(_AsyncWorkerStr(sp_resolved, make_login_fetch), done_ok, done_fail)

    def _save_init_tab(self) -> None:
        label = self._init_label.text().strip()
        if not label:
            QMessageBox.warning(self, "Аккаунт", "Введите название аккаунта.")
            return

        raw = self._normalize_pasted_init(self._init_data.toPlainText())
        if not raw:
            QMessageBox.warning(self, "Данные", "Вставьте строку init_data.")
            return
        try:
            raw = ensure_telegram_mini_app_init_data(raw)
        except ValueError as e:
            QMessageBox.warning(self, "Данные", f"Некорректный Telegram Mini App initData: {e}")
            return
        if "=" not in raw:
            QMessageBox.warning(self, "Данные", "Строка должна содержать параметры вида user=…&hash=…")
            return
        if "user=" not in raw or "hash=" not in raw:
            ans = QMessageBox.question(
                self,
                "Проверка",
                "Обычно в строке есть фрагменты user= и hash=. Сохранить всё равно?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if ans != QMessageBox.StandardButton.Yes:
                return

        try:
            init_payload: dict = {"label": label, "init_data": raw}
            ipx = self._init_proxy.text().strip()
            if ipx:
                init_payload["proxy_url"] = ipx
            rec = AccountRecord.from_dict(
                init_payload,
                0,
                self._accounts_dir,
            )
            append_account(self._cfg.accounts_path, rec)
        except Exception as e:
            QMessageBox.critical(self, "Ошибка", str(e))
            return
        clear_init_cache(label)
        self.added_label = label
        QMessageBox.information(self, "Готово", f"Аккаунт «{label}» добавлен.")
        self.accept()
