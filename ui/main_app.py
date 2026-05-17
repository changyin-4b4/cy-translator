import os
import sys
from datetime import datetime

from PySide6.QtCore import Qt
from PySide6.QtGui import QCursor, QIcon
import json
from pathlib import Path

from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from services.config_store import (
    load_config, save_config,
    get_or_create_pdf_history_entry, set_pdf_config_path,
)
from ui.pdf_viewer import PDFViewer
from ui.reader_tab import ReaderTab

SIZE_PRESETS = ["1920x1080", "2560x1440", "3840x2160"]
RIGHT_PANEL_WIDTH = 500


class _PerPdfConfigDialog(QDialog):
    """Per-PDF config panel: cache file and isolate file paths."""

    def __init__(self, config: dict, pdf_path: str, parent=None):
        super().__init__(parent)
        self._config = config
        self._pdf_path = pdf_path
        entry = get_or_create_pdf_history_entry(config, pdf_path)
        cfg = entry.get("config", {})

        self.setWindowTitle(f"PDF 配置 — {os.path.basename(pdf_path)}")
        self.setMinimumWidth(560)

        layout = QVBoxLayout(self)

        layout.addWidget(QLabel(f"<b>PDF:</b> {pdf_path}"))

        # ── Cache file row ──
        cache_layout = QHBoxLayout()
        cache_layout.addWidget(QLabel("缓存文件:"))
        self._cache_edit = QLineEdit()
        self._cache_edit.setText(cfg.get("cache_file") or "")
        self._cache_edit.textChanged.connect(self._validate_cache)
        cache_layout.addWidget(self._cache_edit)
        cache_browse = QPushButton("...")
        cache_browse.clicked.connect(
            lambda: self._browse_file(self._cache_edit))
        cache_layout.addWidget(cache_browse)
        cache_clear = QPushButton("清除绑定")
        cache_clear.clicked.connect(
            lambda: self._reset_path(self._cache_edit))
        cache_layout.addWidget(cache_clear)
        layout.addLayout(cache_layout)
        self._cache_status = QLabel("")
        layout.addWidget(self._cache_status)

        # ── Isolate file row ──
        isolate_layout = QHBoxLayout()
        isolate_layout.addWidget(QLabel("隔离文件:"))
        self._isolate_edit = QLineEdit()
        self._isolate_edit.setText(cfg.get("isolate_file") or "")
        self._isolate_edit.textChanged.connect(self._validate_isolate)
        isolate_layout.addWidget(self._isolate_edit)
        isolate_browse = QPushButton("...")
        isolate_browse.clicked.connect(
            lambda: self._browse_file(self._isolate_edit))
        isolate_layout.addWidget(isolate_browse)
        isolate_clear = QPushButton("清除绑定")
        isolate_clear.clicked.connect(
            lambda: self._reset_path(self._isolate_edit))
        isolate_layout.addWidget(isolate_clear)
        layout.addLayout(isolate_layout)
        self._isolate_status = QLabel("")
        layout.addWidget(self._isolate_status)

        # ── Note file row ──
        note_layout = QHBoxLayout()
        note_layout.addWidget(QLabel("笔记文件:"))
        self._note_edit = QLineEdit()
        self._note_edit.setText(cfg.get("note_file") or "")
        note_layout.addWidget(self._note_edit)
        note_browse = QPushButton("...")
        note_browse.clicked.connect(
            lambda: self._browse_file(self._note_edit))
        note_layout.addWidget(note_browse)
        note_clear = QPushButton("清除绑定")
        note_clear.clicked.connect(
            lambda: self._reset_path(self._note_edit))
        note_layout.addWidget(note_clear)
        layout.addLayout(note_layout)
        self._note_status = QLabel("")
        layout.addWidget(self._note_status)

        # ── Buttons ──
        btn_layout = QHBoxLayout()
        save_btn = QPushButton("保存")
        save_btn.clicked.connect(self._save_and_accept)
        btn_layout.addWidget(save_btn)
        cancel_btn = QPushButton("取消")
        cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(cancel_btn)
        btn_layout.addStretch()
        layout.addLayout(btn_layout)

        self._validate_cache()
        self._validate_isolate()

    def _browse_file(self, edit: QLineEdit):
        path, _ = QFileDialog.getOpenFileName(
            self, "选择 JSON 文件", "", "JSON 文件 (*.json);;所有文件 (*)",
        )
        if path:
            edit.setText(path)

    def _reset_path(self, edit: QLineEdit):
        edit.clear()

    def _validate_cache(self):
        filepath = self._cache_edit.text().strip()
        if not filepath:
            self._cache_status.setText("[未设置 — 将在首次使用时自动创建]")
            self._cache_status.setStyleSheet("color: gray;")
            return
        p = Path(filepath)
        if not p.exists():
            self._cache_status.setText("[文件不存在 — 将在首次使用时创建]")
            self._cache_status.setStyleSheet("color: orange;")
            return
        if p.suffix.lower() != ".json":
            self._cache_status.setText("[警告：文件后缀不是 .json]")
            self._cache_status.setStyleSheet("color: orange;")
            return
        try:
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            self._cache_status.setText("[错误：文件损坏或无法读取]")
            self._cache_status.setStyleSheet("color: red;")
            return
        if not isinstance(data, dict):
            self._cache_status.setText("[错误：根类型无效，应为 JSON 对象]")
            self._cache_status.setStyleSheet("color: red;")
            return
        for key, entry in data.items():
            if key == "format_version":
                continue
            if not isinstance(entry, dict):
                self._cache_status.setText(f"[错误：条目 {key} 类型无效]")
                self._cache_status.setStyleSheet("color: red;")
                return
            for gk in ("single", "dual"):
                if gk not in entry:
                    self._cache_status.setText(f"[错误：条目 {key} 缺少分组 {gk}]")
                    self._cache_status.setStyleSheet("color: red;")
                    return
                group = entry[gk]
                if not isinstance(group, dict):
                    self._cache_status.setText(f"[错误：条目 {key} 分组 {gk} 类型无效]")
                    self._cache_status.setStyleSheet("color: red;")
                    return
                for sk in ("phrases", "sentences"):
                    if sk not in group:
                        self._cache_status.setText(f"[错误：条目 {key} 分组 {gk} 缺少字段 {sk}]")
                        self._cache_status.setStyleSheet("color: red;")
                        return
                    if not isinstance(group[sk], list):
                        self._cache_status.setText(f"[错误：条目 {key} 分组 {gk} 字段 {sk} 应为数组]")
                        self._cache_status.setStyleSheet("color: red;")
                        return
        self._cache_status.setText("[有效]")
        self._cache_status.setStyleSheet("color: green;")

    def _validate_isolate(self):
        filepath = self._isolate_edit.text().strip()
        if not filepath:
            self._isolate_status.setText("[未设置 — 将在首次使用时自动创建]")
            self._isolate_status.setStyleSheet("color: gray;")
            return
        p = Path(filepath)
        if not p.exists():
            self._isolate_status.setText("[文件不存在 — 将在首次使用时创建]")
            self._isolate_status.setStyleSheet("color: orange;")
            return
        if p.suffix.lower() != ".json":
            self._isolate_status.setText("[警告：文件后缀不是 .json]")
            self._isolate_status.setStyleSheet("color: orange;")
            return
        try:
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            self._isolate_status.setText("[错误：文件损坏或无法读取]")
            self._isolate_status.setStyleSheet("color: red;")
            return
        if not isinstance(data, list):
            self._isolate_status.setText("[错误：隔离文件应为 JSON 数组]")
            self._isolate_status.setStyleSheet("color: red;")
            return
        for z in data:
            if not isinstance(z, dict):
                self._isolate_status.setText("[错误：隔离域条目格式无效]")
                self._isolate_status.setStyleSheet("color: red;")
                return
            for k in ("page", "x0", "y0", "x1", "y1"):
                if k not in z:
                    self._isolate_status.setText(f"[错误：隔离域缺少必要字段 {k}]")
                    self._isolate_status.setStyleSheet("color: red;")
                    return
        self._isolate_status.setText("[有效]")
        self._isolate_status.setStyleSheet("color: green;")

    def _save_and_accept(self):
        cache_val = self._cache_edit.text().strip() or None
        isolate_val = self._isolate_edit.text().strip() or None
        note_val = self._note_edit.text().strip() or None
        set_pdf_config_path(self._config, self._pdf_path,
                            "cache_file", cache_val)
        set_pdf_config_path(self._config, self._pdf_path,
                            "isolate_file", isolate_val)
        set_pdf_config_path(self._config, self._pdf_path,
                            "note_file", note_val)
        save_config(self._config)
        self.accept()


