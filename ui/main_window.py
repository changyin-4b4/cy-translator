import os

from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from services.config_store import (
    load_config,
    save_config,
    add_base_url,
    remove_base_url,
    set_url_key,
    get_url_key,
    get_url_models,
    add_model_to_url,
    remove_model_from_url,
    set_models_for_url,
    add_output_dir,
    remove_output_dir,
    add_append_file,
    remove_append_file,
    add_prompt_file,
    remove_prompt_file,
)
from services.file_writer import append_to_file, normalize_filename, write_new_file
from services.llm_client import translate
from services.model_fetcher import fetch_models
from services.prompt_loader import load_prompt
from utils.paste_cleaner import clean_newlines


# ── Paste-aware text edit ──────────────────────────────────────────────

class PasteCleanTextEdit(QTextEdit):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptRichText(False)
        self._paste_clean_enabled = False

    def set_paste_clean_enabled(self, enabled: bool):
        self._paste_clean_enabled = enabled

    def insertFromMimeData(self, source):
        if self._paste_clean_enabled and source.hasText():
            self.insertPlainText(clean_newlines(source.text()))
            return
        super().insertFromMimeData(source)


# ── Background workers ─────────────────────────────────────────────────

class _TranslateWorker(QThread):
    finished = Signal(bool, str)

    def __init__(self, llm_kwargs, prompt_content, user_text, output_kwargs):
        super().__init__()
        self.llm_kwargs = llm_kwargs
        self.prompt_content = prompt_content
        self.user_text = user_text
        self.output_kwargs = output_kwargs

    def run(self):
        try:
            result = translate(
                base_url=self.llm_kwargs["base_url"],
                api_key=self.llm_kwargs["api_key"],
                model=self.llm_kwargs["model"],
                system_prompt=self.prompt_content,
                user_text=self.user_text,
            )
            mode = self.output_kwargs["mode"]
            if mode == "new":
                write_new_file(
                    self.output_kwargs["directory"],
                    self.output_kwargs["filename"],
                    result,
                )
            else:
                append_to_file(self.output_kwargs["file_path"], result)
            self.finished.emit(True, result)
        except Exception as e:
            self.finished.emit(False, str(e))


class _FetchModelsWorker(QThread):
    finished = Signal(bool, object)

    def __init__(self, base_url, api_key):
        super().__init__()
        self.base_url = base_url
        self.api_key = api_key

    def run(self):
        try:
            models = fetch_models(self.base_url, self.api_key)
            self.finished.emit(True, models)
        except Exception as e:
            self.finished.emit(False, str(e))


class _TestConnectionWorker(QThread):
    finished = Signal(bool, str)

    def __init__(self, base_url, api_key, model):
        super().__init__()
        self.base_url = base_url
        self.api_key = api_key
        self.model = model

    def run(self):
        try:
            translate(
                base_url=self.base_url,
                api_key=self.api_key,
                model=self.model,
                system_prompt="You are a helpful assistant.",
                user_text='Reply with exactly "OK" and nothing else.',
            )
            self.finished.emit(True, "连接成功")
        except Exception as e:
            self.finished.emit(False, str(e))


# ── Management dialogs ─────────────────────────────────────────────────

