import os

from PySide6.QtCore import QThread, Signal
from PySide6.QtGui import QTextCharFormat, QTextCursor
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QSpinBox,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from services.cache_store import (
    load_cache,
    save_cache,
    lookup_phrase,
    add_phrase_entry,
    add_sentence_entry,
    find_overlapping_entries,
    find_containing_entries,
    coord_le_tolerant,
    merge_entries,
    find_mergeable_fragments,
    get_cache_summary,
    remove_file,
    auto_generate_per_pdf_path,
    _get_group,
)
from services.config_store import (
    save_config,
    add_base_url,
    set_url_key,
    get_url_key,
    get_url_models,
    add_model_to_url,
    set_models_for_url,
    add_prompt_file,
    get_or_create_pdf_history_entry,
    set_pdf_config_path,
)
from services.sentence_analyzer import (
    classify,
    expand_to_sentence,
    is_sentence_end,
    split_sentences,
    split_translation,
    transform_dual_column_coords,
)
from services.file_writer import append_to_file, normalize_filename, write_new_file
from services.llm_client import translate
from services.model_fetcher import fetch_models
from services.prompt_loader import load_prompt
from utils.paste_cleaner import clean_newlines

# ── Workers ──────────────────────────────────────────────────────────

class _FastTranslateWorker(QThread):
    finished = Signal(bool, str)

    def __init__(self, url, key, model, text, prompt):
        super().__init__()
        self.url = url
        self.key = key
        self.model = model
        self.text = text
        self.prompt = prompt

    def run(self):
        try:
            result = translate(
                base_url=self.url, api_key=self.key, model=self.model,
                system_prompt=self.prompt, user_text=self.text,
            )
            self.finished.emit(True, result)
        except Exception as e:
            self.finished.emit(False, str(e))


class _PersistTranslateWorker(QThread):
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
            if self.output_kwargs["mode"] == "new":
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


# ── Cache manage dialog ──────────────────────────────────────────────

class _CacheManageDialog(QDialog):
    def __init__(self, cache_path: str | None = None, parent=None):
        super().__init__(parent)
        self._cache_path = cache_path
        self.setWindowTitle("缓存清理")
        self.setMinimumWidth(480)
        layout = QVBoxLayout(self)

        cache = load_cache(cache_path)
        summaries = get_cache_summary(cache)
        if not summaries:
            layout.addWidget(QLabel("当前无缓存数据"))
            self._summaries = []
        else:
            layout.addWidget(QLabel(f"共 {len(summaries)} 个文档的翻译缓存："))
            self._list = QListWidget()
            for s in summaries:
                self._list.addItem(f"{s['filename']}  ({s['entry_count']} 条)")
            layout.addWidget(self._list)

            del_btn = QPushButton("删除选中文件的全部缓存")
            del_btn.clicked.connect(self._delete_selected)
            layout.addWidget(del_btn)

        self._summaries = summaries

    def _delete_selected(self):
        row = self._list.currentRow()
        if 0 <= row < len(self._summaries):
            cache = load_cache(self._cache_path)
            remove_file(cache, self._summaries[row]["file_path"])
            save_cache(cache, self._cache_path)
            del self._summaries[row]
            self._list.takeItem(row)


# ── URL manage dialog (reuse from main_window) ───────────────────────
from ui.main_window import _UrlManageDialog

# ── Unified right panel ──────────────────────────────────────────────