class PdfHistoryDialog(QDialog):
    """First-level menu for selecting a PDF from history or adding a new one."""

    def __init__(self, config: dict, parent=None):
        super().__init__(parent)
        self._config = config
        self._result_path: str | None = None

        self.setWindowTitle("选择PDF文件")
        self.setMinimumWidth(520)
        self.setMinimumHeight(360)

        layout = QVBoxLayout(self)

        self._list = QListWidget()
        self._list.itemDoubleClicked.connect(self._on_double_click)
        layout.addWidget(self._list, 1)

        add_btn = QPushButton("添加")
        add_btn.clicked.connect(self._on_add)
        layout.addWidget(add_btn)

        self._populate()

    def _populate(self):
        self._list.clear()
        history: list = self._config.get("pdf_history", [])
        for entry in history:
            if isinstance(entry, str):
                path = entry
                date_str = ""
            else:
                path = entry.get("path", "")
                date_str = entry.get("date", "")
            # Parse "YYYY-MM-DD-HH-MM-SS" → "YYYY-MM-DD\nHH:MM:SS"
            parts = date_str.split("-")
            if len(parts) == 6:
                display_date = (
                    f"{parts[0]}-{parts[1]}-{parts[2]}\n"
                    f"{parts[3]}:{parts[4]}:{parts[5]}"
                )
            else:
                display_date = date_str

            item = QListWidgetItem()
            item.setText(f"{os.path.basename(path)}\n{display_date}")
            item.setToolTip(path)
            item.setData(Qt.ItemDataRole.UserRole, path)
            self._list.addItem(item)

            # Config + Delete buttons per row
            row_widget = QWidget()
            row_layout = QHBoxLayout(row_widget)
            row_layout.setContentsMargins(0, 0, 4, 0)
            row_layout.addStretch()

            cfg_btn = QPushButton("...")
            cfg_btn.setFixedSize(28, 24)
            cfg_btn.setToolTip("配置（缓存文件、隔离文件）")
            cfg_btn.clicked.connect(
                lambda checked, p=path: self._open_config(p)
            )
            row_layout.addWidget(cfg_btn)

            del_btn = QPushButton("X")
            del_btn.setFixedSize(24, 24)
            del_btn.setStyleSheet("color: red; font-weight: bold;")
            del_btn.clicked.connect(
                lambda checked, p=path: self._on_delete(p)
            )
            row_layout.addWidget(del_btn)
            self._list.setItemWidget(item, row_widget)

    def _open_config(self, path: str):
        dlg = _PerPdfConfigDialog(self._config, path, self)
        dlg.exec()

    def _on_delete(self, path: str):
        history: list = self._config.get("pdf_history", [])
        self._config["pdf_history"] = [
            e for e in history
            if (e if isinstance(e, str) else e.get("path")) != path
        ]
        save_config(self._config)
        self._populate()

    def _on_double_click(self, item: QListWidgetItem):
        self._result_path = item.data(Qt.ItemDataRole.UserRole)
        self.accept()

    def _on_add(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "选择 PDF 文件", "", "PDF 文件 (*.pdf)",
        )
        if path:
            self._result_path = path
            self.accept()

    def result_path(self) -> str | None:
        return self._result_path