class _UrlDetailDialog(QDialog):
    """Level 2: manage a single URL's key and models."""

    def __init__(self, config: dict, url: str, parent=None):
        super().__init__(parent)
        self._config = config
        self._url = url
        self._key_visible = False
        self.setWindowTitle(f"URL 详情 — {url}")
        self.setMinimumWidth(550)
        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)

        # URL header
        layout.addWidget(QLabel(f"<b>URL:</b> {self._url}"))

        # Key row with toggle
        key_row = QHBoxLayout()
        key_row.addWidget(QLabel("<b>Key:</b>"))
        self._key_label = QLineEdit()
        self._key_label.setReadOnly(True)
        self._key_label.setEchoMode(QLineEdit.EchoMode.Password)
        self._key_label.setText(get_url_key(self._config, self._url))
        key_row.addWidget(self._key_label)

        self._toggle_key_btn = QPushButton("显示")
        self._toggle_key_btn.setFixedWidth(60)
        self._toggle_key_btn.clicked.connect(self._toggle_key)
        key_row.addWidget(self._toggle_key_btn)
        layout.addLayout(key_row)

        # Model list
        layout.addWidget(QLabel("<b>模型列表:</b>"))
        self._model_list = QListWidget()
        self._rebuild_models()
        layout.addWidget(self._model_list)

        del_model_btn = QPushButton("删除选中模型")
        del_model_btn.clicked.connect(self._delete_model)
        layout.addWidget(del_model_btn)

    def _rebuild_models(self):
        self._model_list.clear()
        models = get_url_models(self._config, self._url)
        for m in models:
            self._model_list.addItem(m)

    def _toggle_key(self):
        self._key_visible = not self._key_visible
        if self._key_visible:
            self._key_label.setEchoMode(QLineEdit.EchoMode.Normal)
            self._toggle_key_btn.setText("隐藏")
        else:
            self._key_label.setEchoMode(QLineEdit.EchoMode.Password)
            self._toggle_key_btn.setText("显示")

    def _delete_model(self):
        row = self._model_list.currentRow()
        if row < 0:
            return
        model = self._model_list.item(row).text()
        remove_model_from_url(self._config, self._url, model)
        save_config(self._config)
        self._rebuild_models()


class _UrlManageDialog(QDialog):
    """Level 1: manage all saved URLs."""

    def __init__(self, config: dict, parent=None):
        super().__init__(parent)
        self._config = config
        self.setWindowTitle("管理 URL 缓存")
        self.setMinimumWidth(550)
        layout = QVBoxLayout(self)

        self._list = QListWidget()
        self._rebuild()
        self._list.itemDoubleClicked.connect(self._open_detail)
        layout.addWidget(self._list)

        btn_row = QHBoxLayout()
        detail_btn = QPushButton("详情 / 管理模型")
        detail_btn.clicked.connect(self._open_detail)
        btn_row.addWidget(detail_btn)

        del_btn = QPushButton("删除选中 URL")
        del_btn.clicked.connect(self._delete_selected)
        btn_row.addWidget(del_btn)
        layout.addLayout(btn_row)

    def _rebuild(self):
        self._list.clear()
        for url, entry in self._config.get("base_urls", {}).items():
            model_count = len(entry.get("models", []))
            label = f"{url}  —  Models: {model_count}"
            self._list.addItem(label)

    def _get_selected_url(self):
        row = self._list.currentRow()
        if row < 0:
            return None
        urls = list(self._config.get("base_urls", {}).keys())
        if row < len(urls):
            return urls[row]
        return None

    def _open_detail(self):
        url = self._get_selected_url()
        if not url:
            return
        dlg = _UrlDetailDialog(self._config, url, self)
        dlg.exec()
        save_config(self._config)
        self._rebuild()

    def _delete_selected(self):
        url = self._get_selected_url()
        if not url:
            return
        remove_base_url(self._config, url)
        save_config(self._config)
        self._rebuild()


class _PathManageDialog(QDialog):
    """Manage saved directory or file paths."""

    def __init__(self, title: str, items: list, on_remove, parent=None):
        super().__init__(parent)
        self._items = items
        self._on_remove = on_remove
        self.setWindowTitle(title)
        self.setMinimumWidth(500)
        layout = QVBoxLayout(self)

        self._list = QListWidget()
        self._rebuild()
        layout.addWidget(self._list)

        del_btn = QPushButton("删除选中")
        del_btn.clicked.connect(self._delete_selected)
        layout.addWidget(del_btn)

    def _rebuild(self):
        self._list.clear()
        for item in self._items:
            self._list.addItem(item)

    def _delete_selected(self):
        row = self._list.currentRow()
        if 0 <= row < len(self._items):
            removed = self._items[row]
            self._on_remove(removed)
            self._rebuild()


