"""Единая тёмная тема для главного окна и диалогов."""

APP_STYLESHEET = """
QMainWindow, QDialog {
    background-color: #0f1419;
    color: #e7ecf3;
}
QWidget {
    color: #e7ecf3;
    font-size: 13px;
}
QMenuBar {
    background-color: #1a2332;
    color: #e7ecf3;
    padding: 4px;
    border-bottom: 1px solid #2d3d52;
}
QMenuBar::item {
    background: transparent;
    padding: 6px 12px;
    margin: 0 6px 0 0;
    border-radius: 4px;
}
QMenuBar::item:selected {
    background-color: #2d4a7c;
}
QMenuBar::item:pressed {
    background-color: #243b64;
}
QMenu {
    background-color: #1a2332;
    border: 1px solid #2d3d52;
}
QMenu::item:selected {
    background-color: #3d6fd9;
    color: #ffffff;
}
QPushButton {
    background-color: #3d6fd9;
    color: #ffffff;
    border: none;
    border-radius: 6px;
    padding: 8px 18px;
    font-weight: 600;
    min-height: 20px;
}
QPushButton:hover {
    background-color: #5a87f0;
}
QPushButton:pressed {
    background-color: #2d5ab8;
}
QPushButton:disabled {
    background-color: #3a4556;
    color: #8899aa;
}
QPushButton#btnStop {
    background-color: #c94c4c;
}
QPushButton#btnStop:hover {
    background-color: #e06559;
}
QPushButton#btnStop:pressed {
    background-color: #a33d3d;
}
QTableWidget, QTableView {
    background-color: #151d28;
    /* alternate-background-color здесь затирает фон ячеек (бан, setBackground). */
    gridline-color: #2d3d52;
    border: 1px solid #2d3d52;
    border-radius: 8px;
    selection-background-color: #3d6fd9;
}
/* Ячейка: без сплошного фона в QSS — красим из кода (бан) и палитрой (полосы). */
QTableWidget::item, QTableView::item {
    border: none;
    padding: 6px;
}
QTableWidget::item:selected, QTableView::item:selected {
    background-color: #3d6fd9;
    color: #ffffff;
}
QTableCornerButton::section {
    background-color: #1e2a3d;
    border: none;
}
QHeaderView {
    background-color: #1e2a3d;
}
QHeaderView::section:vertical {
    background-color: #1e2a3d;
    color: #8fa8c4;
    border: none;
    border-right: 1px solid #2d3d52;
    border-bottom: 1px solid #2d3d52;
    padding: 4px 6px;
    font-weight: 600;
    font-size: 12px;
    min-width: 28px;
}
QTableWidget#accountsTable {
    background-color: #151d28;
    border: 1px solid #2d3d52;
    gridline-color: #2d3d52;
}
QTableWidget::indicator, QTableView::indicator {
    width: 18px;
    height: 18px;
}
QLabel#workerStatusRunning {
    color: #6bcf8e;
    font-weight: 700;
    font-size: 13px;
    padding: 4px 0;
}
QLabel#workerStatusStopped {
    color: #7d8fa3;
    font-weight: 600;
    font-size: 13px;
    padding: 4px 0;
}
QPushButton#btnToolbar {
    background-color: #243044;
    color: #c5d4e8;
    border: 1px solid #3d5570;
    font-weight: 600;
    padding: 8px 14px;
}
QPushButton#btnToolbar:hover {
    background-color: #2d415c;
    border-color: #5a7aa6;
    color: #ffffff;
}
QHeaderView::section {
    background-color: #1e2a3d;
    color: #9fb3c8;
    padding: 8px;
    border: none;
    border-bottom: 1px solid #2d3d52;
    font-weight: 600;
}
QTextEdit {
    background-color: #0a0e12;
    border: 1px solid #2d3d52;
    border-radius: 8px;
    padding: 8px;
    color: #c5d4e0;
    font-family: "Consolas", "Cascadia Mono", monospace;
    font-size: 12px;
}
QTabWidget::pane {
    border: 1px solid #2d3d52;
    border-radius: 8px;
    background-color: #151d28;
    top: -1px;
}
QTabBar::tab {
      background-color: #1a2332;
      color: #9fb3c8;
      padding: 10px 18px;
      margin-right: 2px;
      border-top-left-radius: 6px;
      border-top-right-radius: 6px;
}
QTabBar::tab:selected {
    background-color: #3d6fd9;
    color: #ffffff;
    font-weight: 600;
}
QLineEdit, QPlainTextEdit, QSpinBox, QDoubleSpinBox {
    background-color: #0a0e12;
    border: 1px solid #2d3d52;
    border-radius: 6px;
    padding: 8px 10px;
    selection-background-color: #3d6fd9;
}
QComboBox {
    combobox-popup: 0;
    background-color: #0a0e12;
    border: 1px solid #2d3d52;
    border-radius: 6px;
    padding: 8px 10px;
    padding-right: 28px;
    min-height: 20px;
    color: #e7ecf3;
    selection-background-color: #3d6fd9;
    selection-color: #ffffff;
}
QComboBox:hover {
    border-color: #3d5570;
}
QComboBox:focus {
    border-color: #5a7aa6;
}
QComboBox::drop-down {
    subcontrol-origin: padding;
    subcontrol-position: top right;
    width: 26px;
    border: none;
    border-left: 1px solid #2d3d52;
    border-top-right-radius: 5px;
    border-bottom-right-radius: 5px;
    background-color: #1a2332;
}
QComboBox::down-arrow {
    width: 0;
    height: 0;
    border-left: 5px solid transparent;
    border-right: 5px solid transparent;
    border-top: 6px solid #9fb3c8;
    margin-right: 8px;
}
QComboBox QAbstractItemView {
    background-color: #151d28;
    color: #e7ecf3;
    border: 1px solid #2d3d52;
    border-radius: 6px;
    padding: 4px;
    outline: none;
    selection-background-color: #3d6fd9;
    selection-color: #ffffff;
}
QComboBox QAbstractItemView::item {
    min-height: 28px;
    padding: 6px 10px;
    border-radius: 4px;
}
QComboBox QAbstractItemView::item:hover {
    background-color: #243044;
}
QComboBox QAbstractItemView::item:selected {
    background-color: #3d6fd9;
    color: #ffffff;
}
QGroupBox {
    font-weight: 600;
    border: 1px solid #2d3d52;
    border-radius: 8px;
    margin-top: 14px;
    padding-top: 12px;
    background-color: #151d28;
}
QGroupBox::title {
    subcontrol-origin: margin;
    left: 12px;
    padding: 0 6px;
    color: #8ab4f8;
}
QCheckBox {
    spacing: 8px;
}
QCheckBox::indicator {
    width: 18px;
    height: 18px;
    border-radius: 4px;
    border: 1px solid #2d3d52;
    background-color: #0a0e12;
}
QCheckBox::indicator:checked {
    background-color: #3d6fd9;
    border-color: #3d6fd9;
}
QLabel#panelTitle {
    font-size: 18px;
    font-weight: 700;
    color: #ffffff;
    padding: 4px 0 8px 0;
}
QLabel#totalGoldSummary {
    color: #e8c96a;
    font-weight: 600;
    font-size: 14px;
    padding: 0 0 6px 0;
}
QLabel#hintLabel {
    color: #9fb3c8;
    font-size: 12px;
}
QLabel#settingsLead {
    font-size: 15px;
    font-weight: 700;
    color: #f0f4fa;
    padding: 0 0 4px 0;
}
QLabel#settingsMicro {
    color: #7d8fa3;
    font-size: 11px;
    padding: 0 0 12px 0;
}
QGroupBox#settingsCard {
    border: 1px solid #2a3a4f;
    border-radius: 10px;
    margin-top: 18px;
    padding: 16px 16px 14px 16px;
    background-color: #121a24;
}
QGroupBox#settingsCard::title {
    subcontrol-origin: margin;
    left: 14px;
    padding: 0 8px;
    color: #b8d4ff;
    font-weight: 700;
    font-size: 13px;
}
QDialogButtonBox QPushButton {
    min-width: 88px;
}
QFrame#addAccountPanel {
    background-color: #121a24;
    border: 1px solid #2a3a4f;
    border-radius: 14px;
}
QPushButton#btnSecondary {
    background-color: transparent;
    color: #b8d4ff;
    border: 1px solid #3d5570;
    font-weight: 600;
    padding: 10px 20px;
}
QPushButton#btnSecondary:hover {
    background-color: #1a2838;
    color: #e7ecf3;
    border-color: #5a7aa6;
}
QPushButton#btnSecondary:pressed {
    background-color: #152030;
}
QPushButton#btnPrimaryWide {
    padding: 12px 24px;
    font-size: 14px;
    min-height: 24px;
}
QPushButton#btnProxyProbe {
    background-color: #3d6fd9;
    color: #ffffff;
    border: none;
    border-radius: 8px;
    min-width: 34px;
    max-width: 34px;
    min-height: 34px;
    max-height: 34px;
    padding: 0px;
    font-weight: 700;
    font-size: 16px;
}
QPushButton#btnProxyProbe:hover {
    background-color: #5a87f0;
}
QPushButton#btnProxyProbe:pressed {
    background-color: #2d5ab8;
}
QPushButton#btnProxyProbe:disabled {
    background-color: #3a4556;
    color: #8899aa;
}
"""


def apply_app_style(app) -> None:
    from PySide6.QtWidgets import QApplication

    if isinstance(app, QApplication):
        app.setStyleSheet(APP_STYLESHEET)