class MainApp(QWidget):
    """V3.4 Unified workspace: left PDF reader (flex), right panel (fixed 500px)."""

    def __init__(self):
        super().__init__()
        self.setWindowTitle("CY-Translator")

        base_dir = os.path.dirname(os.path.abspath(sys.argv[0]))
        icon_path = Path(base_dir) / "translator.png"
        if icon_path.exists():
            self.setWindowIcon(QIcon(str(icon_path)))

        self._config = load_config()
        self._current_pdf = None
        self._setup_ui()
        self._restore_window_size()
        self._restore_auto_complete()

    def _setup_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(4, 4, 4, 4)
        root.setSpacing(2)

        # ── Top bar ────────────────────────────────────────────────
        top_bar = QWidget()
        top_bar.setMaximumHeight(36)
        top = QHBoxLayout(top_bar)
        top.setContentsMargins(0, 0, 0, 0)

        top.addWidget(QLabel("窗口尺寸:"))
        self.size_combo = QComboBox()
        self.size_combo.addItems(SIZE_PRESETS)
        self.size_combo.currentTextChanged.connect(self._on_size_changed)
        top.addWidget(self.size_combo)
        top.addStretch()

        open_btn = QPushButton("打开 PDF")
        open_btn.clicked.connect(self._open_pdf)
        top.addWidget(open_btn)

        self.file_label = QLabel("未打开文件")
        self.file_label.setStyleSheet("color: gray;")
        top.addWidget(self.file_label)

        root.addWidget(top_bar, 0)

        # ── Body: TOC sidebar + QSplitter ──────────────────────────
        self._splitter = QSplitter()
        self._splitter.setChildrenCollapsible(False)

        self.pdf_viewer = PDFViewer()
        self._toc_panel = self.pdf_viewer.toc_panel

        self._left_area = QWidget()
        left_layout = QHBoxLayout(self._left_area)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(0)
        left_layout.addWidget(self._toc_panel)
        left_layout.addWidget(self.pdf_viewer)

        self.reader_tab = ReaderTab(self._config)
        self.reader_tab.setMinimumWidth(500)

        self._splitter.addWidget(self._left_area)
        self._splitter.addWidget(self.reader_tab)

        self._splitter.setSizes([300, RIGHT_PANEL_WIDTH])

        root.addWidget(self._splitter, 1)

        self.pdf_viewer.text_selected.connect(self._on_pdf_selection)
        self.pdf_viewer.context_menu_requested.connect(self._on_pdf_context_menu)
        self.pdf_viewer.auto_complete_changed.connect(self._on_auto_complete_changed)
        self.pdf_viewer.selection_started.connect(self.reader_tab._on_selection_started)
        self.pdf_viewer.toc_collapsed_changed.connect(self._on_toc_collapsed_changed)
        self.pdf_viewer.note_path_needed.connect(self._ensure_note_path)

        self.reader_tab.inject_pdf_viewer(self.pdf_viewer)

    def showEvent(self, event):
        super().showEvent(event)
        if getattr(self, '_spliter_init_done', False):
            return
        self._spliter_init_done = True
        saved_width = self._config.get("right_panel_width", 0)
        right_w = saved_width if saved_width > 0 else RIGHT_PANEL_WIDTH
        left_w = max(self.width() - right_w, 300)
        self._splitter.setSizes([left_w, right_w])

    def resizeEvent(self, event):
        super().resizeEvent(event)
        sizes = self._splitter.sizes()
        if len(sizes) == 2:
            total = self.width()
            right = sizes[1]
            left = max(total - right, 300)
            self._splitter.setSizes([left, right])

    # ── Slots ──────────────────────────────────────────────────────

    def _open_pdf(self):
        dlg = PdfHistoryDialog(self._config, self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        path = dlg.result_path()
        if not path:
            return
        self._add_pdf_history(path)
        self._load_pdf(path)

    def _add_pdf_history(self, path: str):
        history: list = self._config.get("pdf_history", [])
        # Inherit config from existing entry for the same path
        old_config = {"cache_file": None, "isolate_file": None, "dual_column": False, "note_file": None}
        for e in history:
            if isinstance(e, dict) and e.get("path") == path:
                old_config = e.get("config", old_config)
                break
        # Remove existing entry for same path
        history = [
            e for e in history
            if (e if isinstance(e, str) else e.get("path")) != path
        ]
        # Add to head with current timestamp and inherited config
        date_str = datetime.now().strftime("%Y-%m-%d-%H-%M-%S")
        history.insert(0, {
            "path": path, "date": date_str,
            "config": old_config,
        })
        self._config["pdf_history"] = history[:20]
        save_config(self._config)

    def _load_pdf(self, path: str):
        self._current_pdf = path
        self.file_label.setText(os.path.basename(path))
        self.reader_tab.set_current_file(path)
        self.pdf_viewer.load_file(path)
        # Restore isolate_path (cleared by _full_cleanup) and reload zones
        entry = get_or_create_pdf_history_entry(self._config, path)
        isolate_file = entry.get("config", {}).get("isolate_file")
        self.pdf_viewer.set_isolate_path(isolate_file)
        self.pdf_viewer.load_zones()
        # Restore note file binding
        note_file = entry.get("config", {}).get("note_file")
        self.pdf_viewer.set_note_path(note_file)
        self.pdf_viewer.load_notes()

    def _on_pdf_selection(self, lo, hi, text):
        self.reader_tab.set_last_selection(text)
        self.reader_tab.on_pdf_selection(lo, hi, text)

    def _on_auto_complete_changed(self, enabled: bool):
        self._config["auto_complete_enabled"] = enabled
        save_config(self._config)

    def _on_toc_collapsed_changed(self, collapsed: bool):
        self._config["toc_collapsed"] = collapsed
        save_config(self._config)

    def _ensure_note_path(self):
        """Auto-generate a note file path for the current PDF if not set."""
        if not self._current_pdf:
            return
        entry = get_or_create_pdf_history_entry(self._config, self._current_pdf)
        cfg = entry.setdefault("config", {})
        if not cfg.get("note_file"):
            from services.cache_store import auto_generate_per_pdf_path
            note_file = auto_generate_per_pdf_path(self._current_pdf, "_note")
            set_pdf_config_path(self._config, self._current_pdf,
                               "note_file", note_file)
            save_config(self._config)
            self.pdf_viewer.set_note_path(note_file)

    def _on_pdf_context_menu(self, text):
        menu = QMenu(self)
        action = menu.addAction("翻译选中文本")
        action.triggered.connect(
            lambda: self.reader_tab.on_context_menu_translate(text))
        menu.exec(QCursor.pos())

    def _on_size_changed(self, text):
        try:
            w_str, h_str = text.split("x")
            w, h = int(w_str), int(h_str)
            self.resize(w, h)
            self._config["window_size"] = text
            save_config(self._config)
        except (ValueError, AttributeError):
            pass

    def _restore_window_size(self):
        size_str = self._config.get("window_size", "1920x1080")
        if size_str in SIZE_PRESETS:
            self.size_combo.setCurrentText(size_str)
        else:
            self.size_combo.setCurrentText("1920x1080")
        try:
            w_str, h_str = size_str.split("x")
            self.resize(int(w_str), int(h_str))
        except (ValueError, AttributeError):
            self.resize(1920, 1080)

    def _restore_auto_complete(self):
        enabled = self._config.get("auto_complete_enabled", False)
        self.pdf_viewer.set_auto_complete_enabled(enabled)

    def closeEvent(self, event):
        sizes = self._splitter.sizes()
        if len(sizes) == 2:
            self._config["right_panel_width"] = sizes[1]
            save_config(self._config)
        self.pdf_viewer.save_notes_force()
        self.pdf_viewer.shutdown()
        super().closeEvent(event)