# ── Main window ────────────────────────────────────────────────────────

class MainWindow(QWidget):
    def __init__(self):
        super().__init__()
        self._worker = None
        self._config = load_config()
        self._suppress_url_change = False
        self._suppress_key_change = False
        self._setup_ui()
        self._restore_config()
        self.setWindowTitle("CY-Translator")

    # ── UI construction ──────────────────────────────────────────────

    def _setup_ui(self):
        root = QVBoxLayout(self)
        root.setSpacing(12)

        root.addWidget(self._build_input_area())
        root.addWidget(self._build_output_area())
        root.addWidget(self._build_config_area())
        root.addLayout(self._build_action_bar())
        root.addWidget(self._build_status_area())

    def _build_input_area(self):
        group = QGroupBox("输入")
        layout = QVBoxLayout(group)

        self.input_edit = PasteCleanTextEdit()
        self.input_edit.setPlaceholderText("在此输入或粘贴待翻译文本...")
        self.input_edit.setMinimumHeight(180)
        layout.addWidget(self.input_edit)

        row = QHBoxLayout()
        self.paste_clean_toggle = QCheckBox("粘贴时去掉换行")
        self.paste_clean_toggle.toggled.connect(self._on_paste_clean_toggled)
        row.addWidget(self.paste_clean_toggle)
        row.addStretch()

        manual_clean_btn = QPushButton("手动清洗当前文本")
        manual_clean_btn.clicked.connect(self._manual_clean)
        row.addWidget(manual_clean_btn)

        clear_btn = QPushButton("清空")
        clear_btn.clicked.connect(self.input_edit.clear)
        row.addWidget(clear_btn)

        layout.addLayout(row)
        return group

    def _build_output_area(self):
        group = QGroupBox("输出模式")
        layout = QVBoxLayout(group)

        mode_row = QHBoxLayout()
        self.new_radio = QRadioButton("New")
        self.append_radio = QRadioButton("Append")
        self.new_radio.toggled.connect(self._on_output_mode_changed)
        mode_row.addWidget(self.new_radio)
        mode_row.addWidget(self.append_radio)
        mode_row.addStretch()
        layout.addLayout(mode_row)

        # new mode
        self.new_widget = QWidget()
        nl = QHBoxLayout(self.new_widget)
        nl.setContentsMargins(0, 0, 0, 0)
        nl.addWidget(QLabel("目录:"))
        self.dir_combo = QComboBox()
        self.dir_combo.setEditable(True)
        self.dir_combo.setMinimumWidth(300)
        self.dir_combo.currentTextChanged.connect(self._update_path_preview)
        nl.addWidget(self.dir_combo)
        dir_btn = QPushButton("选择")
        dir_btn.clicked.connect(self._pick_output_dir)
        nl.addWidget(dir_btn)
        dir_mgmt = QPushButton("管理")
        dir_mgmt.clicked.connect(self._manage_output_dirs)
        nl.addWidget(dir_mgmt)

        nl.addWidget(QLabel("文件名:"))
        self.filename_edit = QLineEdit()
        self.filename_edit.setPlaceholderText("不必输入.md后缀")
        self.filename_edit.textChanged.connect(self._update_path_preview)
        nl.addWidget(self.filename_edit)
        layout.addWidget(self.new_widget)

        self.path_preview = QLabel("")
        self.path_preview.setStyleSheet("color: gray;")
        layout.addWidget(self.path_preview)

        # append mode
        self.append_widget = QWidget()
        al = QHBoxLayout(self.append_widget)
        al.setContentsMargins(0, 0, 0, 0)
        al.addWidget(QLabel("文件:"))
        self.append_combo = QComboBox()
        self.append_combo.setEditable(True)
        self.append_combo.setMinimumWidth(300)
        al.addWidget(self.append_combo)
        append_btn = QPushButton("选择")
        append_btn.clicked.connect(self._pick_append_file)
        al.addWidget(append_btn)
        append_mgmt = QPushButton("管理")
        append_mgmt.clicked.connect(self._manage_append_files)
        al.addWidget(append_mgmt)
        layout.addWidget(self.append_widget)

        self.append_widget.hide()
        return group

    def _build_config_area(self):
        group = QGroupBox("Prompt 与模型配置")
        layout = QVBoxLayout(group)

        # Prompt row
        pr = QHBoxLayout()
        pr.addWidget(QLabel("Prompt:"))
        self.prompt_combo = QComboBox()
        self.prompt_combo.setEditable(True)
        self.prompt_combo.setMinimumWidth(300)
        pr.addWidget(self.prompt_combo)
        prompt_btn = QPushButton("选择")
        prompt_btn.clicked.connect(self._pick_prompt_file)
        pr.addWidget(prompt_btn)
        preview_btn = QPushButton("预览")
        preview_btn.clicked.connect(self._preview_prompt)
        pr.addWidget(preview_btn)
        prompt_mgmt = QPushButton("管理")
        prompt_mgmt.clicked.connect(self._manage_prompts)
        pr.addWidget(prompt_mgmt)
        layout.addLayout(pr)

        # URL + Key row
        mr = QHBoxLayout()
        mr.addWidget(QLabel("Base URL:"))
        self.url_combo = QComboBox()
        self.url_combo.setEditable(True)
        self.url_combo.setMinimumWidth(250)
        self.url_combo.currentTextChanged.connect(self._on_url_changed)
        mr.addWidget(self.url_combo)

        mr.addWidget(QLabel("Key:"))
        self.api_key_edit = QLineEdit()
        self.api_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_key_edit.setPlaceholderText("sk-...")
        self.api_key_edit.textChanged.connect(self._on_key_changed)
        mr.addWidget(self.api_key_edit)

        url_mgmt = QPushButton("管理")
        url_mgmt.clicked.connect(self._manage_urls)
        mr.addWidget(url_mgmt)
        layout.addLayout(mr)

        # Model row
        mr2 = QHBoxLayout()
        mr2.addWidget(QLabel("Model:"))
        self.model_combo = QComboBox()
        self.model_combo.setEditable(True)
        self.model_combo.setMinimumWidth(300)
        mr2.addWidget(self.model_combo)

        fetch_btn = QPushButton("获取模型列表")
        fetch_btn.setMinimumWidth(100)
        fetch_btn.clicked.connect(self._fetch_models)
        mr2.addWidget(fetch_btn)
        mr2.addStretch()
        layout.addLayout(mr2)

        return group

    def _build_action_bar(self):
        row = QHBoxLayout()

        self.translate_btn = QPushButton("开始翻译")
        self.translate_btn.setMinimumHeight(36)
        self.translate_btn.clicked.connect(self._start_translate)
        row.addWidget(self.translate_btn)

        self.test_btn = QPushButton("测试连接")
        self.test_btn.clicked.connect(self._test_connection)
        row.addWidget(self.test_btn)

        row.addStretch()
        return row

    def _build_status_area(self):
        group = QGroupBox("状态")
        layout = QVBoxLayout(group)

        self.status_label = QLabel("就绪")
        layout.addWidget(self.status_label)

        self.log_edit = QTextEdit()
        self.log_edit.setReadOnly(True)
        self.log_edit.setMaximumHeight(120)
        layout.addWidget(self.log_edit)

        return group

    # ── Slot handlers: paste / output mode ───────────────────────────

    def _on_paste_clean_toggled(self, checked):
        self.input_edit.set_paste_clean_enabled(checked)

    def _on_output_mode_changed(self):
        if self.new_radio.isChecked():
            self.new_widget.show()
            self.path_preview.show()
            self.append_widget.hide()
            self._update_path_preview()
        else:
            self.new_widget.hide()
            self.path_preview.hide()
            self.append_widget.show()

    def _manual_clean(self):
        text = self.input_edit.toPlainText()
        self.input_edit.setPlainText(clean_newlines(text))
        self._log("已手动清洗当前文本中的换行符")

    # ── Slot handlers: file / prompt pickers ─────────────────────────

    def _pick_output_dir(self):
        d = QFileDialog.getExistingDirectory(self, "选择输出目录")
        if d:
            self.dir_combo.setCurrentText(d)
            self.dir_combo.setEditText(d)
            add_output_dir(self._config, d)
            self._refresh_dir_combo()
            self._update_path_preview()
            save_config(self._config)

    def _pick_append_file(self):
        f, _ = QFileDialog.getOpenFileName(
            self, "选择 Markdown 文件", "", "Markdown (*.md)"
        )
        if f:
            self.append_combo.setCurrentText(f)
            self.append_combo.setEditText(f)
            add_append_file(self._config, f)
            self._refresh_append_combo()
            save_config(self._config)

    def _pick_prompt_file(self):
        f, _ = QFileDialog.getOpenFileName(
            self, "选择系统提示词文件", "", "文本文件 (*.txt);;所有文件 (*)"
        )
        if f:
            self.prompt_combo.setCurrentText(f)
            self.prompt_combo.setEditText(f)
            add_prompt_file(self._config, f)
            self._refresh_prompt_combo()
            save_config(self._config)

    def _update_path_preview(self):
        d = self.dir_combo.currentText().strip()
        name = self.filename_edit.text().strip()
        if d and name:
            preview = os.path.join(d, normalize_filename(name))
            self.path_preview.setText(f"→ {preview}")
        else:
            self.path_preview.setText("")

    # ── Slot handlers: URL / Key / Model binding ─────────────────────

    def _on_url_changed(self, text):
        if self._suppress_url_change:
            return
        url = text.strip()
        self._suppress_key_change = True
        if url and url in self._config.get("base_urls", {}):
            key = get_url_key(self._config, url)
            self.api_key_edit.setText(key)
            self._refresh_model_combo()
        else:
            self.api_key_edit.clear()
            self.model_combo.clear()
        self._suppress_key_change = False

    def _on_key_changed(self, text):
        if self._suppress_key_change:
            return
        url = self.url_combo.currentText().strip()
        if url:
            add_base_url(self._config, url)
            set_url_key(self._config, url, text)
            save_config(self._config)

    # ── Slot handlers: model fetch / translate / test ────────────────

    def _fetch_models(self):
        url = self.url_combo.currentText().strip()
        key = self.api_key_edit.text().strip()
        if not url:
            QMessageBox.warning(self, "提示", "请先填写 Base URL")
            return
        self._log("正在获取模型列表...")
        self.status_label.setText("获取模型列表中…")
        self._run_worker(
            _FetchModelsWorker(url, key),
            self._on_models_fetched,
        )

    def _on_models_fetched(self, ok, data):
        if ok:
            url = self.url_combo.currentText().strip()
            set_models_for_url(self._config, url, data)
            # also make sure url entry exists with key
            key = self.api_key_edit.text().strip()
            if url not in self._config.get("base_urls", {}):
                add_base_url(self._config, url)
            set_url_key(self._config, url, key)
            save_config(self._config)
            self._refresh_model_combo()
            self._log(f"已获取 {len(data)} 个模型")
            self.status_label.setText("模型列表获取成功")
        else:
            self._log(f"获取模型列表失败: {data}")
            self.status_label.setText("获取失败，请手动填写模型名")

    def _start_translate(self):
        errors = []
        user_text = self.input_edit.toPlainText().strip()
        if not user_text:
            errors.append("输入文本不能为空")

        prompt_path = self.prompt_combo.currentText().strip()
        if not prompt_path:
            errors.append("请选择系统提示词文件")
        try:
            prompt_content = load_prompt(prompt_path) if prompt_path else ""
        except Exception as e:
            errors.append(str(e))
            prompt_content = ""

        if errors:
            QMessageBox.warning(self, "输入错误", "\n".join(errors))
            return

        base_url = self.url_combo.currentText().strip()
        api_key = self.api_key_edit.text().strip()
        model = self.model_combo.currentText().strip()
        if not base_url:
            errors.append("Base URL 不能为空")
        if not api_key:
            errors.append("API Key 不能为空")
        if not model:
            errors.append("Model 不能为空")

        output_kwargs = {}
        if self.new_radio.isChecked():
            d = self.dir_combo.currentText().strip()
            name = self.filename_edit.text().strip()
            if not d:
                errors.append("请选择输出目录")
            if not name:
                errors.append("请输入文件名")
            preview_path = os.path.join(d, normalize_filename(name)) if d and name else ""
            if preview_path and os.path.exists(preview_path):
                reply = QMessageBox.question(
                    self, "文件已存在",
                    f"文件 {preview_path} 已存在，是否覆盖？",
                    QMessageBox.Yes | QMessageBox.No,
                )
                if reply != QMessageBox.Yes:
                    return
            output_kwargs = {"mode": "new", "directory": d, "filename": name}
        else:
            f = self.append_combo.currentText().strip()
            if not f:
                errors.append("请选择要追加的 .md 文件")
            elif not f.lower().endswith(".md"):
                errors.append("目标文件必须是 .md 文件")
            elif not os.path.isfile(f):
                errors.append("目标文件不存在")
            output_kwargs = {"mode": "append", "file_path": f}

        if errors:
            QMessageBox.warning(self, "输入错误", "\n".join(errors))
            return

        self._log("开始翻译...")
        self.status_label.setText("翻译中…")
        self._set_controls_enabled(False)

        # persist current paths
        self._persist_current_paths()

        self._run_worker(
            _TranslateWorker(
                llm_kwargs={"base_url": base_url, "api_key": api_key, "model": model},
                prompt_content=prompt_content,
                user_text=user_text,
                output_kwargs=output_kwargs,
            ),
            self._on_translate_done,
        )

    def _on_translate_done(self, ok, data):
        self._set_controls_enabled(True)
        if ok:
            url = self.url_combo.currentText().strip()
            model = self.model_combo.currentText().strip()
            # persist validated model
            add_base_url(self._config, url)
            set_url_key(self._config, url, self.api_key_edit.text().strip())
            add_model_to_url(self._config, url, model)
            save_config(self._config)
            self._refresh_model_combo()
            self._log("翻译完成")
            self._log(f"---\n{data}\n---")
            self.status_label.setText("翻译完成")
        else:
            self._log(f"翻译失败: {data}")
            self.status_label.setText("翻译失败")
            QMessageBox.critical(self, "翻译失败", data)

    def _test_connection(self):
        url = self.url_combo.currentText().strip()
        key = self.api_key_edit.text().strip()
        model = self.model_combo.currentText().strip()
        errors = []
        if not url:
            errors.append("Base URL 不能为空")
        if not key:
            errors.append("API Key 不能为空")
        if not model:
            errors.append("Model 不能为空")
        if errors:
            QMessageBox.warning(self, "输入错误", "\n".join(errors))
            return

        self._log("测试连接...")
        self.status_label.setText("测试连接中…")
        self._set_controls_enabled(False)
        self._run_worker(
            _TestConnectionWorker(url, key, model),
            self._on_test_done,
        )

    def _on_test_done(self, ok, msg):
        self._set_controls_enabled(True)
        if ok:
            url = self.url_combo.currentText().strip()
            model = self.model_combo.currentText().strip()
            # persist validated model
            add_base_url(self._config, url)
            set_url_key(self._config, url, self.api_key_edit.text().strip())
            add_model_to_url(self._config, url, model)
            save_config(self._config)
            self._refresh_model_combo()
            self._log("连接测试成功")
            self.status_label.setText("连接成功")
            QMessageBox.information(self, "成功", "连接测试通过")
        else:
            self._log(f"连接测试失败: {msg}")
            self.status_label.setText("连接失败")
            QMessageBox.critical(self, "连接失败", msg)

    # ── Management dialogs ───────────────────────────────────────────

    def _manage_urls(self):
        dlg = _UrlManageDialog(self._config, self)
        dlg.exec()
        save_config(self._config)
        self._refresh_url_combo()
        self._refresh_model_combo()

    def _manage_output_dirs(self):
        items = list(self._config.get("output_dirs", []))

        def on_remove(item):
            remove_output_dir(self._config, item)
            save_config(self._config)

        dlg = _PathManageDialog("管理输出目录缓存", items, on_remove, self)
        dlg.exec()
        save_config(self._config)
        self._refresh_dir_combo()

    def _manage_append_files(self):
        items = list(self._config.get("append_files", []))

        def on_remove(item):
            remove_append_file(self._config, item)
            save_config(self._config)

        dlg = _PathManageDialog("管理 Append 文件缓存", items, on_remove, self)
        dlg.exec()
        save_config(self._config)
        self._refresh_append_combo()

    def _manage_prompts(self):
        items = list(self._config.get("prompt_files", []))

        def on_remove(item):
            remove_prompt_file(self._config, item)
            save_config(self._config)

        dlg = _PathManageDialog("管理 Prompt 缓存", items, on_remove, self)
        dlg.exec()
        save_config(self._config)
        self._refresh_prompt_combo()

    def _preview_prompt(self):
        path = self.prompt_combo.currentText().strip()
        if not path:
            QMessageBox.warning(self, "提示", "请先选择 Prompt 文件")
            return
        try:
            content = load_prompt(path)
            box = QMessageBox(self)
            box.setWindowTitle("Prompt 预览")
            box.setText(content[:2000])
            box.exec()
        except Exception as e:
            QMessageBox.critical(self, "错误", str(e))

    # ── Combo refresh helpers ────────────────────────────────────────

    def _refresh_url_combo(self):
        self._config = load_config()
        self._suppress_url_change = True
        current = self.url_combo.currentText()
        self.url_combo.clear()
        urls = list(self._config.get("base_urls", {}).keys())
        self.url_combo.addItems(urls)
        if current:
            self.url_combo.setCurrentText(current)
        self._suppress_url_change = False

    def _refresh_model_combo(self):
        self._config = load_config()
        url = self.url_combo.currentText().strip()
        current = self.model_combo.currentText()
        self.model_combo.clear()
        if url:
            models = get_url_models(self._config, url)
            self.model_combo.addItems(models)
        if current:
            self.model_combo.setCurrentText(current)

    def _refresh_dir_combo(self):
        current = self.dir_combo.currentText()
        self.dir_combo.clear()
        dirs = self._config.get("output_dirs", [])
        self.dir_combo.addItems(dirs)
        if current:
            self.dir_combo.setCurrentText(current)

    def _refresh_append_combo(self):
        current = self.append_combo.currentText()
        self.append_combo.clear()
        files = self._config.get("append_files", [])
        self.append_combo.addItems(files)
        if current:
            self.append_combo.setCurrentText(current)

    def _refresh_prompt_combo(self):
        current = self.prompt_combo.currentText()
        self.prompt_combo.clear()
        files = self._config.get("prompt_files", [])
        self.prompt_combo.addItems(files)
        if current:
            self.prompt_combo.setCurrentText(current)

    # ── Config persistence ───────────────────────────────────────────

    def _persist_current_paths(self):
        """Save currently selected paths into history lists."""
        d = self.dir_combo.currentText().strip()
        if d:
            add_output_dir(self._config, d)
            self._config["current_output_dir"] = d
        f = self.append_combo.currentText().strip()
        if f:
            add_append_file(self._config, f)
            self._config["current_append_file"] = f
        p = self.prompt_combo.currentText().strip()
        if p:
            add_prompt_file(self._config, p)
            self._config["current_prompt_file"] = p
        url = self.url_combo.currentText().strip()
        if url:
            add_base_url(self._config, url)
            self._config["current_url"] = url
            self._config["current_model"] = self.model_combo.currentText().strip()
        save_config(self._config)

    def _collect_config(self):
        self._persist_current_paths()
        self._config["output_mode"] = "new" if self.new_radio.isChecked() else "append"
        self._config["paste_clean_enabled"] = self.paste_clean_toggle.isChecked()

    def _restore_config(self):
        c = self._config

        # URL and key
        self._suppress_url_change = True
        self.url_combo.clear()
        urls = list(c.get("base_urls", {}).keys())
        self.url_combo.addItems(urls)
        if c.get("current_url", ""):
            self.url_combo.setCurrentText(c["current_url"])
        elif urls:
            self.url_combo.setCurrentText(urls[0])
        self._suppress_url_change = False

        # trigger URL change to load key + models
        cur_url = self.url_combo.currentText().strip()
        self._suppress_key_change = True
        if cur_url:
            self.api_key_edit.setText(get_url_key(c, cur_url))
        self._suppress_key_change = False

        # model
        models = get_url_models(c, cur_url)
        self.model_combo.clear()
        self.model_combo.addItems(models)
        if c.get("current_model", ""):
            self.model_combo.setCurrentText(c["current_model"])
        elif models:
            self.model_combo.setCurrentText(models[0])

        # prompt
        prompt_files = c.get("prompt_files", [])
        self.prompt_combo.clear()
        self.prompt_combo.addItems(prompt_files)
        if c.get("current_prompt_file", ""):
            self.prompt_combo.setCurrentText(c["current_prompt_file"])
        elif prompt_files:
            self.prompt_combo.setCurrentText(prompt_files[0])

        # output mode: default append if append_files non-empty
        output_mode = c.get("output_mode", "new")
        append_files = c.get("append_files", [])
        if output_mode == "append" or (output_mode == "new" and append_files and c.get("current_append_file", "")):
            self.append_radio.setChecked(True)
            self._on_output_mode_changed()
        else:
            self.new_radio.setChecked(True)

        # dir history
        dirs = c.get("output_dirs", [])
        self.dir_combo.clear()
        self.dir_combo.addItems(dirs)
        if c.get("current_output_dir", ""):
            self.dir_combo.setCurrentText(c["current_output_dir"])
        elif dirs:
            self.dir_combo.setCurrentText(dirs[0])

        # append history
        self.append_combo.clear()
        self.append_combo.addItems(append_files)
        if c.get("current_append_file", ""):
            self.append_combo.setCurrentText(c["current_append_file"])
        elif append_files:
            self.append_combo.setCurrentText(append_files[0])

        self.paste_clean_toggle.setChecked(c.get("paste_clean_enabled", False))
        self.input_edit.set_paste_clean_enabled(self.paste_clean_toggle.isChecked())
        self._update_path_preview()

    # ── Helpers ──────────────────────────────────────────────────────

    def _run_worker(self, worker, on_finish):
        if self._worker and self._worker.isRunning():
            self._worker.terminate()
            self._worker.wait()
        self._worker = worker
        worker.finished.connect(on_finish)
        worker.finished.connect(lambda: setattr(self, "_worker", None))
        worker.start()

    def _set_controls_enabled(self, enabled):
        self.translate_btn.setEnabled(enabled)
        self.test_btn.setEnabled(enabled)

    def _log(self, msg):
        self.log_edit.append(msg)

    def closeEvent(self, event):
        self._collect_config()
        save_config(self._config)
        super().closeEvent(event)
