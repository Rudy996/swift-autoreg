from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, QThread, Signal, Slot
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QApplication,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSlider,
    QSpinBox,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from registration import (
    AppConfig,
    RefPool,
    config_for_run,
    load_config,
    multi_config_from_base,
    run_all,
    run_multi_all,
)

LOG_COLORS = {
    "default": "#1a1a1a",
    "step": "#2563eb",
    "success": "#15803d",
    "error": "#dc2626",
    "info": "#a16207",
}

LOG_STYLESHEET = """
QTextEdit {
    background-color: #ffffff;
    color: #1a1a1a;
    border: 1px solid #d1d5db;
    padding: 8px;
    selection-background-color: #bfdbfe;
}
"""


class RegistrationWorker(QThread):
    log_line = Signal(str, str)
    finished_run = Signal(int, int, int)
    failed_start = Signal(str)

    def __init__(
        self,
        base_cfg: AppConfig,
        ref_code: str,
        cycles: int,
        threads: int,
    ) -> None:
        super().__init__()
        self._base_cfg = base_cfg
        self._ref_code = ref_code
        self._cycles = cycles
        self._threads = threads

    def run(self) -> None:
        try:
            cfg = config_for_run(
                self._base_cfg,
                ref_code=self._ref_code,
                cycles=self._cycles,
                threads=self._threads,
            )
        except ValueError as exc:
            self.failed_start.emit(str(exc))
            return

        def on_line(text: str, kind: str) -> None:
            self.log_line.emit(text, kind)

        result = run_all(cfg, on_line=on_line)
        self.finished_run.emit(result.success, result.failed, result.total)


class MultiRegistrationWorker(QThread):
    log_line = Signal(str, str)
    finished_run = Signal(int, int, int)
    pool_size = Signal(int)
    failed_start = Signal(str)

    def __init__(
        self,
        base_cfg: AppConfig,
        accounts: int,
        chance_without_ref: int,
        chance_with_ref: int,
        threads: int,
    ) -> None:
        super().__init__()
        self._base_cfg = base_cfg
        self._accounts = accounts
        self._chance_without_ref = chance_without_ref
        self._chance_with_ref = chance_with_ref
        self._threads = threads

    def run(self) -> None:
        try:
            cfg = multi_config_from_base(
                self._base_cfg,
                accounts=self._accounts,
                chance_without_ref=self._chance_without_ref,
                chance_with_ref=self._chance_with_ref,
                threads=self._threads,
            )
        except ValueError as exc:
            self.failed_start.emit(str(exc))
            return

        pool = RefPool(Path(self._base_cfg.ref_pool_file))

        def on_line(text: str, kind: str) -> None:
            self.log_line.emit(text, kind)

        def on_pool_size(size: int) -> None:
            self.pool_size.emit(size)

        result = run_multi_all(cfg, pool, on_line=on_line, on_pool_size=on_pool_size)
        self.finished_run.emit(result.success, result.failed, result.total)