class ReaderTab(QWidget):
    """Right-side control panel: shared config + two translation mode tabs."""

    def __init__(self, config: dict, parent=None):
        super().__init__(parent)
        self._worker = None
        self._config = config           # shared reference from MainApp
        self._cache: dict = {}
        self._cache_path: str | None = None
        self._current_file = None
        self._pdf_viewer = None
        self._suppress_url_change = False
        self._suppress_key_change = False
        self._suppress_fast_save = False
        self._has_result = False
        self._pending_sentences: dict | None = None
        self._setup_ui()
        self._restore()

    # ── UI construction ────────────────────────────────────────────

    def _setup_ui(self):
        root = QVBoxLayout(self)
        root.setSpacing(8)

        # ── Shared config: URL + Key ───────────────────────────────
        cfg_group = QGroupBox("模型配置（全局共享池）")
        cfg_layout = QVBoxLayout(cfg_group)

        row1 = QHBoxLayout()
        row1.addWidget(QLabel("URL:"))
        self.url_combo = QComboBox()
        self.url_combo.setEditable(True)
        self.url_combo.setMinimumWidth(200)
        self.url_combo.currentTextChanged.connect(self._on_url_changed)
        row1.addWidget(self.url_combo)

        row1.addWidget(QLabel("Key:"))
        self.key_edit = QLineEdit()
        self.key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.key_edit.textChanged.connect(self._on_key_changed)
        row1.addWidget(self.key_edit)

        mgmt_btn = QPushButton("管理 URL")
        mgmt_btn.clicked.connect(self._manage_urls)
        row1.addWidget(mgmt_btn)

        cache_btn = QPushButton("缓存清理")
        cache_btn.clicked.connect(self._manage_cache)
        row1.addWidget(cache_btn)
        cfg_layout.addLayout(row1)

        root.addWidget(cfg_group, 0)  # fixed height, never stretch

        # ── Mode tabs ──────────────────────────────────────────────
        self._mode_tabs = QTabWidget()

        self._mode_tabs.addTab(self._build_fast_tab(), "划词速翻")
        self._mode_tabs.addTab(self._build_persist_tab(), "翻译持久化")

        root.addWidget(self._mode_tabs, 1)  # stretch = take all remaining space

    def _build_fast_tab(self):
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setSpacing(6)

        # Model row
        mr = QHBoxLayout()
        mr.addWidget(QLabel("Model:"))
        self.fast_model_combo = QComboBox()
        self.fast_model_combo.setEditable(True)
        self.fast_model_combo.setMinimumWidth(180)
        self.fast_model_combo.currentTextChanged.connect(self._save_fast_config)
        mr.addWidget(self.fast_model_combo)
        fast_fetch = QPushButton("获取模型列表")
        fast_fetch.clicked.connect(self._fetch_models)
        mr.addWidget(fast_fetch)
        fast_test = QPushButton("测试连接")
        fast_test.clicked.connect(self._test_connection_fast)
        mr.addWidget(fast_test)
        mr.addStretch()
        layout.addLayout(mr)

        # Prompt row
        pr = QHBoxLayout()
        pr.addWidget(QLabel("Prompt:"))
        self.fast_prompt_combo = QComboBox()
        self.fast_prompt_combo.setEditable(True)
        self.fast_prompt_combo.setMinimumWidth(180)
        self.fast_prompt_combo.currentTextChanged.connect(self._save_fast_config)
        pr.addWidget(self.fast_prompt_combo)
        fast_prompt_btn = QPushButton("选择")
        fast_prompt_btn.clicked.connect(self._pick_fast_prompt)
        pr.addWidget(fast_prompt_btn)
        pr.addStretch()
        layout.addLayout(pr)

        # Auto-translate checkbox
        auto_row = QHBoxLayout()
        self.fast_auto_check = QCheckBox("划词后自动翻译")
        self.fast_auto_check.toggled.connect(self._save_fast_config)
        auto_row.addWidget(self.fast_auto_check)
        auto_row.addStretch()
        layout.addLayout(auto_row)

        # Cache hint
        self.cache_hint = QLabel("")
        self.cache_hint.setStyleSheet("color: #cc6600; font-weight: bold;")
        self.cache_hint.hide()
        layout.addWidget(self.cache_hint)

        # Result
        result_header = QHBoxLayout()
        result_header.addWidget(QLabel("翻译结果:"))
        result_header.addStretch()
        result_header.addWidget(QLabel("字号:"))
        self.result_font_spin = QSpinBox()
        self.result_font_spin.setRange(10, 24)
        self.result_font_spin.setValue(14)
        self.result_font_spin.setFixedWidth(60)
        self.result_font_spin.valueChanged.connect(self._on_result_font_changed)
        result_header.addWidget(self.result_font_spin)
        layout.addLayout(result_header)

        self.fast_result = QTextEdit()
        self.fast_result.setReadOnly(True)
        self.fast_result.setAcceptRichText(False)
        self.fast_result.setMinimumHeight(180)
        self.fast_result.setPlaceholderText("在左侧 PDF 中划选文本，翻译结果将显示在这里...")
        self._apply_result_font_size(14)
        layout.addWidget(self.fast_result)

        return w

    def _build_persist_tab(self):
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setSpacing(6)

        # Model row
        mr = QHBoxLayout()
        mr.addWidget(QLabel("Model:"))
        self.persist_model_combo = QComboBox()
        self.persist_model_combo.setEditable(True)
        self.persist_model_combo.setMinimumWidth(180)
        mr.addWidget(self.persist_model_combo)
        persist_fetch = QPushButton("获取模型列表")
        persist_fetch.clicked.connect(self._fetch_models)
        mr.addWidget(persist_fetch)
        persist_test = QPushButton("测试连接")
        persist_test.clicked.connect(self._test_connection_persist)
        mr.addWidget(persist_test)
        mr.addStretch()
        layout.addLayout(mr)

        # Prompt file row
        pr = QHBoxLayout()
        pr.addWidget(QLabel("Prompt:"))
        self.prompt_combo = QComboBox()
        self.prompt_combo.setEditable(True)
        self.prompt_combo.setMinimumWidth(180)
        self.prompt_combo.currentTextChanged.connect(self._save_persist_prompt)
        pr.addWidget(self.prompt_combo)
        prompt_btn = QPushButton("选择")
        prompt_btn.clicked.connect(self._pick_prompt)
        pr.addWidget(prompt_btn)
        pr.addStretch()
        layout.addLayout(pr)

        # Input area
        self.persist_input = QTextEdit()
        self.persist_input.setAcceptRichText(False)
        self.persist_input.setPlaceholderText("输入或粘贴待翻译文本...")
        self.persist_input.setMinimumHeight(140)
        layout.addWidget(self.persist_input)

        # Paste clean + clear row
        pr2 = QHBoxLayout()
        self.paste_clean_toggle = QCheckBox("粘贴时去掉换行")
        pr2.addWidget(self.paste_clean_toggle)
        clean_btn = QPushButton("手动清洗")
        clean_btn.clicked.connect(self._manual_clean)
        pr2.addWidget(clean_btn)
        clear_btn = QPushButton("清空")
        clear_btn.clicked.connect(self.persist_input.clear)
        pr2.addWidget(clear_btn)
        pr2.addStretch()
        layout.addLayout(pr2)

        # Output mode
        om_row = QHBoxLayout()
        self.new_radio = QRadioButton("New")
        self.append_radio = QRadioButton("Append")
        self.new_radio.setChecked(True)
        self.new_radio.toggled.connect(self._on_persist_mode_changed)
        om_row.addWidget(self.new_radio)
        om_row.addWidget(self.append_radio)
        om_row.addStretch()
        layout.addLayout(om_row)

        # New mode widgets
        self.new_widget = QWidget()
        nl = QHBoxLayout(self.new_widget)
        nl.setContentsMargins(0, 0, 0, 0)
        nl.addWidget(QLabel("目录:"))
        self.dir_combo = QComboBox()
        self.dir_combo.setEditable(True)
        self.dir_combo.setMinimumWidth(160)
        nl.addWidget(self.dir_combo)
        dir_btn = QPushButton("选择")
        dir_btn.clicked.connect(self._pick_dir)
        nl.addWidget(dir_btn)
        nl.addWidget(QLabel("文件名:"))
        self.filename_edit = QLineEdit()
        self.filename_edit.setPlaceholderText("不必输入.md后缀")
        nl.addWidget(self.filename_edit)
        layout.addWidget(self.new_widget)

        self.path_preview = QLabel("")
        self.path_preview.setStyleSheet("color: gray;")
        layout.addWidget(self.path_preview)

        # Append mode widgets
        self.append_widget = QWidget()
        al = QHBoxLayout(self.append_widget)
        al.setContentsMargins(0, 0, 0, 0)
        al.addWidget(QLabel("文件:"))
        self.append_combo = QComboBox()
        self.append_combo.setEditable(True)
        self.append_combo.setMinimumWidth(160)
        al.addWidget(self.append_combo)
        append_btn = QPushButton("选择")
        append_btn.clicked.connect(self._pick_append_file)
        al.addWidget(append_btn)
        layout.addWidget(self.append_widget)
        self.append_widget.hide()

        # Translate button
        btn_row = QHBoxLayout()
        self.persist_translate_btn = QPushButton("开始翻译")
        self.persist_translate_btn.setMinimumHeight(34)
        self.persist_translate_btn.clicked.connect(self._start_persist_translate)
        btn_row.addWidget(self.persist_translate_btn)
        btn_row.addStretch()
        layout.addLayout(btn_row)

        # Log
        layout.addWidget(QLabel("状态:"))
        self.persist_log = QTextEdit()
        self.persist_log.setReadOnly(True)
        self.persist_log.setMaximumHeight(100)
        layout.addWidget(self.persist_log)

        return w

    # ── URL / Key binding ─────────────────────────────────────────

    def _on_url_changed(self, text):
        if self._suppress_url_change:
            return
        url = text.strip()
        self._suppress_key_change = True
        if url and url in self._config.get("base_urls", {}):
            self.key_edit.setText(get_url_key(self._config, url))
        else:
            self.key_edit.clear()
        self._suppress_key_change = False
        self._refresh_fast_model_combo()
        self._refresh_persist_model_combo()

    def _on_key_changed(self, text):
        if self._suppress_key_change:
            return
        url = self.url_combo.currentText().strip()
        if url:
            add_base_url(self._config, url)
            set_url_key(self._config, url, text)
            save_config(self._config)

    # ── Model fetch / test ─────────────────────────────────────────

    def _fetch_models(self):
        url = self.url_combo.currentText().strip()
        key = self.key_edit.text().strip()
        if not url:
            return
        try:
            models = fetch_models(url, key)
        except Exception as e:
            QMessageBox.warning(self, "获取失败", str(e))
            return
        set_models_for_url(self._config, url, models)
        add_base_url(self._config, url)
        set_url_key(self._config, url, key)
        save_config(self._config)
        self._refresh_fast_model_combo()
        self._refresh_persist_model_combo()
        QMessageBox.information(self, "成功", f"已获取 {len(models)} 个模型")

    def _test_connection_fast(self):
        url, key, model, _ = self._get_llm_kwargs("fast")
        if not url or not key or not model:
            QMessageBox.warning(self, "提示", "请填写 URL、Key 和 Model")
            return
        try:
            translate(base_url=url, api_key=key, model=model,
                      system_prompt="You are a helpful assistant.",
                      user_text='Reply with exactly "OK" and nothing else.')
        except Exception as e:
            QMessageBox.critical(self, "连接失败", str(e))
            return
        self._persist_validated_model("fast")
        QMessageBox.information(self, "成功", "连接测试通过")

    def _test_connection_persist(self):
        url, key, model, _ = self._get_llm_kwargs("persist")
        if not url or not key or not model:
            QMessageBox.warning(self, "提示", "请填写 URL、Key 和 Model")
            return
        try:
            translate(base_url=url, api_key=key, model=model,
                      system_prompt="You are a helpful assistant.",
                      user_text='Reply with exactly "OK" and nothing else.')
        except Exception as e:
            QMessageBox.critical(self, "连接失败", str(e))
            return
        self._persist_validated_model("persist")
        QMessageBox.information(self, "成功", "连接测试通过")

    # ── Fast translate (from PDF selection) ────────────────────────

    @property
    def _is_dual(self) -> bool:
        if self._pdf_viewer is None:
            return False
        return self._pdf_viewer.is_dual_column

    def inject_pdf_viewer(self, viewer) -> None:
        self._pdf_viewer = viewer
        viewer.dual_column_toggle_requested.connect(
            self._on_dual_column_toggle_requested)
        viewer.isolate_path_needed.connect(
            self._ensure_per_pdf_paths)

    def _on_dual_column_toggle_requested(self, enabled: bool):
        target_label = "双栏" if enabled else "单栏"
        self._ensure_per_pdf_paths()
        if self._current_file and self._cache_path:
            cache = load_cache(self._cache_path)
            group_key = "dual" if enabled else "single"
            entry = cache.get(self._current_file, {})
            group = entry.get(group_key, {}) if isinstance(entry, dict) else {}
            phrases = group.get("phrases", []) if isinstance(group, dict) else []
            sentences = group.get("sentences", []) if isinstance(group, dict) else []
            if not phrases and not sentences:
                reply = QMessageBox.question(
                    self, "切换缓存模式",
                    f"单栏与双栏cache相互独立，切换后将使用{target_label}cache，是否继续？",
                    QMessageBox.Yes | QMessageBox.No,
                )
                if reply != QMessageBox.Yes:
                    self._pdf_viewer._column_btn.setChecked(not enabled)
                    return
        self._pdf_viewer.set_dual_column(enabled)
        self._persist_dual_column(enabled)

    def _persist_dual_column(self, enabled: bool):
        if self._current_file:
            set_pdf_config_path(self._config, self._current_file,
                                "dual_column", enabled)
            save_config(self._config)

    def _show_result(self, text: str) -> None:
        self.fast_result.clear()
        cursor = self.fast_result.textCursor()
        fmt = QTextCharFormat()
        fmt.setForeground(self.fast_result.palette().text())
        cursor.setCharFormat(fmt)
        self.fast_result.setTextCursor(cursor)
        self.fast_result.setPlainText(text)
        self._has_result = True

    def _show_translating(self):
        """Append gray italic '翻译中……' indicator before firing API request."""
        cursor = self.fast_result.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertHtml(
            '<br><span style="color: gray;"><i>翻译中……</i></span>'
        )

    def _on_selection_started(self):
        """Append gray separator when starting a new selection after a result."""
        if self._has_result:
            existing = self.fast_result.toPlainText().strip()
            if existing:
                cursor = self.fast_result.textCursor()
                cursor.movePosition(QTextCursor.MoveOperation.End)
                cursor.insertHtml(
                    '<br><span style="color: gray;">'
                    '══════════ 以上为历史结果 ══════════'
                    '</span>'
                )
            self._has_result = False

    def on_pdf_selection(self, lo: int, hi: int, text: str):
        """Called by main_app when PDF text is selected (mouseup)."""
        if not text or not self._current_file or self._pdf_viewer is None:
            return

        self._last_pdf_selection = text

        auto_complete = self._pdf_viewer.is_auto_complete_enabled()
        word_count = hi - lo + 1
        classify_text = text
        if not auto_complete and word_count <= 5 and text:
            stripped = text.rstrip()
            if stripped and stripped[-1] in ('.', '。'):
                classify_text = stripped[:-1]
        mode = classify(classify_text, word_count, auto_complete)

        if mode == "phrase":
            self._handle_phrase(text)
        elif auto_complete:
            self._handle_sentence_auto_complete(lo, hi)
        else:
            self._handle_sentence_manual(lo, hi, text)

    def on_context_menu_translate(self, text: str):
        """Called by main_app when user right-clicks → 翻译选中文本."""
        self._last_pdf_selection = text
        self._ensure_per_pdf_paths()
        # If pending sentences exist, use them (phase 2) for both ON/OFF
        if self._pdf_viewer is not None and self._pending_sentences:
            if self._pdf_viewer.is_auto_complete_enabled():
                self._execute_auto_translate()
            else:
                self._execute_sentence_manual()
            return
        cache_mode = classify(text, len(text.split()), False)
        if cache_mode == "sentence" and self._pdf_viewer is not None:
            lo, hi = self._pdf_viewer.get_selection_range()
            if lo is not None and hi is not None:
                words = self._pdf_viewer.words
                sub_sentences = split_sentences(words, lo, hi)
                if self._pdf_viewer.is_dual_column:
                    transform_dual_column_coords(sub_sentences)
                word_texts = self._pdf_viewer.get_word_texts()
                head_frag = lo == 0 or not is_sentence_end(word_texts[lo - 1])
                tail_frag = not is_sentence_end(word_texts[hi])
                for i, sub in enumerate(sub_sentences):
                    sub["is_head_fragment"] = (i == 0 and head_frag)
                    sub["is_tail_fragment"] = (i == len(sub_sentences) - 1 and tail_frag)
                self._do_fast_translate(
                    text, cache_mode="sentence",
                    sentence_meta={
                        "head_fragment": head_frag,
                        "tail_fragment": tail_frag,
                        "sentences": sub_sentences,
                    },
                )
                return
        # Fall back to phrase mode if sentence-mode coordinates unavailable
        if cache_mode == "sentence":
            self._do_fast_translate(text, cache_mode="phrase")
        else:
            self._do_fast_translate(text, cache_mode="phrase")

    # ── Phrase handler ─────────────────────────────────────────

    def _handle_phrase(self, text: str):
        self._pending_sentences = None
        self._ensure_per_pdf_paths()
        self._cache = load_cache(self._cache_path)
        tgt = lookup_phrase(self._cache, self._current_file, text,
                             is_dual=self._is_dual)
        if tgt is not None:
            self.cache_hint.setText("[短语缓存命中]")
            self.cache_hint.show()
            self._show_result(tgt)
            return
        self.cache_hint.hide()
        if self.fast_auto_check.isChecked():
            self._do_fast_translate(text, cache_mode="phrase")

    # ── Sentence auto-complete handler (two-phase) ───────────────

    def _handle_sentence_auto_complete(self, lo: int, hi: int):
        """Phase 1: expand selection, save pending sentences.
        Does NOT display or translate. Phase 2 triggers separately."""
        words = self._pdf_viewer.words
        expanded = expand_to_sentence(words, lo, hi)
        new_lo, new_hi = expanded.new_lo, expanded.new_hi

        self._pdf_viewer.set_highlight_range(new_lo, new_hi)

        expanded_raw = self._pdf_viewer.get_text_by_range(new_lo, new_hi)
        expanded_text = clean_newlines(expanded_raw)
        if not expanded_text:
            self._pending_sentences = None
            return

        self._last_pdf_selection = expanded_text

        sub_sentences = split_sentences(words, new_lo, new_hi)
        if self._pdf_viewer.is_dual_column:
            transform_dual_column_coords(sub_sentences)
        for i, sub in enumerate(sub_sentences):
            sub["is_head_fragment"] = (i == 0 and expanded.head_fragment)
            sub["is_tail_fragment"] = (i == len(sub_sentences) - 1 and expanded.tail_fragment)

        self._pending_sentences = {
            "head_fragment": expanded.head_fragment,
            "tail_fragment": expanded.tail_fragment,
            "sentences": sub_sentences,
            "expanded_text": expanded_text,
        }

        if self.fast_auto_check.isChecked():
            self._execute_auto_translate()

    def _execute_auto_translate(self):
        """Phase 2: gap detection + translate using self._pending_sentences."""
        pending = self._pending_sentences
        if not pending:
            return
        sub_sentences = pending["sentences"]
        if not sub_sentences:
            return

        self._ensure_per_pdf_paths()

        first_sub = sub_sentences[0]
        last_sub = sub_sentences[-1]
        sp, sy = first_sub["start_page"], first_sub["start_y_pct"]
        ep, ey = last_sub["end_page"], last_sub["end_y_pct"]
        sx = first_sub["start_x_pct"]
        ex = last_sub["end_x_pct"]

        self._cache = load_cache(self._cache_path)
        overlapping = find_overlapping_entries(
            self._cache, self._current_file, sp, sy, ep, ey,
            is_dual=self._is_dual,
        )

        # Collect all cache sub-sentences from overlapping entries
        cache_subs: list[dict] = []
        if overlapping:
            group = _get_group(self._cache, self._current_file, self._is_dual)
            for idx in overlapping:
                entry = group["sentences"][idx]
                for sub in entry.get("sentences", []):
                    cache_subs.append(sub)

        # Detect gaps: pending sub not covered by any cache sub (strict overlap)
        gap_indices: list[int] = []
        for i, psub in enumerate(sub_sentences):
            covered = False
            for csub in cache_subs:
                if not coord_le_tolerant(
                    csub["end_page"], csub["end_y_pct"], csub["end_x_pct"],
                    psub["start_page"], psub["start_y_pct"], psub["start_x_pct"],
                ) and not coord_le_tolerant(
                    psub["end_page"], psub["end_y_pct"], psub["end_x_pct"],
                    csub["start_page"], csub["start_y_pct"], csub["start_x_pct"],
                ):
                    covered = True
                    break
            if not covered:
                gap_indices.append(i)

        if not gap_indices:
            # ── No gaps: extract from cache ─────────────────────────
            parts = []
            for csub in cache_subs:
                if not coord_le_tolerant(
                    ep, ey, ex,
                    csub["start_page"], csub["start_y_pct"], csub["start_x_pct"],
                ) and not coord_le_tolerant(
                    csub["end_page"], csub["end_y_pct"], csub["end_x_pct"],
                    sp, sy, sx,
                ):
                    if csub["tgt"]:
                        parts.append(csub["tgt"])
            if overlapping:
                merge_entries(self._cache, self._current_file, overlapping,
                              is_dual=self._is_dual)
                save_cache(self._cache, self._cache_path)
            result = "".join(parts) if parts else pending["expanded_text"]
            self.cache_hint.setText("[缓存命中]")
            self.cache_hint.show()
            self._show_result(result)
            return

        # ── Has gaps ────────────────────────────────────────────────
        gap_src = " ".join(sub_sentences[i]["src"] for i in gap_indices)
        self._show_gap_dialog(gap_src, pending, gap_indices, overlapping)

    # ── Gap dialog ───────────────────────────────────────────────

    def _show_gap_dialog(self, gap_src: str, pending: dict,
                         gap_indices: list[int], overlapping: list[int]):
        dlg = QDialog(self)
        dlg.setWindowTitle("翻译确认")
        dlg.setMinimumWidth(400)
        layout = QVBoxLayout(dlg)

        truncated = gap_src[:200] + "…" if len(gap_src) > 200 else gap_src
        layout.addWidget(QLabel(f"部分内容已翻译，新内容：\n{truncated}"))
        layout.addWidget(QLabel("请选择翻译方式："))

        btn_row = QHBoxLayout()
        inc_btn = QPushButton("增量翻译")
        full_btn = QPushButton("全量重新翻译")
        btn_row.addWidget(inc_btn)
        btn_row.addWidget(full_btn)
        layout.addLayout(btn_row)

        inc_btn.clicked.connect(lambda: (
            dlg.accept(),
            self._do_auto_translate(
                gap_src, pending, gap_indices, overlapping, is_incremental=True,
            ),
        ))
        full_btn.clicked.connect(lambda: (
            dlg.accept(),
            self._do_auto_translate(
                pending["expanded_text"], pending, gap_indices, overlapping,
                is_incremental=False,
            ),
        ))

        dlg.setModal(False)
        dlg.show()
        self._gap_dialog = dlg  # keep reference to prevent GC

    # ── Auto translate worker + callback ─────────────────────────

    def _do_auto_translate(self, text: str, pending: dict,
                           gap_indices: list[int], overlapping: list[int],
                           is_incremental: bool):
        """Send text to LLM and handle result via _on_auto_done."""
        url, key, model, prompt = self._get_llm_kwargs("fast")
        if not url or not key or not model:
            self._show_result("[错误] 请先配置 URL / Key / Model")
            return

        self._show_translating()
        self._run_worker(
            _FastTranslateWorker(url, key, model, text, prompt),
            lambda ok, data: self._on_auto_done(
                ok, data, pending, gap_indices, overlapping, is_incremental,
            ),
        )

    def _on_auto_done(self, ok, data, pending: dict,
                      gap_indices: list[int], overlapping: list[int],
                      is_incremental: bool):
        if not ok:
            self._show_result(f"[翻译失败] {data}")
            return

        sub_sentences = pending["sentences"]

        self._cache = load_cache(self._cache_path)

        if is_incremental:
            # Fill gap sub-sentences
            gap_tgt_parts = split_translation(data, len(gap_indices))
            for gi, idx in enumerate(gap_indices):
                if gi < len(gap_tgt_parts):
                    sub_sentences[idx]["tgt"] = gap_tgt_parts[gi]
            merged = "".join(s.get("tgt", "") for s in sub_sentences)
            self._show_result(merged)
        else:
            # Full: replace all sub-sentence tgts
            all_tgt_parts = split_translation(data, len(sub_sentences))
            for i in range(len(sub_sentences)):
                if i < len(all_tgt_parts):
                    sub_sentences[i]["tgt"] = all_tgt_parts[i]

        # Build entry and write cache
        new_entry = {
            "src": pending["expanded_text"],
            "tgt": "".join(s["tgt"] for s in sub_sentences),
            "head_fragment": pending["head_fragment"],
            "tail_fragment": pending["tail_fragment"],
            "sentences": sub_sentences,
        }
        add_sentence_entry(self._cache, self._current_file, new_entry,
                           is_dual=self._is_dual)

        # Merge overlapping entries into one
        if overlapping:
            # Re-read overlapping after adding new entry
            first_sub = sub_sentences[0]
            last_sub = sub_sentences[-1]
            sp, sy = first_sub["start_page"], first_sub["start_y_pct"]
            ep, ey = last_sub["end_page"], last_sub["end_y_pct"]
            overlapping = find_overlapping_entries(
                self._cache, self._current_file, sp, sy, ep, ey,
                is_dual=self._is_dual,
            )
            if len(overlapping) >= 2:
                merge_entries(self._cache, self._current_file, overlapping,
                              is_dual=self._is_dual)

        save_cache(self._cache, self._cache_path)
        self._fragment_self_merge()

    # ── Sentence manual handler (two-phase) ──────────────────────

    def _handle_sentence_manual(self, lo: int, hi: int, text: str):
        """Phase 1: split sentences, save pending. No cache, no LLM, no display."""
        word_texts = self._pdf_viewer.get_word_texts()
        words = self._pdf_viewer.words
        head_frag = lo == 0 or not is_sentence_end(word_texts[lo - 1])
        tail_frag = not is_sentence_end(word_texts[hi])

        sub_sentences = split_sentences(words, lo, hi)
        if self._pdf_viewer.is_dual_column:
            transform_dual_column_coords(sub_sentences)
        for i, sub in enumerate(sub_sentences):
            sub["is_head_fragment"] = (i == 0 and head_frag)
            sub["is_tail_fragment"] = (i == len(sub_sentences) - 1 and tail_frag)

        self._pending_sentences = {
            "head_fragment": head_frag,
            "tail_fragment": tail_frag,
            "sentences": sub_sentences,
            "expanded_text": text,
        }

        if self.fast_auto_check.isChecked():
            self._execute_sentence_manual()

    def _execute_sentence_manual(self):
        """Phase 2: cache check → extract or LLM translate → cache write."""
        pending = self._pending_sentences
        if not pending:
            return
        sub_sentences = pending["sentences"]
        if not sub_sentences:
            return

        self._ensure_per_pdf_paths()

        first_sub = sub_sentences[0]
        last_sub = sub_sentences[-1]
        sp, sx, sy = first_sub["start_page"], first_sub["start_x_pct"], first_sub["start_y_pct"]
        ep, ex, ey = last_sub["end_page"], last_sub["end_x_pct"], last_sub["end_y_pct"]

        self._cache = load_cache(self._cache_path)
        containing = find_containing_entries(
            self._cache, self._current_file, sp, sy, sx, ep, ey, ex,
            is_dual=self._is_dual,
        )

        if containing:
            group = _get_group(self._cache, self._current_file, self._is_dual)
            entry = group["sentences"][containing[0]]
            parts = []
            for sub in entry.get("sentences", []):
                if not coord_le_tolerant(
                    ep, ey, ex,
                    sub["start_page"], sub["start_y_pct"], sub["start_x_pct"],
                ) and not coord_le_tolerant(
                    sub["end_page"], sub["end_y_pct"], sub["end_x_pct"],
                    sp, sy, sx,
                ):
                    if sub["tgt"]:
                        parts.append(sub["tgt"])
            result = "".join(parts) if parts else entry["tgt"]
            self.cache_hint.setText("[句子缓存命中]")
            self.cache_hint.show()
            self._show_result(result)
            return

        self.cache_hint.hide()
        src_text = " ".join(s["src"] for s in sub_sentences)
        self._do_manual_translate(src_text, pending)

    def _do_manual_translate(self, text: str, pending: dict):
        """Send full selection text to LLM, handle via _on_manual_done."""
        url, key, model, prompt = self._get_llm_kwargs("fast")
        if not url or not key or not model:
            self._show_result("[错误] 请先配置 URL / Key / Model")
            return

        self._show_translating()
        self._run_worker(
            _FastTranslateWorker(url, key, model, text, prompt),
            lambda ok, data: self._on_manual_done(ok, data, pending),
        )

    def _on_manual_done(self, ok, data, pending: dict):
        """Split LLM result, write cache with overlap merge."""
        if not ok:
            self._show_result(f"[翻译失败] {data}")
            return

        sub_sentences = pending["sentences"]
        self._show_result(data)

        tgt_parts = split_translation(data, len(sub_sentences))
        for i in range(len(sub_sentences)):
            if i < len(tgt_parts):
                sub_sentences[i]["tgt"] = tgt_parts[i]

        new_entry = {
            "src": pending["expanded_text"],
            "tgt": "".join(s["tgt"] for s in sub_sentences),
            "head_fragment": pending["head_fragment"],
            "tail_fragment": pending["tail_fragment"],
            "sentences": sub_sentences,
        }

        self._cache = load_cache(self._cache_path)

        first_sub = sub_sentences[0]
        last_sub = sub_sentences[-1]
        sp, sy = first_sub["start_page"], first_sub["start_y_pct"]
        ep, ey = last_sub["end_page"], last_sub["end_y_pct"]
        overlapping = find_overlapping_entries(
            self._cache, self._current_file, sp, sy, ep, ey,
            is_dual=self._is_dual,
        )

        if not overlapping:
            add_sentence_entry(self._cache, self._current_file, new_entry,
                               is_dual=self._is_dual)
        else:
            group = _get_group(self._cache, self._current_file, self._is_dual)

            # Collect sub-sentences from overlapping cache entries
            merged_subs: list[dict] = []
            for idx in overlapping:
                entry = group["sentences"][idx]
                for sub in entry.get("sentences", []):
                    merged_subs.append(sub)

            # Replace matching old subs with new subs by start coordinate
            for new_sub in sub_sentences:
                replaced = False
                for old_sub in merged_subs:
                    if (
                        coord_le_tolerant(
                            new_sub["start_page"], new_sub["start_y_pct"], new_sub["start_x_pct"],
                            old_sub["start_page"], old_sub["start_y_pct"], old_sub["start_x_pct"],
                        ) and coord_le_tolerant(
                            old_sub["start_page"], old_sub["start_y_pct"], old_sub["start_x_pct"],
                            new_sub["start_page"], new_sub["start_y_pct"], new_sub["start_x_pct"],
                        )
                    ):
                        old_sub["tgt"] = new_sub["tgt"]
                        replaced = True
                        break
                if not replaced:
                    merged_subs.append(new_sub)

            merged_subs.sort(key=lambda s: (
                s["start_page"], s["start_y_pct"], s["start_x_pct"],
            ))

            merged = {
                "src": " ".join(s["src"] for s in merged_subs),
                "tgt": "".join(s["tgt"] for s in merged_subs if s["tgt"]),
                "head_fragment": merged_subs[0]["is_head_fragment"] if merged_subs else False,
                "tail_fragment": merged_subs[-1]["is_tail_fragment"] if merged_subs else False,
                "sentences": merged_subs,
            }

            # Delete old overlapping entries (in reverse order)
            for i in sorted(overlapping, reverse=True):
                group["sentences"].pop(i)
            group["sentences"].append(merged)

        save_cache(self._cache, self._cache_path)
        self._fragment_self_merge()

    # ── Translate + cache write ──────────────────────────────────

    def _do_fast_translate(self, text: str, cache_mode: str = "phrase",
                            sentence_meta: dict | None = None):
        if not text or not self._current_file:
            return

        url, key, model, prompt = self._get_llm_kwargs("fast")
        if not url or not key or not model:
            self._show_result("[错误] 请先配置 URL / Key / Model")
            return

        captured_src = text
        self._show_translating()
        self._run_worker(
            _FastTranslateWorker(url, key, model, text, prompt),
            lambda ok, data, cm=cache_mode, sm=sentence_meta, st=captured_src: (
                self._on_fast_done(ok, data, cm, sm, st)
            ),
        )

    def _on_fast_done(self, ok, data, cache_mode: str, sentence_meta: dict | None,
                      src: str = ""):
        if ok:
            self._show_result(data)
            if self._current_file and src:
                self._cache = load_cache(self._cache_path)
                if cache_mode == "phrase":
                    add_phrase_entry(self._cache, self._current_file, src, data,
                                     is_dual=self._is_dual)
                else:
                    entry = self._build_sentence_entry(src, data, sentence_meta)
                    if entry is not None:
                        add_sentence_entry(self._cache, self._current_file, entry,
                                           is_dual=self._is_dual)
                    save_cache(self._cache, self._cache_path)
                    self._fragment_self_merge()
                    return
                save_cache(self._cache, self._cache_path)
        else:
            self._show_result(f"[翻译失败] {data}")

    def _build_sentence_entry(self, src: str, tgt: str, meta: dict | None) -> dict:
        if meta is None:
            meta = {}
        sub_sentences = list(meta.get("sentences", []))
        if not sub_sentences:
            sub_sentences = [{
                "start_page": 0, "start_x_pct": 0, "start_y_pct": 0,
                "end_page": 0, "end_x_pct": 0, "end_y_pct": 0,
                "src": src,
                "tgt": tgt,
                "is_head_fragment": False,
                "is_tail_fragment": False,
            }]
        else:
            for i, sub in enumerate(sub_sentences):
                sub["tgt"] = ""
                sub["is_head_fragment"] = (i == 0 and meta.get("head_fragment", False))
                sub["is_tail_fragment"] = (i == len(sub_sentences) - 1 and meta.get("tail_fragment", False))
            tgt_parts = split_translation(tgt, len(sub_sentences))
            for i, tgt_part in enumerate(tgt_parts):
                if i < len(sub_sentences):
                    sub_sentences[i]["tgt"] = tgt_part
        return {
            "src": src,
            "tgt": "".join(s["tgt"] for s in sub_sentences),
            "head_fragment": meta.get("head_fragment", False),
            "tail_fragment": meta.get("tail_fragment", False),
            "sentences": sub_sentences,
        }

    # ── Fragment self-merge ──────────────────────────────────────

    def _fragment_self_merge(self):
        """Recursively merge fragment caches that share sentence boundaries."""
        if not self._current_file:
            return
        cache = load_cache(self._cache_path)
        changed = True
        while changed:
            changed = False
            result = find_mergeable_fragments(cache, self._current_file,
                                               is_dual=self._is_dual)
            if result is not None:
                idx_a, idx_b, _ = result
                merge_entries(cache, self._current_file, [idx_a, idx_b],
                              is_dual=self._is_dual)
                changed = True
        if changed:
            save_cache(cache, self._cache_path)
            self._cache = cache

    def set_current_file(self, path: str | None):
        self._current_file = path
        if path:
            entry = get_or_create_pdf_history_entry(self._config, path)
            cfg = entry.setdefault("config", {})

            # ── Cache file ──
            cache_file = cfg.get("cache_file")
            if cache_file:
                self._cache_path = cache_file
                self._cache = load_cache(cache_file)
            else:
                self._cache_path = None
                self._cache = {"format_version": 2}

            # ── Isolate file ──
            if self._pdf_viewer is not None:
                isolate_file = cfg.get("isolate_file")
                if isolate_file:
                    self._pdf_viewer.set_isolate_path(isolate_file)
                    self._pdf_viewer.load_zones()
                else:
                    self._pdf_viewer.set_isolate_path(None)

                # ── Dual column ──
                dual_col = cfg.get("dual_column", False)
                self._pdf_viewer.set_dual_column_silent(dual_col)
        else:
            self._cache_path = None
            self._cache = {}

    def _ensure_per_pdf_paths(self):
        """Lazily create default cache and isolate file paths on first use."""
        if not self._current_file:
            return
        entry = get_or_create_pdf_history_entry(self._config, self._current_file)
        cfg = entry.setdefault("config", {})

        if self._cache_path is None:
            cache_file = auto_generate_per_pdf_path(self._current_file, "_cache")
            set_pdf_config_path(self._config, self._current_file, "cache_file", cache_file)
            save_config(self._config)
            self._cache_path = cache_file

        if self._pdf_viewer is not None and self._pdf_viewer._isolate_path is None:
            isolate_file = cfg.get("isolate_file")
            if not isolate_file:
                isolate_file = auto_generate_per_pdf_path(self._current_file, "_isolate")
                set_pdf_config_path(self._config, self._current_file, "isolate_file", isolate_file)
                save_config(self._config)
            self._pdf_viewer.set_isolate_path(isolate_file)

    def set_last_selection(self, text: str):
        self._last_pdf_selection = text

    # ── Persist translate (manual input → file) ────────────────────

    def _start_persist_translate(self):
        errors = []
        user_text = self.persist_input.toPlainText().strip()
        if not user_text:
            errors.append("输入文本不能为空")

        prompt_path = self.prompt_combo.currentText().strip()
        prompt_content = ""
        if prompt_path:
            try:
                prompt_content = load_prompt(prompt_path)
            except Exception as e:
                errors.append(str(e))

        url, key, model, _ = self._get_llm_kwargs("persist")
        if not url:
            errors.append("Base URL 不能为空")
        if not key:
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

        self.persist_log.append("开始翻译...")
        self.persist_translate_btn.setEnabled(False)
        self._persist_current_paths()

        self._run_worker(
            _PersistTranslateWorker(
                llm_kwargs={"base_url": url, "api_key": key, "model": model},
                prompt_content=prompt_content,
                user_text=user_text,
                output_kwargs=output_kwargs,
            ),
            self._on_persist_done,
        )

    def _on_persist_done(self, ok, data):
        self.persist_translate_btn.setEnabled(True)
        if ok:
            # persist validated model
            url, key, model, _ = self._get_llm_kwargs("persist")
            if url and model:
                add_base_url(self._config, url)
                set_url_key(self._config, url, key)
                add_model_to_url(self._config, url, model)
                save_config(self._config)
                self._refresh_persist_model_combo()
            self.persist_log.append("翻译完成")
            self.persist_log.append(f"---\n{data}\n---")
        else:
            self.persist_log.append(f"翻译失败: {data}")
            QMessageBox.critical(self, "翻译失败", data)

    # ── Management dialogs ─────────────────────────────────────────

    def _manage_urls(self):
        dlg = _UrlManageDialog(self._config, self)
        dlg.exec()
        save_config(self._config)
        self._refresh_url_combo()
        self._refresh_fast_model_combo()
        self._refresh_persist_model_combo()

    def _manage_cache(self):
        self._ensure_per_pdf_paths()
        dlg = _CacheManageDialog(self._cache_path, self)
        dlg.exec()

    # ── File pickers ───────────────────────────────────────────────

    def _pick_prompt(self):
        f, _ = QFileDialog.getOpenFileName(
            self, "选择系统提示词文件", "", "文本文件 (*.txt);;所有文件 (*)"
        )
        if f:
            self.prompt_combo.setCurrentText(f)
            self.prompt_combo.setEditText(f)
            add_prompt_file(self._config, f)
            save_config(self._config)
            self._refresh_persist_prompt_combo()

    def _pick_fast_prompt(self):
        f, _ = QFileDialog.getOpenFileName(
            self, "选择系统提示词文件", "", "文本文件 (*.txt);;所有文件 (*)"
        )
        if f:
            self.fast_prompt_combo.setCurrentText(f)
            self.fast_prompt_combo.setEditText(f)
            add_prompt_file(self._config, f)
            save_config(self._config)
            self._refresh_fast_prompt_combo()

    def _pick_dir(self):
        d = QFileDialog.getExistingDirectory(self, "选择输出目录")
        if d:
            self.dir_combo.setCurrentText(d)
            self.dir_combo.setEditText(d)

    def _pick_append_file(self):
        f, _ = QFileDialog.getOpenFileName(
            self, "选择 Markdown 文件", "", "Markdown (*.md)"
        )
        if f:
            self.append_combo.setCurrentText(f)
            self.append_combo.setEditText(f)

    def _on_persist_mode_changed(self):
        if self.new_radio.isChecked():
            self.new_widget.show()
            self.path_preview.show()
            self.append_widget.hide()
        else:
            self.new_widget.hide()
            self.path_preview.hide()
            self.append_widget.show()

    def _manual_clean(self):
        text = self.persist_input.toPlainText()
        self.persist_input.setPlainText(clean_newlines(text))
        self.persist_log.append("已手动清洗换行符")

    # ── Combo refresh ──────────────────────────────────────────────

    def _refresh_url_combo(self):
        self._suppress_url_change = True
        current = self.url_combo.currentText()
        self.url_combo.clear()
        urls = list(self._config.get("base_urls", {}).keys())
        self.url_combo.addItems(urls)
        if current:
            self.url_combo.setCurrentText(current)
        self._suppress_url_change = False

    def _refresh_fast_model_combo(self):
        url = self.url_combo.currentText().strip()
        current = self.fast_model_combo.currentText()
        self.fast_model_combo.clear()
        if url:
            self.fast_model_combo.addItems(get_url_models(self._config, url))
        if current:
            self.fast_model_combo.setCurrentText(current)

    def _refresh_persist_model_combo(self):
        url = self.url_combo.currentText().strip()
        current = self.persist_model_combo.currentText()
        self.persist_model_combo.clear()
        if url:
            self.persist_model_combo.addItems(get_url_models(self._config, url))
        if current:
            self.persist_model_combo.setCurrentText(current)

    def _refresh_fast_prompt_combo(self):
        self._suppress_fast_save = True
        current = self.fast_prompt_combo.currentText()
        self.fast_prompt_combo.clear()
        files = self._config.get("prompt_files", [])
        self.fast_prompt_combo.addItems(files)
        if current:
            self.fast_prompt_combo.setCurrentText(current)
        self._suppress_fast_save = False

    def _refresh_persist_prompt_combo(self):
        self._suppress_fast_save = True
        current = self.prompt_combo.currentText()
        self.prompt_combo.clear()
        files = self._config.get("prompt_files", [])
        self.prompt_combo.addItems(files)
        if current:
            self.prompt_combo.setCurrentText(current)
        self._suppress_fast_save = False

    # ── Config helpers ─────────────────────────────────────────────

    def _get_llm_kwargs(self, tab: str):
        url = self.url_combo.currentText().strip()
        key = self.key_edit.text().strip()
        if tab == "fast":
            model = self.fast_model_combo.currentText().strip()
            prompt = self.fast_prompt_combo.currentText().strip()
        else:
            model = self.persist_model_combo.currentText().strip()
            prompt = ""
        return url, key, model, prompt

    def _persist_validated_model(self, tab: str):
        url, key, model, _ = self._get_llm_kwargs(tab)
        if url and model:
            add_base_url(self._config, url)
            set_url_key(self._config, url, key)
            add_model_to_url(self._config, url, model)
            save_config(self._config)
            if tab == "fast":
                self._refresh_fast_model_combo()
            else:
                self._refresh_persist_model_combo()

    def _persist_current_paths(self):
        c = self._config
        d = self.dir_combo.currentText().strip()
        if d:
            c["current_output_dir"] = d
        f = self.append_combo.currentText().strip()
        if f:
            c["current_append_file"] = f
        p = self.prompt_combo.currentText().strip()
        if p:
            c["current_prompt_file"] = p
        c["current_url"] = self.url_combo.currentText().strip()
        c["current_model"] = self.persist_model_combo.currentText().strip()
        c["output_mode"] = "new" if self.new_radio.isChecked() else "append"
        c["mode_b_current_url"] = self.url_combo.currentText().strip()
        c["mode_b_current_model"] = self.fast_model_combo.currentText().strip()
        c["mode_b_prompt"] = self.fast_prompt_combo.currentText().strip()
        c["mode_b_auto_translate"] = self.fast_auto_check.isChecked()
        save_config(c)

    def save_config(self):
        self._persist_current_paths()

    def _apply_result_font_size(self, size: int):
        font = self.fast_result.font()
        font.setPointSize(size)
        self.fast_result.setFont(font)

    def _on_result_font_changed(self, value: int):
        self._apply_result_font_size(value)
        self._config["result_font_size"] = value
        save_config(self._config)

    def _save_fast_config(self):
        if self._suppress_fast_save:
            return
        self._config["mode_b_current_model"] = self.fast_model_combo.currentText().strip()
        self._config["mode_b_prompt"] = self.fast_prompt_combo.currentText().strip()
        self._config["mode_b_auto_translate"] = self.fast_auto_check.isChecked()
        self._config["result_font_size"] = self.result_font_spin.value()
        save_config(self._config)

    def _save_persist_prompt(self):
        if self._suppress_fast_save:
            return
        p = self.prompt_combo.currentText().strip()
        if p:
            self._config["current_prompt_file"] = p
        save_config(self._config)

    def _restore(self):
        self._suppress_fast_save = True
        c = self._config

        # URL combo
        self._suppress_url_change = True
        urls = list(c.get("base_urls", {}).keys())
        self.url_combo.clear()
        self.url_combo.addItems(urls)
        cur_url = c.get("current_url", "")
        if cur_url:
            self.url_combo.setCurrentText(cur_url)
        elif urls:
            self.url_combo.setCurrentText(urls[0])
        self._suppress_url_change = False

        cur_url = self.url_combo.currentText().strip()
        self._suppress_key_change = True
        if cur_url:
            self.key_edit.setText(get_url_key(c, cur_url))
        self._suppress_key_change = False

        # Fast model
        fast_models = get_url_models(c, cur_url)
        self.fast_model_combo.clear()
        self.fast_model_combo.addItems(fast_models)
        mb_model = c.get("mode_b_current_model", "")
        if mb_model:
            self.fast_model_combo.setCurrentText(mb_model)
        elif fast_models:
            self.fast_model_combo.setCurrentText(fast_models[0])

        # Fast prompt
        prompt_files = c.get("prompt_files", [])
        self.fast_prompt_combo.clear()
        self.fast_prompt_combo.addItems(prompt_files)
        mb_prompt = c.get("mode_b_prompt", "")
        if mb_prompt:
            self.fast_prompt_combo.setCurrentText(mb_prompt)
        elif prompt_files:
            self.fast_prompt_combo.setCurrentText(prompt_files[0])

        # Result font size
        font_size = c.get("result_font_size", 14)
        self.result_font_spin.setValue(font_size)
        self._apply_result_font_size(font_size)

        # Auto-translate checkbox
        self.fast_auto_check.setChecked(c.get("mode_b_auto_translate", True))

        # Persist model
        persist_models = get_url_models(c, cur_url)
        self.persist_model_combo.clear()
        self.persist_model_combo.addItems(persist_models)
        cur_model = c.get("current_model", "")
        if cur_model:
            self.persist_model_combo.setCurrentText(cur_model)
        elif persist_models:
            self.persist_model_combo.setCurrentText(persist_models[0])

        # Prompt
        prompt_files = c.get("prompt_files", [])
        self.prompt_combo.clear()
        self.prompt_combo.addItems(prompt_files)
        cur_prompt = c.get("current_prompt_file", "")
        if cur_prompt:
            self.prompt_combo.setCurrentText(cur_prompt)
        elif prompt_files:
            self.prompt_combo.setCurrentText(prompt_files[0])

        # Output mode
        if c.get("output_mode", "new") == "append":
            self.append_radio.setChecked(True)
        else:
            self.new_radio.setChecked(True)
        self._on_persist_mode_changed()

        # Dir history
        dirs = c.get("output_dirs", [])
        self.dir_combo.clear()
        self.dir_combo.addItems(dirs)
        if c.get("current_output_dir", ""):
            self.dir_combo.setCurrentText(c["current_output_dir"])
        elif dirs:
            self.dir_combo.setCurrentText(dirs[0])

        # Append history
        append_files = c.get("append_files", [])
        self.append_combo.clear()
        self.append_combo.addItems(append_files)
        if c.get("current_append_file", ""):
            self.append_combo.setCurrentText(c["current_append_file"])
        elif append_files:
            self.append_combo.setCurrentText(append_files[0])

        self._suppress_fast_save = False

    # ── Worker helper ──────────────────────────────────────────────

    def _run_worker(self, worker, on_finish):
        if self._worker and self._worker.isRunning():
            self._worker.terminate()
            self._worker.wait()
        self._worker = worker
        worker.finished.connect(on_finish)
        worker.finished.connect(lambda: setattr(self, "_worker", None))
        worker.start()