class RefRegistrationTab(QWidget):
    def __init__(self, base_cfg: AppConfig, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._base_cfg = base_cfg
        self._worker: Optional[RegistrationWorker] = None

        self.ref_input = QLineEdit()
        self.ref_input.setPlaceholderText("пусто — без рефа")
        if base_cfg.ref_code:
            self.ref_input.setText(base_cfg.ref_code)

        self.cycles_input = QSpinBox()
        self.cycles_input.setRange(1, 1_000_000)
        self.cycles_input.setValue(base_cfg.cycles)

        self.threads_input = QSpinBox()
        self.threads_input.setRange(1, 500)
        self.threads_input.setValue(base_cfg.threads)

        self.start_btn = QPushButton("Старт")
        self.log_view = QTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setFont(QFont("Consolas", 10))
        self.log_view.setStyleSheet(LOG_STYLESHEET)

        form = QFormLayout()
        form.addRow("Реф код", self.ref_input)
        form.addRow("Циклы", self.cycles_input)
        form.addRow("Потоки", self.threads_input)

        actions = QHBoxLayout()
        actions.addWidget(self.start_btn)
        actions.addStretch()

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addLayout(actions)
        layout.addWidget(self.log_view, stretch=1)

        self.start_btn.clicked.connect(self._on_start)

    def _set_running(self, running: bool) -> None:
        self.start_btn.setEnabled(not running)
        self.ref_input.setEnabled(not running)
        self.cycles_input.setEnabled(not running)
        self.threads_input.setEnabled(not running)

    def _append_log(self, text: str, kind: str) -> None:
        color = LOG_COLORS.get(kind, LOG_COLORS["default"])
        safe = (
            text.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )
        self.log_view.append(f'<span style="color:{color};">{safe}</span>')

    @Slot()
    def _on_start(self) -> None:
        if self._worker and self._worker.isRunning():
            return

        self.log_view.clear()
        self._set_running(True)

        self._worker = RegistrationWorker(
            self._base_cfg,
            self.ref_input.text(),
            self.cycles_input.value(),
            self.threads_input.value(),
        )
        self._worker.log_line.connect(self._append_log)
        self._worker.failed_start.connect(self._on_failed_start)
        self._worker.finished_run.connect(self._on_finished)
        self._worker.finished.connect(self._on_worker_done)
        self._worker.start()

    @Slot(str)
    def _on_failed_start(self, message: str) -> None:
        self._append_log(message, "error")
        self._set_running(False)

    @Slot(int, int, int)
    def _on_finished(self, success: int, failed: int, total: int) -> None:
        self._append_log(f"--- Готово: {success}/{total}, ошибок {failed} ---", "info")

    @Slot()
    def _on_worker_done(self) -> None:
        self._set_running(False)
        self._worker = None


class MultiRegistrationTab(QWidget):
    def __init__(self, base_cfg: AppConfig, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._base_cfg = base_cfg
        self._worker: Optional[MultiRegistrationWorker] = None
        self._pool = RefPool(Path(base_cfg.ref_pool_file))

        self.accounts_input = QSpinBox()
        self.accounts_input.setRange(1, 1_000_000)
        self.accounts_input.setValue(100)

        self.chance_slider = QSlider(Qt.Orientation.Horizontal)
        self.chance_slider.setRange(0, 100)
        self.chance_slider.setValue(60)
        self.chance_slider.setTickPosition(QSlider.TickPosition.TicksBelow)
        self.chance_slider.setTickInterval(10)

        self.chance_without_label = QLabel()
        self.chance_with_label = QLabel()
        self._update_chance_labels(60)

        chance_pct_row = QHBoxLayout()
        chance_pct_row.addWidget(self.chance_without_label)
        chance_pct_row.addWidget(self.chance_slider, stretch=1)
        chance_pct_row.addWidget(self.chance_with_label)

        chance_titles_row = QHBoxLayout()
        chance_titles_row.addWidget(QLabel("Без рефа"))
        chance_titles_row.addStretch()
        chance_titles_row.addWidget(QLabel("С рефом"))

        chance_box = QVBoxLayout()
        chance_box.addLayout(chance_titles_row)
        chance_box.addLayout(chance_pct_row)
        chance_widget = QWidget()
        chance_widget.setLayout(chance_box)

        self.threads_input = QSpinBox()
        self.threads_input.setRange(1, 500)
        self.threads_input.setValue(base_cfg.threads)

        self.pool_label = QLabel()
        self._update_pool_label()

        self.start_btn = QPushButton("Старт")
        self.log_view = QTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setFont(QFont("Consolas", 10))
        self.log_view.setStyleSheet(LOG_STYLESHEET)

        form = QFormLayout()
        form.addRow("Аккаунты", self.accounts_input)
        form.addRow("Шансы", chance_widget)
        form.addRow("Потоки", self.threads_input)
        form.addRow("Пул", self.pool_label)

        actions = QHBoxLayout()
        actions.addWidget(self.start_btn)
        actions.addStretch()

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addLayout(actions)
        layout.addWidget(self.log_view, stretch=1)

        self.start_btn.clicked.connect(self._on_start)
        self.chance_slider.valueChanged.connect(self._update_chance_labels)

    def _update_chance_labels(self, with_ref: int) -> None:
        without = 100 - with_ref
        self.chance_without_label.setText(f"{without}%")
        self.chance_with_label.setText(f"{with_ref}%")

    def _update_pool_label(self) -> None:
        self.pool_label.setText(str(self._pool.size()))

    def _set_running(self, running: bool) -> None:
        self.start_btn.setEnabled(not running)
        self.accounts_input.setEnabled(not running)
        self.chance_slider.setEnabled(not running)
        self.threads_input.setEnabled(not running)

    def _append_log(self, text: str, kind: str) -> None:
        color = LOG_COLORS.get(kind, LOG_COLORS["default"])
        safe = (
            text.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )
        self.log_view.append(f'<span style="color:{color};">{safe}</span>')

    @Slot()
    def _on_start(self) -> None:
        if self._worker and self._worker.isRunning():
            return

        with_ref = self.chance_slider.value()
        without = 100 - with_ref

        self.log_view.clear()
        self._set_running(True)

        self._worker = MultiRegistrationWorker(
            self._base_cfg,
            self.accounts_input.value(),
            without,
            with_ref,
            self.threads_input.value(),
        )
        self._worker.log_line.connect(self._append_log)
        self._worker.failed_start.connect(self._on_failed_start)
        self._worker.finished_run.connect(self._on_finished)
        self._worker.pool_size.connect(self._on_pool_size)
        self._worker.finished.connect(self._on_worker_done)
        self._worker.start()

    @Slot(str)
    def _on_failed_start(self, message: str) -> None:
        self._append_log(message, "error")
        self._set_running(False)

    @Slot(int, int, int)
    def _on_finished(self, success: int, failed: int, total: int) -> None:
        self._append_log(f"--- Готово: {success}/{total}, ошибок {failed} ---", "info")

    @Slot(int)
    def _on_pool_size(self, size: int) -> None:
        self.pool_label.setText(str(size))

    @Slot()
    def _on_worker_done(self) -> None:
        self._set_running(False)
        self._worker = None


class MainWindow(QMainWindow):
    def __init__(self, base_cfg: AppConfig) -> None:
        super().__init__()
        self.setWindowTitle("Shift")
        self.resize(720, 520)

        tabs = QTabWidget()
        tabs.addTab(RefRegistrationTab(base_cfg), "Регистрация")
        tabs.addTab(MultiRegistrationTab(base_cfg), "Мульти регистратор")

        self.setCentralWidget(tabs)


def run_gui() -> int:
    try:
        base_cfg = load_config()
    except Exception as exc:
        app = QApplication(sys.argv)
        QMessageBox.critical(None, "Shift", str(exc))
        return 1

    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    window = MainWindow(base_cfg)
    window.show()
    return app.exec()
