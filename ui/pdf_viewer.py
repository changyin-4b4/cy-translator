import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

import fitz  # PyMuPDF
import shiboken6
from PySide6.QtCore import QEvent, Qt, QPointF, QRectF, QThread, QTimer, Signal
from PySide6.QtGui import (
    QBrush,
    QColor,
    QCursor,
    QIcon,
    QImage,
    QKeySequence,
    QPainter,
    QPen,
    QPixmap,
    QShortcut,
)
from PySide6.QtWidgets import (
    QApplication,
    QGraphicsPixmapItem,
    QGraphicsProxyWidget,
    QGraphicsRectItem,
    QGraphicsScene,
    QGraphicsView,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QTextEdit,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)
from services.note_store import (
    load_notes as _load_notes_from_file,
    save_notes as _save_notes_to_file,
    DEFAULT_FONT_SIZE,
    DEFAULT_NOTE_WIDTH,
    DEFAULT_NOTE_HEIGHT,
)
from utils.paste_cleaner import clean_newlines

# ── Constants ─────────────────────────────────────────────────────────

DPI = 250
PAGE_GAP = 16

HIGHLIGHT_COLOR = QColor(0, 120, 255, 60)
HIGHLIGHT_BORDER = QColor(0, 100, 220, 140)

ZONE_READING_COLOR = QColor(128, 128, 128, 25)
ZONE_MANAGING_COLOR = QColor(128, 128, 128, 100)
ZONE_SELECTED_COLOR = QColor(100, 100, 100, 140)
ZONE_PREVIEW_COLOR = QColor(255, 0, 0, 60)
HANDLE_SIZE = 8

MODE_READING = 0
MODE_FRAMING = 1
MODE_MANAGING = 2
MODE_NOTING = 3


# ── Data types ────────────────────────────────────────────────────────

@dataclass
class _Word:
    """Word bbox in percentage coordinates (0-1, page-local)."""
    idx: int
    page_idx: int
    x0_pct: float
    y0_pct: float
    x1_pct: float
    y1_pct: float
    text: str
    size: float = 0.0
    flags: int = 0

    @property
    def center_x(self) -> float:
        return (self.x0_pct + self.x1_pct) / 2.0

    @property
    def center_y(self) -> float:
        return (self.y0_pct + self.y1_pct) / 2.0


# ── Note Proxy (edge-drag resize at QGraphicsProxyWidget level) ──────

class _NoteProxy(QGraphicsProxyWidget):
    """Proxy that handles edge-drag resize before forwarding events to widget."""

    resize_requested = Signal(int, float, float, int, int)  # note_idx, orig_w, orig_h, cum_dx, cum_dy

    EDGE = 18

    def __init__(self, note_idx: int, parent=None):
        super().__init__(parent)
        self._note_idx = note_idx
        self._resizing = False
        self._resize_origin = None
        self._resize_orig_w = 0
        self._resize_orig_h = 0

    # ── edge helpers ────────────────────────────────────────────────

    def _edge_flags(self, pos):
        w = self.size().width()
        h = self.size().height()
        return pos.x() >= w - self.EDGE, pos.y() >= h - self.EDGE

    # ── event overrides ─────────────────────────────────────────────

    def hoverMoveEvent(self, event):
        on_right, on_bottom = self._edge_flags(event.pos())
        super().hoverMoveEvent(event)

    def hoverLeaveEvent(self, event):
        super().hoverLeaveEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            on_right, on_bottom = self._edge_flags(event.pos())
            if on_right or on_bottom:
                self._resizing = True
                self._resize_origin = event.screenPos()
                self._resize_orig_w = self.size().width()
                self._resize_orig_h = self.size().height()
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._resizing:
            cur = event.screenPos()
            dx = int(cur.x() - self._resize_origin.x())
            dy = int(cur.y() - self._resize_origin.y())
            self.resize_requested.emit(
                self._note_idx, self._resize_orig_w, self._resize_orig_h,
                dx, dy)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._resizing:
            self._resizing = False
            self._resize_origin = None
            event.accept()
            return
        super().mouseReleaseEvent(event)


# ── Note Editor (plain text editing, no resize logic) ─────────────────

class _NoteEditor(QTextEdit):
    """Plain QTextEdit for note content. Resize is handled by _NoteProxy."""
    pass


# ── TOC Sidebar Panel ──────────────────────────────────────────────────

class TOCPanel(QWidget):
    """Collapsible left-sidebar TOC panel. Fixed 200px expanded, 8px collapsed."""

    toc_collapsed_changed = Signal(bool)
    page_navigate_requested = Signal(int)  # page_idx (0-based)

    EXPANDED_WIDTH = 200
    COLLAPSED_WIDTH = 15

    def __init__(self, parent=None):
        super().__init__(parent)
        self._collapsed = True
        self._has_toc = False

        self.setFixedWidth(self.COLLAPSED_WIDTH)
        self.hide()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # ── Expanded content ──────────────────────────────────────
        self._content = QWidget()
        content_layout = QVBoxLayout(self._content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(0)

        header = QWidget()
        header.setFixedHeight(28)
        header.setStyleSheet("background-color: #3a3a3a;")
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(6, 0, 2, 0)
        header_label = QLabel("目录")
        header_label.setStyleSheet("color: #ccc; font-weight: bold;")
        header_layout.addWidget(header_label)
        header_layout.addStretch()
        collapse_btn = QPushButton("◀")
        collapse_btn.setFixedSize(20, 20)
        collapse_btn.setStyleSheet(
            "QPushButton { border: none; color: #aaa; }"
            "QPushButton:hover { color: #fff; }"
        )
        collapse_btn.clicked.connect(lambda: self.set_collapsed(True))
        header_layout.addWidget(collapse_btn)
        content_layout.addWidget(header)

        self._tree = QTreeWidget()
        self._tree.setHeaderHidden(True)
        self._tree.setRootIsDecorated(True)
        self._tree.setIndentation(16)
        self._tree.setStyleSheet(
            "QTreeWidget { background-color: #2a2a2a; color: #ccc; border: none; font-size: 16px; }"
        )
        self._tree.itemClicked.connect(self._on_item_clicked)
        content_layout.addWidget(self._tree)

        layout.addWidget(self._content)

        # ── Collapsed strip ───────────────────────────────────────
        self._strip = QPushButton()
        self._strip.setFixedWidth(self.COLLAPSED_WIDTH)
        self._strip.setCursor(Qt.CursorShape.PointingHandCursor)
        self._strip.setSizePolicy(
            QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)
        self._strip.setMinimumHeight(0)
        self._strip.setStyleSheet("""
            QPushButton {
                background-color: #4a4a4a;
                border: none;
            }
            QPushButton:hover {
                background-color: #6a90b0;
            }
        """)
        self._strip.clicked.connect(lambda: self.set_collapsed(False))
        layout.addWidget(self._strip)

        self._content.hide()

    def load_toc(self, doc):
        """Extract PDF outline via PyMuPDF and populate the tree widget."""
        self._tree.clear()
        if doc is None:
            self._has_toc = False
            self.hide()
            return
        toc = doc.get_toc()
        if not toc:
            self._has_toc = False
            self.hide()
            return
        self._has_toc = True
        parent_stack: dict[int, QTreeWidgetItem] = {}
        for level, title, page in toc:
            page_idx = max(0, min(page - 1, len(doc) - 1))
            item = QTreeWidgetItem()
            item.setText(0, title)
            item.setData(0, Qt.ItemDataRole.UserRole, page_idx)
            if level <= 1:
                self._tree.addTopLevelItem(item)
            else:
                parent = None
                for lvl in range(level - 1, 0, -1):
                    if lvl in parent_stack:
                        parent = parent_stack[lvl]
                        break
                if parent:
                    parent.addChild(item)
                else:
                    self._tree.addTopLevelItem(item)
            parent_stack[level] = item
        self._collapsed = True
        self.show()
        self._apply_state()

    def set_collapsed(self, collapsed: bool):
        self._collapsed = collapsed
        self._apply_state()
        self.toc_collapsed_changed.emit(self._collapsed)

    def _apply_state(self):
        if self._collapsed:
            self.setFixedWidth(self.COLLAPSED_WIDTH)
            self._content.hide()
            self._strip.show()
        else:
            self.setFixedWidth(self.EXPANDED_WIDTH)
            self._content.show()
            self._strip.hide()

    def _on_item_clicked(self, item, column):
        page_idx = item.data(0, Qt.ItemDataRole.UserRole)
        if page_idx is not None:
            self.page_navigate_requested.emit(page_idx)


# ── Background prerender worker ────────────────────────────────────────

class _PrerenderWorker(QThread):
    """Renders PDF pages to raw pixel data in a background thread.
    Emits batches of (page_idx, samples_bytes, width, height, stride).
    """

    batch_data_ready = Signal(list)

    def __init__(self, file_path: str, page_indices: list[int],
                 available_width: int, dpr: float, dpi: int):
        super().__init__()
        self._file_path = file_path
        self._page_indices = list(page_indices)
        self._available_width = available_width
        self._dpr = dpr
        self._dpi = dpi
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    def run(self):
        doc = fitz.open(self._file_path)
        try:
            batch = []
            for i, page_idx in enumerate(self._page_indices):
                if self._cancelled:
                    break
                page = doc[page_idx]
                pix = page.get_pixmap(dpi=self._dpi)
                batch.append((page_idx, bytes(pix.samples),
                              pix.width, pix.height, pix.stride))
                if len(batch) >= 2 or i == len(self._page_indices) - 1:
                    if not self._cancelled:
                        self.batch_data_ready.emit(batch)
                    batch = []
                    if i < len(self._page_indices) - 1 and not self._cancelled:
                        QThread.msleep(500)
        finally:
            doc.close()


# ── Graphics View with keyboard navigation ─────────────────────────────

class _PDFGraphicsView(QGraphicsView):
    def __init__(self, viewer, scene):
        super().__init__(scene)
        self._viewer = viewer

    def scrollContentsBy(self, dx, dy):
        super().scrollContentsBy(dx, dy)
        self._viewer._on_scrolled()

    def keyPressEvent(self, event):
        key = event.key()
        if key in (Qt.Key.Key_PageDown, Qt.Key.Key_Down):
            self._viewer._navigate_to_next_page()
        elif key in (Qt.Key.Key_PageUp, Qt.Key.Key_Up):
            self._viewer._navigate_to_prev_page()
        else:
            super().keyPressEvent(event)


# ── PDF Viewer ────────────────────────────────────────────────────────

class PDFViewer(QWidget):
    text_selected = Signal(int, int, str)  # lo, hi, cleaned_text
    auto_complete_changed = Signal(bool)
    context_menu_requested = Signal(str)
    selection_started = Signal()  # emitted on mouse press (new selection begins)
    dual_column_toggle_requested = Signal(bool)  # new desired state
    isolate_path_needed = Signal()  # emitted when _save_zones needs an isolate path
    toc_collapsed_changed = Signal(bool)
    note_path_needed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        # ── Toolbar ────────────────────────────────────────────────
        toolbar = QWidget()
        toolbar.setMaximumHeight(32)
        tb_layout = QHBoxLayout(toolbar)
        tb_layout.setContentsMargins(4, 2, 4, 2)
        self._column_btn = QPushButton("单栏模式")
        self._column_btn.setCheckable(True)
        self._column_btn.setFixedWidth(80)
        self._column_btn.clicked.connect(self._toggle_column_mode)
        tb_layout.addWidget(self._column_btn)

        self._auto_complete_btn = QPushButton("自动句补全 OFF")
        self._auto_complete_btn.setCheckable(True)
        self._auto_complete_btn.setToolTip("开启后自动将选中范围扩展至完整句子")
        self._auto_complete_btn.toggled.connect(self._on_auto_complete_toggled)
        tb_layout.addWidget(self._auto_complete_btn)

        self._frame_btn = QPushButton("框选隔离域")
        self._frame_btn.setCheckable(True)
        self._frame_btn.clicked.connect(self._toggle_frame_mode)
        tb_layout.addWidget(self._frame_btn)

        self._manage_btn = QPushButton("管理隔离域")
        self._manage_btn.setCheckable(True)
        self._manage_btn.clicked.connect(self._toggle_manage_mode)
        tb_layout.addWidget(self._manage_btn)

        self._note_btn = QPushButton("笔记")
        self._note_btn.setCheckable(True)
        self._note_btn.clicked.connect(self._toggle_note_mode)
        tb_layout.addWidget(self._note_btn)

        note_font_label = QLabel("笔记字号：")
        tb_layout.addWidget(note_font_label)
        self._note_font_spin = QSpinBox()
        self._note_font_spin.setRange(10, 24)
        self._note_font_spin.setValue(12)
        self._note_font_spin.setFixedWidth(48)
        self._note_font_spin.valueChanged.connect(
            self._on_note_font_size_changed)
        tb_layout.addWidget(self._note_font_spin)

        tb_layout.addStretch()
        layout.addWidget(toolbar)

        # ── TOC panel (placed externally by main_app) ──────────────
        self._toc_panel = TOCPanel()
        self._toc_panel.page_navigate_requested.connect(self._scroll_to_page_top)
        self._toc_panel.toc_collapsed_changed.connect(
            self.toc_collapsed_changed.emit)
        self._toc_panel.toc_collapsed_changed.connect(
            self._on_toc_resized)

        # ── View ──────────────────────────────────────────────────
        self._scene = _SelectionScene(self)
        self._view = _PDFGraphicsView(self, self._scene)
        self._view.setRenderHint(QPainter.RenderHint.Antialiasing)
        self._view.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self._view.setTransformationAnchor(
            QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self._view.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._view.setBackgroundBrush(QBrush(QColor(64, 64, 64)))
        layout.addWidget(self._view)

        # Alt+N shortcut for note mode
        self._note_shortcut = QShortcut(QKeySequence("Alt+N"), self._view)
        self._note_shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self._note_shortcut.activated.connect(self._on_note_shortcut)

        # ── State ──────────────────────────────────────────────────
        self._doc: fitz.Document | None = None
        self._words: list[_Word] = []
        self._page_offsets: list[float] = []       # scene Y of each page top-left
        self._page_width_pts: list[float] = []     # page width in PDF points
        self._page_height_pts: list[float] = []    # page height in PDF points
        self._scale_factor: float = 1.0             # PDF-pt → scene-px
        self._available_width: float = 800
        self._dual_column = False

        self._page_items: list[QGraphicsPixmapItem] = []
        self._highlight_items: list[QGraphicsRectItem] = []
        self._start_idx: int = -1
        self._end_idx: int = -1
        self._dragging = False
        self._words_outside: list[_Word] = []
        self._words_inside: list[_Word] = []
        self._active_words: list[_Word] | None = None

        # ── Isolation zone state ──────────────────────────────────
        self._zone_mode: int = MODE_READING
        self._zones: list[dict] = []          # [{page, x0, y0, x1, y1}, ...]
        self._zone_items: list[QGraphicsRectItem] = []
        self._selected_zone_idx: int = -1
        self._handle_items: list[QGraphicsRectItem] = []
        self._frame_start: QPointF | None = None
        self._frame_preview: QGraphicsRectItem | None = None
        self._handle_drag_idx: int = -1       # which handle is being dragged (-1 = move whole zone)
        self._zone_dragging: bool = False
        self._isolate_path: str | None = None

        # ── Lazy render state ───────────────────────────────────────
        self._rendered_pages: dict[int, bool] = {}
        self._prerender_worker: _PrerenderWorker | None = None
        self._dpr: float = 1.0

        # ── Note state ──────────────────────────────────────────────
        self._notes: list[dict] = []
        self._note_items: list[QGraphicsPixmapItem] = []
        self._note_popups: dict[int, QGraphicsProxyWidget] = {}
        self._note_save_timer: QTimer | None = None
        self._note_path: str | None = None
        self._note_font_size: int = DEFAULT_FONT_SIZE
        self._note_dragging_idx: int = -1
        self._note_icon_pixmap: QPixmap | None = None

        self._init_note_icon()

    # ── Size / layout ──────────────────────────────────────────────

    def _update_available_width(self):
        w = self._view.viewport().width() if self._view.viewport() else self.width()
        self._available_width = max(w, 400)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        new_w = self._view.viewport().width() if self._view.viewport() else self.width()
        new_w = max(new_w, 400)
        if self._doc:
            self._available_width = new_w
            if not hasattr(self, '_resize_timer'):
                self._resize_timer = QTimer(self)
                self._resize_timer.setSingleShot(True)
                self._resize_timer.timeout.connect(self._re_render_visuals)
            self._resize_timer.start(100)

    # ── Load / clear ───────────────────────────────────────────────

    def load_file(self, path: str):
        self._full_cleanup()
        self._update_available_width()
        self._doc = fitz.open(path)
        self._current_file = path
        self._render()
        self.load_zones()
        if self._zone_mode != MODE_READING:
            self._enter_mode(MODE_READING)

    def _full_cleanup(self):
        if self._doc is not None:
            self._doc.close()
            self._doc = None
        self._current_file = None
        self._clear_highlights()
        self._clear_zones()
        self._zones.clear()
        self._selected_zone_idx = -1
        self._clear_note_icons()
        self._notes.clear()
        self._note_path = None
        self._scene.clear()
        self._words.clear()
        self._page_offsets.clear()
        self._page_width_pts.clear()
        self._page_height_pts.clear()
        self._page_items.clear()
        self._start_idx = -1
        self._end_idx = -1
        self._dragging = False
        self._words_outside.clear()
        self._words_inside.clear()
        self._active_words = None
        self._rendered_pages.clear()
        self._toc_panel.load_toc(None)

    # ── Render ─────────────────────────────────────────────────────

    def _render(self):
        if self._doc is None:
            return
        self._re_render()
        self._load_toc()

    def _re_render(self):
        """Full render: pages + word extraction + zones."""
        self._do_render_pages()

    def _re_render_visuals(self):
        """Resize-only render: pages + zones. Skips word extraction (words are %-based)."""
        if self._doc is None or not self._words:
            return
        self._do_render_pages(extract_words=False)

    # ── TOC ──────────────────────────────────────────────────────

    def _load_toc(self):
        self._toc_panel.load_toc(self._doc)

    def set_toc_collapsed(self, collapsed: bool):
        self._toc_panel.set_collapsed(collapsed)

    def _on_toc_resized(self, collapsed: bool):
        if self._doc and self._words:
            QTimer.singleShot(50, self._re_render_visuals)

    @property
    def toc_panel(self) -> TOCPanel:
        return self._toc_panel

    # ── Page navigation ───────────────────────────────────────────

    def _get_current_page_at_center(self):
        viewport = self._view.viewport()
        if not viewport or not self._page_offsets:
            return None
        center = self._view.mapToScene(viewport.rect().center())
        for i in range(len(self._page_offsets)):
            next_off = (self._page_offsets[i + 1]
                        if i + 1 < len(self._page_offsets)
                        else float("inf"))
            if self._page_offsets[i] <= center.y() < next_off:
                return i
        return None

    def _scroll_to_page_top(self, page_idx):
        if page_idx < 0 or page_idx >= len(self._page_offsets):
            return
        viewport_h = self._view.viewport().height()
        page_top_y = self._page_offsets[page_idx]
        self._view.centerOn(self._available_width / 2,
                           page_top_y + viewport_h / 2)

    def _navigate_to_next_page(self):
        page_idx = self._get_current_page_at_center()
        if page_idx is not None and page_idx < len(self._page_offsets) - 1:
            self._scroll_to_page_top(page_idx + 1)

    def _navigate_to_prev_page(self):
        page_idx = self._get_current_page_at_center()
        if page_idx is not None and page_idx > 0:
            self._scroll_to_page_top(page_idx - 1)

    def _do_render_pages(self, extract_words=True):
        """Core: compute geometry for all pages, render visible ones as
        pixmaps, use gray placeholders for the rest, start prerender."""
        if self._doc is None:
            return
        self._cancel_prerender()

        self._scene.clear()
        self._page_offsets.clear()
        self._page_items.clear()
        self._rendered_pages.clear()
        self._clear_highlights()
        self._zone_items.clear()
        self._handle_items.clear()
        self._note_items.clear()
        self._note_popups.clear()

        if extract_words:
            self._words.clear()
            self._page_width_pts.clear()
            self._page_height_pts.clear()

        screen = QApplication.primaryScreen()
        self._dpr = screen.devicePixelRatio() if screen else 1.0

        offset_y = 0.0
        total_pages = len(self._doc)

        for page_idx in range(total_pages):
            page = self._doc[page_idx]
            pw = page.rect.width
            ph = page.rect.height
            if extract_words:
                self._page_width_pts.append(pw)
                self._page_height_pts.append(ph)

            sf = self._available_width / pw
            if page_idx == 0:
                self._scale_factor = sf

            scene_h = ph * sf
            self._page_offsets.append(offset_y)

            placeholder = QGraphicsRectItem(
                0, offset_y, self._available_width, scene_h)
            placeholder.setBrush(QBrush(QColor(80, 80, 80)))
            placeholder.setPen(QPen(Qt.PenStyle.NoPen))
            self._scene.addItem(placeholder)
            self._page_items.append(placeholder)
            self._rendered_pages[page_idx] = False

            offset_y += scene_h + PAGE_GAP

        if extract_words:
            self._extract_words()
        self._render_zones()

        total_h = max(offset_y, 400)
        self._scene.setSceneRect(0, 0, self._available_width, total_h)
        self._view.setSceneRect(0, 0, self._available_width, total_h)
        self._view.horizontalScrollBar().setRange(0, 0)

        # Render visible pages synchronously
        current_page = self._get_current_page_at_center() or 0
        for pi in range(max(0, current_page - 2),
                        min(total_pages, current_page + 3)):
            self._render_single_page_sync(pi)

        # Start background prerender for remaining pages
        self._start_prerender(current_page)

        # Re-render note icons (destroyed by scene.clear above)
        if self._notes:
            self._render_note_icons()

    # ── Lazy render helpers ────────────────────────────────────────

    def _render_single_page_sync(self, page_idx: int):
        """Render one page pixmap synchronously (main thread only)."""
        if self._rendered_pages.get(page_idx, False):
            return
        page = self._doc[page_idx]
        pix = page.get_pixmap(dpi=DPI)
        self._replace_with_pixmap(
            page_idx, bytes(pix.samples),
            pix.width, pix.height, pix.stride)

    def _replace_with_pixmap(self, page_idx: int, samples: bytes,
                             w: int, h: int, stride: int):
        """Build QPixmap from raw samples and replace the placeholder."""
        if page_idx >= len(self._page_items):
            return
        img = QImage(samples, w, h, stride, QImage.Format.Format_RGB888)
        pixmap = QPixmap.fromImage(img.copy())

        logical_w = int(self._available_width)
        logical_h = int(pixmap.height() * logical_w / pixmap.width())
        dpr = self._dpr
        physical_w = int(logical_w * dpr)
        physical_h = int(logical_h * dpr)
        scaled = pixmap.scaled(
            physical_w, physical_h,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        scaled.setDevicePixelRatio(dpr)

        old_item = self._page_items[page_idx]
        self._scene.removeItem(old_item)

        item = QGraphicsPixmapItem(scaled)
        item.setPos(0, self._page_offsets[page_idx])
        item.setFlag(QGraphicsPixmapItem.GraphicsItemFlag.ItemIsSelectable, False)
        item.setFlag(QGraphicsPixmapItem.GraphicsItemFlag.ItemIsMovable, False)
        self._scene.addItem(item)
        self._page_items[page_idx] = item
        self._rendered_pages[page_idx] = True

    def _build_prerender_order(self, current_page: int) -> list[int]:
        """Pages ordered by distance from current_page, skipping rendered."""
        total = len(self._page_offsets)
        order = []
        max_dist = max(current_page, total - current_page)
        for dist in range(1, max_dist + 1):
            for offset in [dist, -dist]:
                pi = current_page + offset
                if 0 <= pi < total and pi not in order:
                    order.append(pi)
        return [p for p in order if not self._rendered_pages.get(p, False)]

    def _start_prerender(self, current_page: int):
        page_order = self._build_prerender_order(current_page)
        if not page_order:
            return
        self._prerender_worker = _PrerenderWorker(
            self._current_file, page_order,
            self._available_width, self._dpr, DPI,
        )
        self._prerender_worker.batch_data_ready.connect(
            self._on_prerender_batch)
        self._prerender_worker.start()

    def _cancel_prerender(self):
        if self._prerender_worker is not None:
            if self._prerender_worker.isRunning():
                self._prerender_worker.cancel()
                self._prerender_worker.wait(1000)
            self._prerender_worker = None

    def _on_prerender_batch(self, batch: list):
        for page_idx, samples, w, h, stride in batch:
            if self._rendered_pages.get(page_idx, False):
                continue
            self._replace_with_pixmap(page_idx, samples, w, h, stride)

    # ── Scroll acceleration ────────────────────────────────────────

    def _on_scrolled(self):
        if not hasattr(self, '_scroll_debounce_timer'):
            self._scroll_debounce_timer = QTimer(self)
            self._scroll_debounce_timer.setSingleShot(True)
            self._scroll_debounce_timer.timeout.connect(
                self._check_scroll_boundary)
        self._scroll_debounce_timer.start(50)

    def _check_scroll_boundary(self):
        current_page = self._get_current_page_at_center()
        if current_page is None or not self._page_offsets:
            return
        total = len(self._page_offsets)
        for pi in range(max(0, current_page - 3),
                        min(total, current_page + 4)):
            if not self._rendered_pages.get(pi, False):
                self._render_single_page_sync(pi)
                return

    def _extract_words(self):
        """Re-extract word list from document. Respects _dual_column ordering."""
        self._words.clear()
        for page_idx in range(len(self._doc)):
            page = self._doc[page_idx]
            pw = self._page_width_pts[page_idx]
            ph = self._page_height_pts[page_idx]

            # Build span lookup: (x0, y0) -> (size, flags) from dict extraction
            span_info: dict[tuple[float, float], tuple[float, int]] = {}
            dict_data = page.get_text("dict")
            for block in dict_data.get("blocks", []):
                for line in block.get("lines", []):
                    for span in line.get("spans", []):
                        bbox = span["bbox"]
                        key = (round(bbox[0], 1), round(bbox[1], 1))
                        span_info[key] = (span.get("size", 0.0), span.get("flags", 0))

            words_data = page.get_text("words")
            page_words = []
            for w in words_data:
                x0, y0, x1, y1 = w[:4]
                text = w[4]
                if not text or not text.strip():
                    continue

                word_size = 0.0
                word_flags = 0
                if span_info:
                    best_dist = float("inf")
                    for (sx0, sy0), (size, flags) in span_info.items():
                        dist = (x0 - sx0) ** 2 + (y0 - sy0) ** 2
                        if dist < best_dist:
                            best_dist = dist
                            word_size = size
                            word_flags = flags

                word = _Word(
                    idx=0,
                    page_idx=page_idx,
                    x0_pct=x0 / pw,
                    y0_pct=y0 / ph,
                    x1_pct=x1 / pw,
                    y1_pct=y1 / ph,
                    text=text,
                    size=word_size,
                    flags=word_flags,
                )
                page_words.append(word)

            if self._dual_column:
                page_words = self._reorder_dual_column(page_words, pw)

            for w in page_words:
                w.idx = len(self._words)
                self._words.append(w)

        self._rebuild_word_lists()

    def _reorder_dual_column(self, page_words: list[_Word], page_width: float) -> list[_Word]:
        """Line-level adaptive dual-column reorder.

        Each line is independently judged:
        - If any word in the line straddles the midpoint (with 1% margin),
          the line is treated as single-column and kept in original order.
        - Otherwise the line is split into left/right. All left words are
          collected first, all right words second, at page end.
        """
        lines = self._group_words_into_lines(page_words)

        result: list[_Word] = []
        left_collected: list[_Word] = []
        right_collected: list[_Word] = []

        for line_words in lines:
            is_single = any(
                w.x0_pct < 0.49 and w.x1_pct > 0.51
                for w in line_words
            )
            if is_single:
                result.extend(line_words)
            else:
                line_left = [w for w in line_words if w.center_x < 0.5]
                line_right = [w for w in line_words if w.center_x >= 0.5]
                line_left.sort(key=lambda w: (w.y0_pct, w.x0_pct))
                line_right.sort(key=lambda w: (w.y0_pct, w.x0_pct))
                left_collected.extend(line_left)
                right_collected.extend(line_right)

        result.extend(left_collected)
        result.extend(right_collected)
        return result

    def _toggle_column_mode(self):
        self.dual_column_toggle_requested.emit(self._column_btn.isChecked())

    def set_dual_column(self, enabled: bool):
        self._apply_dual_column(enabled)

    def set_dual_column_silent(self, enabled: bool):
        self._column_btn.setChecked(enabled)
        self._apply_dual_column(enabled)

    def _apply_dual_column(self, enabled: bool):
        self._dual_column = enabled
        self._column_btn.setText("双栏模式" if self._dual_column else "单栏模式")
        self._clear_highlights()
        self._start_idx = -1
        self._end_idx = -1
        if self._doc is not None:
            self._extract_words()

    def _on_auto_complete_toggled(self, checked: bool):
        self._auto_complete_btn.setText("自动句补全 ON" if checked else "自动句补全 OFF")
        self.auto_complete_changed.emit(checked)

    # ── Isolation zones ────────────────────────────────────────────

    def set_isolate_path(self, path: str | None):
        self._isolate_path = path

    def load_zones(self):
        """Load isolation zones from per-PDF isolate file."""
        if not self._isolate_path:
            self._zones = []
            self._rebuild_word_lists()
            return
        try:
            with open(self._isolate_path, "r", encoding="utf-8") as f:
                self._zones = json.load(f)
        except (json.JSONDecodeError, OSError, FileNotFoundError):
            self._zones = []
        self._rebuild_word_lists()
        self._render_zones()

    def _rebuild_word_lists(self):
        """Rebuild _words_inside and _words_outside based on current _zones.
        A word is inside if its bbox is fully contained within any zone."""
        if not self._zones:
            self._words_outside = list(self._words)
            self._words_inside = []
            return
        inside: list[_Word] = []
        outside: list[_Word] = []
        for w in self._words:
            in_zone = False
            for z in self._zones:
                if (w.page_idx == z["page"]
                        and w.x0_pct >= z["x0"] and w.x1_pct <= z["x1"]
                        and w.y0_pct >= z["y0"] and w.y1_pct <= z["y1"]):
                    in_zone = True
                    break
            if in_zone:
                inside.append(w)
            else:
                outside.append(w)
        self._words_outside = outside
        self._words_inside = inside

    def _save_zones(self):
        """Persist zones to per-PDF isolate file."""
        if not self._isolate_path:
            self.isolate_path_needed.emit()
        if not self._isolate_path:
            return
        Path(self._isolate_path).parent.mkdir(parents=True, exist_ok=True)
        with open(self._isolate_path, "w", encoding="utf-8") as f:
            json.dump(self._zones, f, ensure_ascii=False, indent=2)

    def _render_zones(self):
        """Re-draw all zone overlay rectangles."""
        self._clear_zones()
        for zi, z in enumerate(self._zones):
            rect = self._zone_scene_rect(z)
            item = QGraphicsRectItem(rect)
            item.setZValue(50)
            if self._zone_mode == MODE_MANAGING:
                color = ZONE_MANAGING_COLOR
                item.setAcceptedMouseButtons(Qt.MouseButton.LeftButton)
                item.setCursor(Qt.CursorShape.PointingHandCursor)
            else:
                color = ZONE_READING_COLOR
                item.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
            item.setBrush(QBrush(color))
            item.setPen(QPen(Qt.PenStyle.NoPen))
            item.setData(0, zi)  # store zone index
            self._scene.addItem(item)
            self._zone_items.append(item)

        if self._zone_mode == MODE_MANAGING and self._selected_zone_idx >= 0:
            self._draw_handles()

    def _clear_zones(self):
        """Remove all zone overlay rects and handles from scene."""
        self._clear_handles()
        for item in self._zone_items:
            self._scene.removeItem(item)
        self._zone_items.clear()

    def _zone_scene_rect(self, zone: dict) -> QRectF:
        """Convert zone percentage coords → scene rectangle."""
        pi = zone["page"]
        off_y = self._page_offsets[pi]
        ph = self._page_height_pts[pi]
        sf = self._scale_factor
        return QRectF(
            zone["x0"] * self._available_width,
            off_y + zone["y0"] * ph * sf,
            (zone["x1"] - zone["x0"]) * self._available_width,
            (zone["y1"] - zone["y0"]) * ph * sf,
        )

    def _scene_rect_to_zone(self, pi: int, rect: QRectF) -> dict:
        """Convert scene rectangle → zone percentage coords."""
        off_y = self._page_offsets[pi]
        ph = self._page_height_pts[pi]
        sf = self._scale_factor
        x0 = max(0.0, min(1.0, rect.x() / self._available_width))
        y0_pct = (rect.y() - off_y) / (ph * sf)
        y0 = max(0.0, min(1.0, y0_pct))
        x1 = max(0.0, min(1.0, rect.right() / self._available_width))
        y1_pct = (rect.bottom() - off_y) / (ph * sf)
        y1 = max(0.0, min(1.0, y1_pct))
        return {"page": pi, "x0": x0, "y0": y0, "x1": x1, "y1": y1}

    # ── Mode switching ────────────────────────────────────────────

    def _toggle_frame_mode(self):
        if self._zone_mode == MODE_FRAMING:
            self._enter_mode(MODE_READING)
        else:
            self._enter_mode(MODE_FRAMING)

    def _toggle_manage_mode(self):
        if self._zone_mode == MODE_MANAGING:
            self._enter_mode(MODE_READING)
        else:
            self._enter_mode(MODE_MANAGING)

    def _enter_mode(self, mode: int):
        """Enter a zone mode, updating cursors and overlays."""
        prev = self._zone_mode
        self._zone_mode = mode

        # Update button states
        self._frame_btn.setChecked(mode == MODE_FRAMING)
        self._manage_btn.setChecked(mode == MODE_MANAGING)
        self._note_btn.setChecked(mode == MODE_NOTING)

        # Clear selection state
        self._selected_zone_idx = -1
        self._clear_handles()
        self._frame_start = None
        self._note_dragging_idx = -1
        if self._frame_preview:
            self._scene.removeItem(self._frame_preview)
            self._frame_preview = None

        # Set cursor
        if mode == MODE_NOTING:
            pm = self._note_icon_pixmap
            # Use QIcon intermediate for reliable cross-platform custom cursor
            icon = QIcon(pm)
            cp = icon.pixmap(24, 24)
            cursor = QCursor(cp, 0, 0)
            self._view.viewport().setCursor(cursor)
            self._view.setCursor(cursor)
        elif mode == MODE_FRAMING:
            self._view.viewport().setCursor(Qt.CursorShape.CrossCursor)
        else:
            self._view.viewport().setCursor(Qt.CursorShape.ArrowCursor)
            self._view.unsetCursor()

        # Set drag mode
        if mode in (MODE_MANAGING, MODE_NOTING):
            self._view.setDragMode(QGraphicsView.DragMode.NoDrag)
        else:
            self._view.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)

        # Re-render zones
        self._render_zones()

    # ── Handles (managing mode) ──────────────────────────────────

    def _draw_handles(self):
        self._clear_handles()
        if self._selected_zone_idx < 0 or self._selected_zone_idx >= len(self._zones):
            return
        z = self._zones[self._selected_zone_idx]
        r = self._zone_scene_rect(z)
        pts = [
            r.topLeft(),      # 0=TL
            r.topRight(),     # 1=TR
            r.bottomRight(),  # 2=BR
            r.bottomLeft(),   # 3=BL
        ]
        for hi, pt in enumerate(pts):
            hr = QRectF(
                pt.x() - HANDLE_SIZE / 2, pt.y() - HANDLE_SIZE / 2,
                HANDLE_SIZE, HANDLE_SIZE,
            )
            h_item = QGraphicsRectItem(hr)
            h_item.setPen(QPen(QColor(60, 60, 60), 1))
            h_item.setBrush(QBrush(QColor(220, 220, 220)))
            h_item.setZValue(60)
            h_item.setData(0, hi)
            h_item.setAcceptedMouseButtons(Qt.MouseButton.LeftButton)
            h_item.setCursor(Qt.CursorShape.SizeAllCursor)
            self._scene.addItem(h_item)
            self._handle_items.append(h_item)

    def _clear_handles(self):
        for item in self._handle_items:
            self._scene.removeItem(item)
        self._handle_items.clear()

    def _zone_at_scene_pos(self, pos: QPointF) -> int:
        """Return index of the zone under pos, or -1."""
        for zi, z in enumerate(self._zones):
            r = self._zone_scene_rect(z)
            if r.contains(pos):
                return zi
        return -1

    # ── Public API for reader_tab ──────────────────────────────────

    @property
    def words(self) -> list[_Word]:
        if self._active_words is not None:
            return self._active_words
        return self._words

    def get_word_texts(self) -> list[str]:
        src = self._active_words if self._active_words is not None else self._words
        return [w.text for w in src]

    def get_text_by_range(self, lo: int, hi: int) -> str:
        words = self._get_selected_words(lo, hi)
        if not words:
            return ""
        return " ".join(w.text for w in words)

    def set_highlight_range(self, lo: int, hi: int) -> None:
        self._clear_highlights()
        self._start_idx = lo
        self._end_idx = hi
        self._draw_highlights()

    def set_auto_complete_enabled(self, enabled: bool) -> None:
        self._auto_complete_btn.setChecked(enabled)
        self._auto_complete_btn.setText("自动句补全 ON" if enabled else "自动句补全 OFF")

    def is_auto_complete_enabled(self) -> bool:
        return self._auto_complete_btn.isChecked()

    @property
    def is_dual_column(self) -> bool:
        return self._dual_column

    # ── Coordinate mapping ─────────────────────────────────────────

    def _scene_to_pdf(self, pos: QPointF) -> tuple[int, float, float] | None:
        """Convert scene position → (page_idx, x_pct, y_pct). Returns None if outside all pages."""
        if not self._page_offsets:
            return None

        # Find page by Y
        page_idx = 0
        for i in range(len(self._page_offsets)):
            next_off = (self._page_offsets[i + 1]
                        if i + 1 < len(self._page_offsets)
                        else float("inf"))
            if pos.y() < next_off:
                page_idx = i
                break

        page_top = self._page_offsets[page_idx]
        rel_x = pos.x()
        rel_y = pos.y() - page_top

        x_pct = rel_x / self._available_width
        y_pct = rel_y / (self._page_height_pts[page_idx] * self._scale_factor)

        return page_idx, x_pct, y_pct

    # ── Line detection ────────────────────────────────────────────

    @staticmethod
    def _line_centers(words: list[_Word]) -> list[float]:
        """Return the unique Y-center of each line, sorted top→bottom.
        Clusters word center_y values within LINE_TOLERANCE percentage.
        """
        if not words:
            return []
        ys = sorted(set(round(w.center_y, 4) for w in words))
        if len(ys) <= 1:
            return ys
        clusters = [[ys[0]]]
        for y in ys[1:]:
            if y - clusters[-1][-1] <= PDFViewer.LINE_TOLERANCE:
                clusters[-1].append(y)
            else:
                clusters.append([y])
        return [sum(c) / len(c) for c in clusters]

    @staticmethod
    def _group_words_into_lines(words: list[_Word]) -> list[list[_Word]]:
        """Group words into lines by Y proximity. Each group is one line,
        sorted top-to-bottom; words within a line keep their original order.
        """
        if not words:
            return []
        sorted_words = sorted(words, key=lambda w: (w.y0_pct, w.x0_pct))
        lines: list[list[_Word]] = [[sorted_words[0]]]
        for w in sorted_words[1:]:
            last_center = sum(w2.center_y for w2 in lines[-1]) / len(lines[-1])
            if abs(w.center_y - last_center) <= PDFViewer.LINE_TOLERANCE:
                lines[-1].append(w)
            else:
                lines.append([w])
        return lines

    LINE_TOLERANCE = 0.005  # percentage — words within 0.5% page height belong to same line
    BUFFER = 20             # words to expand on each side for spatial retrieval
    SPATIAL_TOLERANCE = 0.0005  # 0.5% page height vertical tolerance

    def _split_line_by_physical_column(self, line_words: list[_Word]) -> list[list[_Word]]:
        """In dual-column mode, split a line's words into left/right groups by
        physical x0_pct. Single-column mode returns the whole line as one group."""
        if not self._dual_column or not line_words:
            return [line_words] if line_words else []
        has_straddle = any(
            w.x0_pct < 0.49 and w.x1_pct > 0.51 for w in line_words
        )
        if has_straddle:
            return [line_words]
        left = [w for w in line_words if w.x0_pct < 0.5]
        right = [w for w in line_words if w.x0_pct >= 0.5]
        groups: list[list[_Word]] = []
        if left:
            groups.append(left)
        if right:
            groups.append(right)
        return groups if groups else [line_words]

    def _word_at_scene_pos(self, pos: QPointF) -> int | None:
        """Return word index nearest to scene position.
        Uses _active_words (set by _snap_start) as the candidate pool
        so isolation zones are enforced by word-list selection, not runtime filtering.
        """
        if not self._active_words:
            return None

        mapped = self._scene_to_pdf(pos)
        if mapped is None:
            return None
        page_idx, x_pct, y_pct = mapped

        src = self._active_words
        candidates = [w for w in src if w.page_idx == page_idx]
        if not candidates:
            candidates = src

        # Dual-column: filter by X column first, no fallback
        if self._dual_column:
            if x_pct < 0.5:
                candidates = [w for w in candidates if w.center_x < 0.5]
            else:
                candidates = [w for w in candidates if w.center_x >= 0.5]
            if not candidates:
                return None

        if not candidates:
            return None

        # Step 1 — find reference word nearest to mouse Y
        def _y_dist(w, y):
            if w.y0_pct <= y <= w.y1_pct:
                return 0.0
            return min(abs(w.y0_pct - y), abs(w.y1_pct - y))

        ref = min(candidates, key=lambda w: _y_dist(w, y_pct))

        # Step 2 — collect same-line words by Y-interval overlap
        line_words = []
        for w in candidates:
            overlap = min(w.y1_pct, ref.y1_pct) - max(w.y0_pct, ref.y0_pct)
            if overlap > 0:
                hw = w.y1_pct - w.y0_pct
                hr = ref.y1_pct - ref.y0_pct
                if overlap >= 0.5 * min(hw, hr):
                    line_words.append(w)
        if not line_words:
            line_words = [ref]

        # Step 3 — nearest word in X within that line
        best = min(line_words, key=lambda w: abs(w.center_x - x_pct))
        return src.index(best)

    # ── Selection ──────────────────────────────────────────────────

    def _snap_start(self, pos: QPointF):
        # Determine active word list before word lookup
        if self._zone_mode == MODE_READING and self._zones:
            mapped = self._scene_to_pdf(pos)
            if mapped:
                page_idx, x_pct, y_pct = mapped
                in_zone = any(
                    z["page"] == page_idx
                    and z["x0"] <= x_pct <= z["x1"]
                    and z["y0"] <= y_pct <= z["y1"]
                    for z in self._zones
                )
                self._active_words = self._words_inside if in_zone else self._words_outside
            else:
                self._active_words = self._words_outside
        else:
            self._active_words = self._words
        self._start_idx = self._word_at_scene_pos(pos)
        if self._start_idx is None:
            self._start_idx = -1

    def _snap_end(self, pos: QPointF):
        idx = self._word_at_scene_pos(pos)
        if idx is not None:
            self._end_idx = idx
        src = self._active_words if self._active_words is not None else self._words

    def _selected_word_range(self):
        if (self._start_idx is None or self._start_idx < 0 or
                self._end_idx is None or self._end_idx < 0):
            return None, None
        lo = min(self._start_idx, self._end_idx)
        hi = max(self._start_idx, self._end_idx)
        return lo, hi

    def get_selection_range(self):
        """Public accessor for the current word-level selection range.
        Returns (lo, hi) or (None, None)."""
        return self._selected_word_range()

    def _get_selected_words(self, lo: int | None = None,
                            hi: int | None = None) -> list[_Word]:
        """Buffer-expanded spatial retrieval with logical coordinates.
        anchor_start = press point (src[lo]), anchor_end = drag point (src[hi]).
        Expands buffer by BUFFER around the raw range, determines first_anchor
        and last_anchor by logical coordinate order, then applies anchor walls
        and spatial filter before geometric reorder.
        """
        if lo is None or hi is None:
            lo = self._start_idx
            hi = self._end_idx
        if lo is None or lo < 0 or hi is None or hi < 0:
            return []
        src = self._active_words if self._active_words is not None else self._words

        # ── Logical coordinate helpers ─────────────────────────────
        dual = self.is_dual_column

        def _lx(w: _Word) -> float:
            """Logical center X: right column shifted left by 0.5."""
            return w.center_x - 0.5 if (dual and w.center_x >= 0.5) else w.center_x

        def _ly(w: _Word) -> float:
            """Logical Y: page * 2.0 + y0_pct, right column +1.0."""
            base = w.y0_pct + w.page_idx * 2.0
            return base + 1.0 if (dual and w.center_x >= 0.5) else base

        def _lx0(w: _Word) -> float:
            return w.x0_pct - 0.5 if (dual and w.center_x >= 0.5) else w.x0_pct

        def _lx1(w: _Word) -> float:
            """Logical right X: right column shifted left by 0.5."""
            return w.x1_pct - 0.5 if (dual and w.center_x >= 0.5) else w.x1_pct

        def _ly1(w: _Word) -> float:
            """Logical Y bottom: page * 2.0 + y1_pct, right column +1.0."""
            base = w.y1_pct + w.page_idx * 2.0
            return base + 1.0 if (dual and w.center_x >= 0.5) else base

        def _same_line(a: _Word, b: _Word) -> bool:
            """True if logical-Y intervals overlap > 50% of the smaller height."""
            ya0, ya1 = _ly(a), _ly1(a)
            yb0, yb1 = _ly(b), _ly1(b)
            overlap = min(ya1, yb1) - max(ya0, yb0)
            if overlap <= 0:
                return False
            ha = ya1 - ya0
            hb = yb1 - yb0
            return overlap >= 0.5 * min(ha, hb)

        # ── 0. Anchors: press point vs drag point ──────────────────
        anchor_start = src[lo]
        anchor_end = src[hi]

        # Determine first/last: same-line by X, otherwise by Y-center
        def _lcy(w: _Word) -> float:
            return (_ly(w) + _ly1(w)) / 2

        if _same_line(anchor_start, anchor_end):
            if _lx(anchor_start) <= _lx(anchor_end):
                first_anchor, last_anchor = anchor_start, anchor_end
            else:
                first_anchor, last_anchor = anchor_end, anchor_start
        else:
            if _lcy(anchor_start) <= _lcy(anchor_end):
                first_anchor, last_anchor = anchor_start, anchor_end
            else:
                first_anchor, last_anchor = anchor_end, anchor_start

        # ── 1. Buffer expansion ────────────────────────────────────
        buf_lo = max(0, min(lo, hi) - self.BUFFER)
        buf_hi = min(len(src), max(lo, hi) + self.BUFFER + 1)
        candidate_words: list[_Word] = list(src[buf_lo:buf_hi])

        # ── 2. Core bounding box (from two anchors) ────────────────
        y_min = min(_ly(anchor_start), _ly(anchor_end))
        y_max = max(_ly(anchor_start), _ly(anchor_end))

        # ── 3. Anchor walls + spatial filter (logical coords) ──────
        fa_lx = _lx0(first_anchor)
        fa_lcy = (_ly(first_anchor) + _ly1(first_anchor)) / 2
        la_lx = _lx1(last_anchor)
        la_lcy = (_ly(last_anchor) + _ly1(last_anchor)) / 2

        tol = self.SPATIAL_TOLERANCE
        retrieved: list[_Word] = []
        for w in candidate_words:
            # Anchor walls: logical center Y hard discard, then X if same line
            lcy = (_ly(w) + _ly1(w)) / 2
            lx_end = _lx(w) if w is not last_anchor else la_lx + 1.0
            lx_start = _lx0(w)

            # First anchor wall: discard if clearly above, or same-line left
            if lcy < fa_lcy - self.LINE_TOLERANCE:
                continue
            if _same_line(w, first_anchor) and lx_end < fa_lx:
                continue

            # Last anchor wall: discard if clearly below, or same-line right
            if lcy > la_lcy + self.LINE_TOLERANCE:
                continue
            if _same_line(w, last_anchor) and lx_start > la_lx:
                continue

            # Spatial filter: soft Y boundaries from two-anchor box
            wly = _ly(w)
            if wly < y_min - tol or wly > y_max + tol:
                continue
            retrieved.append(w)

        # ── 4. Geometric reorder ───────────────────────────────────
        lines = self._group_words_into_lines(retrieved)
        result: list[_Word] = []
        for line_words in lines:
            line_words.sort(key=lambda w: w.x0_pct)
            result.extend(line_words)
        return result

    def _selected_text(self) -> str:
        words = self._get_selected_words()
        if not words:
            return ""
        return " ".join(w.text for w in words)

    def _word_scene_rect(self, w: _Word) -> QRectF:
        """Convert word percentage bbox → scene rectangle."""
        off_y = self._page_offsets[w.page_idx]
        page_h = self._page_height_pts[w.page_idx]
        sf = self._scale_factor
        return QRectF(
            w.x0_pct * self._available_width,
            off_y + w.y0_pct * page_h * sf,
            (w.x1_pct - w.x0_pct) * self._available_width,
            (w.y1_pct - w.y0_pct) * page_h * sf,
        )

    # ── Highlights ─────────────────────────────────────────────────

    def _clear_highlights(self):
        for item in self._highlight_items:
            if shiboken6.isValid(item):
                self._scene.removeItem(item)
        self._highlight_items.clear()

    def _draw_highlights(self):
        self._clear_highlights()
        selected = self._get_selected_words()
        if not selected:
            return

        # Group selected words by page so lines on different pages
        # with similar center_y are never conflated.
        by_page: dict[int, list[_Word]] = {}
        for w in selected:
            by_page.setdefault(w.page_idx, []).append(w)

        pen = QPen(HIGHLIGHT_BORDER, 1.0)
        brush = QBrush(HIGHLIGHT_COLOR)
        sf = self._scale_factor

        for page_idx, page_words in by_page.items():
            off_y = self._page_offsets[page_idx]
            page_h = self._page_height_pts[page_idx]
            centers = self._line_centers(page_words)

            for yc in centers:
                line_words = [
                    w for w in page_words
                    if abs(w.center_y - yc) <= self.LINE_TOLERANCE
                ]
                if not line_words:
                    continue

                groups = self._split_line_by_physical_column(line_words)

                for group in groups:
                    sorted_x = sorted(group, key=lambda w: w.x0_pct)
                    x0_pct = sorted_x[0].x0_pct
                    x1_pct = sorted_x[-1].x1_pct
                    y0_pct = min(w.y0_pct for w in group)
                    y1_pct = max(w.y1_pct for w in group)

                    rect = QRectF(
                        x0_pct * self._available_width,
                        off_y + y0_pct * page_h * sf,
                        (x1_pct - x0_pct) * self._available_width,
                        (y1_pct - y0_pct) * page_h * sf,
                    )
                    item = QGraphicsRectItem(rect)
                    item.setPen(pen)
                    item.setBrush(brush)
                    item.setZValue(100)
                    self._scene.addItem(item)
                    self._highlight_items.append(item)

    # ── Framing mode mouse handlers ───────────────────────────────

    def _on_frame_press(self, pos: QPointF):
        pi, _, _ = self._scene_to_pdf(pos) or (0, 0, 0)
        self._frame_start = pos
        self._frame_page = pi

    def _on_frame_move(self, pos: QPointF):
        if self._frame_preview:
            self._scene.removeItem(self._frame_preview)
        x = min(self._frame_start.x(), pos.x())
        y = min(self._frame_start.y(), pos.y())
        w = abs(pos.x() - self._frame_start.x())
        h = abs(pos.y() - self._frame_start.y())
        self._frame_preview = QGraphicsRectItem(QRectF(x, y, w, h))
        self._frame_preview.setPen(QPen(QColor(255, 0, 0), 1.5))
        self._frame_preview.setBrush(QBrush(ZONE_PREVIEW_COLOR))
        self._frame_preview.setZValue(55)
        self._scene.addItem(self._frame_preview)

    def _on_frame_release(self, pos: QPointF):
        if self._frame_preview:
            self._scene.removeItem(self._frame_preview)
            self._frame_preview = None
        if not self._frame_start:
            self._frame_start = None
            return
        x = min(self._frame_start.x(), pos.x())
        y = min(self._frame_start.y(), pos.y())
        w = abs(pos.x() - self._frame_start.x())
        h = abs(pos.y() - self._frame_start.y())
        self._frame_start = None
        if w < 10 and h < 10:  # too small, ignore
            return
        zone = self._scene_rect_to_zone(self._frame_page, QRectF(x, y, w, h))
        self._zones.append(zone)
        self._rebuild_word_lists()
        self._save_zones()
        self._render_zones()
        self._enter_mode(MODE_READING)

    # ── Managing mode mouse handlers ──────────────────────────────

    def _on_manage_press(self, pos: QPointF):
        # Check if clicking a handle
        for hi, h_item in enumerate(self._handle_items):
            if h_item.contains(pos):
                self._handle_drag_idx = hi
                self._drag_orig_zone = dict(self._zones[self._selected_zone_idx])
                self._drag_orig_pos = pos
                self._zone_dragging = True
                return
        # Check if clicking a zone
        zi = self._zone_at_scene_pos(pos)
        self._selected_zone_idx = zi
        self._render_zones()
        if zi >= 0:
            self._drag_orig_zone = dict(self._zones[zi])
            self._drag_orig_pos = pos
            self._handle_drag_idx = -1  # moving whole zone
            self._zone_dragging = True

    def _on_manage_move(self, pos: QPointF):
        if self._selected_zone_idx < 0:
            return
        z = self._zones[self._selected_zone_idx]
        orig = self._drag_orig_zone
        dx = pos.x() - self._drag_orig_pos.x()
        dy = pos.y() - self._drag_orig_pos.y()
        pi = z["page"]
        off_y = self._page_offsets[pi]
        ph = self._page_height_pts[pi]
        sf = self._scale_factor
        dx_pct = dx / self._available_width
        dy_pct = dy / (ph * sf)

        if self._handle_drag_idx == -1:
            # Move whole zone
            z["x0"] = max(0.0, min(1.0, orig["x0"] + dx_pct))
            z["y0"] = max(0.0, min(1.0, orig["y0"] + dy_pct))
            z["x1"] = max(0.0, min(1.0, orig["x1"] + dx_pct))
            z["y1"] = max(0.0, min(1.0, orig["y1"] + dy_pct))
        else:
            # Resize by handle: 0=TL,1=TR,2=BR,3=BL
            hi = self._handle_drag_idx
            if hi == 0:  # TL → x0, y0
                z["x0"] = max(0.0, min(z["x1"] - 0.01, orig["x0"] + dx_pct))
                z["y0"] = max(0.0, min(z["y1"] - 0.01, orig["y0"] + dy_pct))
            elif hi == 1:  # TR → x1, y0
                z["x1"] = max(z["x0"] + 0.01, min(1.0, orig["x1"] + dx_pct))
                z["y0"] = max(0.0, min(z["y1"] - 0.01, orig["y0"] + dy_pct))
            elif hi == 2:  # BR → x1, y1
                z["x1"] = max(z["x0"] + 0.01, min(1.0, orig["x1"] + dx_pct))
                z["y1"] = max(z["y0"] + 0.01, min(1.0, orig["y1"] + dy_pct))
            elif hi == 3:  # BL → x0, y1
                z["x0"] = max(0.0, min(z["x1"] - 0.01, orig["x0"] + dx_pct))
                z["y1"] = max(z["y0"] + 0.01, min(1.0, orig["y1"] + dy_pct))
        self._render_zones()

    def _on_manage_release(self, pos: QPointF):
        if self._selected_zone_idx >= 0:
            self._rebuild_word_lists()
            self._save_zones()
        self._handle_drag_idx = -1
        self._zone_dragging = False

    def _on_manage_right_click(self, pos: QPointF):
        zi = self._zone_at_scene_pos(pos)
        if zi < 0:
            return
        self._selected_zone_idx = zi
        self._render_zones()
        menu = QMenu()
        action = menu.addAction("删除")
        action.triggered.connect(lambda: self._delete_zone(zi))
        menu.exec(QCursor.pos())

    def _delete_zone(self, zi: int):
        if 0 <= zi < len(self._zones):
            self._zones.pop(zi)
        self._selected_zone_idx = -1
        self._rebuild_word_lists()
        self._save_zones()
        self._render_zones()

    # ── Mouse events (via _SelectionScene) ─────────────────────────

    def _on_scene_press(self, pos: QPointF):
        self._dragging = True
        self._clear_highlights()
        self._view.viewport().update()
        self._snap_start(pos)
        self._end_idx = self._start_idx
        self._draw_highlights()
        self.selection_started.emit()

    def _on_scene_move(self, pos: QPointF):
        if not self._dragging:
            return
        self._snap_end(pos)
        self._draw_highlights()

    def _on_scene_release(self, pos: QPointF):
        if not self._dragging:
            return
        self._dragging = False
        self._snap_end(pos)
        self._draw_highlights()

        raw_lo, raw_hi = self._selected_word_range()
        if raw_lo is None:
            self._clear_highlights()
            self._start_idx = -1
            self._end_idx = -1
            return

        words = self._get_selected_words()
        text = " ".join(w.text for w in words) if words else ""
        cleaned = clean_newlines(text)
        if cleaned:
            QApplication.clipboard().setText(cleaned)
            src = self._active_words if self._active_words is not None else self._words
            pos_of = {id(w): i for i, w in enumerate(src)}
            lo = min(pos_of[id(w)] for w in words)
            hi = max(pos_of[id(w)] for w in words)
            self.text_selected.emit(lo, hi, cleaned)

    def _on_scene_context_menu(self, pos: QPointF):
        text = self._selected_text()
        cleaned = clean_newlines(text)
        if cleaned:
            self.context_menu_requested.emit(cleaned)

    # ── Note mode ──────────────────────────────────────────────────

    def _init_note_icon(self):
        """Load note icon pixmap from project root, or create a fallback."""
        base = os.path.dirname(os.path.abspath(sys.argv[0]))
        for name in ("note.png", "note.ico"):
            p = Path(base) / name
            if p.exists():
                pm = QPixmap(str(p))
                if not pm.isNull():
                    self._note_icon_pixmap = pm.scaled(
                        24, 24,
                        Qt.AspectRatioMode.KeepAspectRatio,
                        Qt.TransformationMode.SmoothTransformation,
                    )
                    return
        # Fallback: small colored square
        px = QPixmap(24, 24)
        px.fill(QColor(220, 180, 60))
        self._note_icon_pixmap = px

    def _build_note_icon_item(self) -> QGraphicsPixmapItem:
        """Create a new note icon QGraphicsPixmapItem (24×24)."""
        item = QGraphicsPixmapItem(self._note_icon_pixmap)
        item.setZValue(70)
        item.setCursor(Qt.CursorShape.PointingHandCursor)
        item.setAcceptedMouseButtons(
            Qt.MouseButton.LeftButton | Qt.MouseButton.RightButton)
        return item

    def _note_scene_pos(self, note: dict) -> QPointF:
        """Convert note percentage coords → scene position (top-left of icon)."""
        off_y = self._page_offsets[note["page"]]
        ph = self._page_height_pts[note["page"]]
        sf = self._scale_factor
        x = note["x_pct"] * self._available_width
        y = off_y + note["y_pct"] * ph * sf
        return QPointF(x, y)

    def _note_at_scene_pos(self, pos: QPointF) -> int:
        """Return index of note icon under pos (within 24×24 hit area), or -1."""
        for ni, item in enumerate(self._note_items):
            if not shiboken6.isValid(item):
                continue
            r = QRectF(item.pos(), item.pixmap().size())
            r.adjust(-8, -8, 8, 8)  # 各方向扩大8px
            if r.contains(pos):
                return ni
        return -1

    def _render_note_icons(self):
        """Render all note icons on the scene."""
        self._clear_note_icons()
        for note in self._notes:
            item = self._build_note_icon_item()
            item.setPos(self._note_scene_pos(note))
            self._scene.addItem(item)
            self._note_items.append(item)

    def _clear_note_icons(self):
        """Remove all note icons and popups from scene."""
        for popup in self._note_popups.values():
            if shiboken6.isValid(popup):
                self._scene.removeItem(popup)
        self._note_popups.clear()
        for item in self._note_items:
            if shiboken6.isValid(item):
                self._scene.removeItem(item)
        self._note_items.clear()

    # ── Note mode toggle ──────────────────────────────────────────

    def _toggle_note_mode(self):
        if self._zone_mode == MODE_NOTING:
            self._enter_mode(MODE_READING)
        else:
            self._enter_mode(MODE_NOTING)

    def _on_note_shortcut(self):
        """Alt+N: enter note mode if no popup is open and view has focus."""
        if not self._view.hasFocus():
            return
        if self._note_popups:
            return
        if self._zone_mode != MODE_NOTING:
            self._enter_mode(MODE_NOTING)

    # ── Note click in reading mode ─────────────────────────────────

    def _on_note_icon_click_reading(self, note_idx: int):
        """Reading mode: click note icon toggles popup."""
        if note_idx in self._note_popups:
            self._close_note_popup(note_idx)
        else:
            self._open_note_popup(note_idx)

    def _open_note_popup(self, note_idx: int):
        note = self._notes[note_idx]
        icon_pos = self._note_scene_pos(note)
        popup_x = icon_pos.x() + 28
        popup_y = icon_pos.y()

        w = note.get("width", DEFAULT_NOTE_WIDTH)
        h = note.get("height", DEFAULT_NOTE_HEIGHT)

        editor = _NoteEditor()
        editor.setAcceptRichText(False)
        editor.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        editor.setPlainText(note.get("text", ""))
        editor.setStyleSheet(
            "QTextEdit {"
            " background-color: #3a3a3a;"
            " color: #eee;"
            " border: 1px solid #666;"
            " border-radius: 4px;"
            " padding: 4px;"
            "}"
        )
        font = editor.font()
        font.setPointSize(self._note_font_size)
        editor.setFont(font)
        editor.textChanged.connect(
            lambda: self._on_note_text_changed(note_idx))

        proxy = _NoteProxy(note_idx)
        proxy.setWidget(editor)
        proxy.setZValue(200)
        proxy.setPos(popup_x, popup_y)
        proxy.resize(w, h)
        proxy.setFlag(
            QGraphicsProxyWidget.GraphicsItemFlag.ItemIsFocusable, True)
        proxy.setAcceptHoverEvents(True)
        proxy.setAcceptedMouseButtons(
            Qt.MouseButton.LeftButton | Qt.MouseButton.RightButton)
        proxy.resize_requested.connect(
            lambda ni, ow, oh, dx, dy: self._on_note_resize(ni, ow, oh, dx, dy))

        self._scene.addItem(proxy)
        self._note_popups[note_idx] = proxy
        editor.setFocus()

    def _close_note_popup(self, note_idx: int):
        proxy = self._note_popups.pop(note_idx, None)
        if proxy:
            self._scene.removeItem(proxy)

    def _on_note_text_changed(self, note_idx: int):
        """User typed in popup — update note text, reset debounce timer."""
        proxy = self._note_popups.get(note_idx)
        if proxy is None:
            return
        editor = proxy.widget()
        if isinstance(editor, _NoteEditor):
            self._notes[note_idx]["text"] = editor.toPlainText()
        self._reset_note_save_timer()

    def _on_note_resize(self, note_idx: int, orig_w: float, orig_h: float,
                         dx: int, dy: int):
        """Edge-drag resize: grow proxy + editor, persist."""
        proxy = self._note_popups.get(note_idx)
        if proxy is None:
            return
        nw = max(100, int(orig_w + dx))
        nh = max(60, int(orig_h + dy))
        # 右边界限制
        scene_x = proxy.scenePos().x()
        max_width = self._available_width - scene_x
        nw = min(nw, max_width)
        # 下边界限制（仅最后一页）
        note_page = self._notes[note_idx]["page"]
        if note_page == len(self._page_offsets) - 1:
            scene_y = proxy.scenePos().y()
            last_page_bottom = self._page_offsets[-1] + self._page_height_pts[-1] * self._scale_factor
            max_height = last_page_bottom - scene_y
            nh = min(nh, max_height)
        proxy.resize(nw, nh)
        proxy.widget().resize(nw, nh)
        self._notes[note_idx]["width"] = nw
        self._notes[note_idx]["height"] = nh
        self._reset_note_save_timer()

    # ── Note mode mouse handlers ───────────────────────────────────

    def _on_note_press(self, pos: QPointF):
        """Note mode: click empty space → create note; click icon → start drag."""
        ni = self._note_at_scene_pos(pos)
        if ni >= 0:
            self._note_dragging_idx = ni
            self._drag_orig_pos = pos
            self._drag_orig_note = dict(self._notes[ni])
        else:
            mapped = self._scene_to_pdf(pos)
            if mapped is None:
                return
            page_idx, x_pct, y_pct = mapped
            self._notes.append({
                "page": page_idx, "x_pct": x_pct, "y_pct": y_pct,
                "text": "", "width": DEFAULT_NOTE_WIDTH,
                "height": DEFAULT_NOTE_HEIGHT,
            })
            self._render_note_icons()
            self._debounce_save_notes()
            self._enter_mode(MODE_READING)

    def _on_note_move(self, pos: QPointF):
        if self._note_dragging_idx < 0:
            return
        mapped = self._scene_to_pdf(pos)
        if mapped is None:
            return
        page_idx, x_pct, y_pct = mapped
        ni = self._note_dragging_idx
        self._notes[ni]["page"] = page_idx
        self._notes[ni]["x_pct"] = x_pct
        self._notes[ni]["y_pct"] = y_pct
        self._note_items[ni].setPos(self._note_scene_pos(self._notes[ni]))
        if ni in self._note_popups:
            icon_pos = self._note_scene_pos(self._notes[ni])
            self._note_popups[ni].setPos(icon_pos.x() + 28, icon_pos.y())

    def _on_note_release(self, pos: QPointF):
        if self._note_dragging_idx >= 0:
            self._note_dragging_idx = -1
            self._debounce_save_notes()
            self._enter_mode(MODE_READING)

    def _on_note_right_click(self, pos: QPointF):
        """Right-click note icon → delete menu."""
        ni = self._note_at_scene_pos(pos)
        if ni < 0:
            return
        menu = QMenu()
        action = menu.addAction("删除笔记")
        action.triggered.connect(lambda: self._delete_note(ni))
        menu.exec(QCursor.pos())

    def _delete_note(self, ni: int):
        if 0 <= ni < len(self._notes):
            self._close_note_popup(ni)
            self._notes.pop(ni)
            self._clear_note_icons()
            self._render_note_icons()
            self._debounce_save_notes()

    # ── Note save / load ───────────────────────────────────────────

    def set_note_path(self, path: str | None):
        self._note_path = path

    def load_notes(self):
        """Load notes from the bound note file and render icons."""
        self._clear_note_icons()
        self._notes, self._note_font_size = _load_notes_from_file(
            self._note_path)
        self._note_font_spin.setValue(self._note_font_size)
        if self._notes:
            self._render_note_icons()

    def save_notes_force(self):
        """Force-save notes immediately (called on shutdown)."""
        if self._note_save_timer is not None:
            self._note_save_timer.stop()
        # Persist current popup text and sizes
        for ni, proxy in list(self._note_popups.items()):
            editor = proxy.widget()
            if isinstance(editor, _NoteEditor):
                self._notes[ni]["text"] = editor.toPlainText()
                self._notes[ni]["width"] = int(proxy.size().width())
                self._notes[ni]["height"] = int(proxy.size().height())
        if self._note_path and self._notes is not None:
            _save_notes_to_file(
                self._note_path, self._notes, self._note_font_size)

    def _reset_note_save_timer(self):
        if self._note_save_timer is None:
            self._note_save_timer = QTimer(self)
            self._note_save_timer.setSingleShot(True)
            self._note_save_timer.timeout.connect(
                self._debounce_save_notes)
        self._note_save_timer.start(500)

    def _debounce_save_notes(self):
        """Save notes to file (called by timer timeout)."""
        if not self._note_path:
            self.note_path_needed.emit()
        if not self._note_path:
            return
        _save_notes_to_file(
            self._note_path, self._notes, self._note_font_size)

    def _on_note_font_size_changed(self, value: int):
        self._note_font_size = value
        for proxy in self._note_popups.values():
            editor = proxy.widget()
            if isinstance(editor, _NoteEditor):
                font = editor.font()
                font.setPointSize(value)
                editor.setFont(font)
        self._debounce_save_notes()

    def shutdown(self):
        self._cancel_prerender()
        self._full_cleanup()


# ── Inner scene ───────────────────────────────────────────────────────

class _SelectionScene(QGraphicsScene):
    def __init__(self, viewer: PDFViewer):
        super().__init__()
        self._viewer = viewer

    def mousePressEvent(self, event):
        pos = event.scenePos()
        # Right-click on note icon → delete menu (any mode)
        if event.button() == Qt.MouseButton.RightButton:
            ni = self._viewer._note_at_scene_pos(pos)
            if ni >= 0:
                self._viewer._on_note_right_click(pos)
                event.accept()
                return

        if event.button() == Qt.MouseButton.LeftButton and not event.modifiers():
            mode = self._viewer._zone_mode

            # Note mode: create or drag
            if mode == MODE_NOTING:
                self._viewer._on_note_press(pos)
                event.accept()
                return

            # Reading mode: check note icon click first
            if mode == MODE_READING:
                ni = self._viewer._note_at_scene_pos(pos)
                if ni >= 0:
                    self._viewer._on_note_icon_click_reading(ni)
                    event.accept()
                    return
                # Click on open popup → let Qt dispatch to the proxy widget
                if self._viewer._note_popups:
                    on_popup = False
                    for proxy in self._viewer._note_popups.values():
                        if proxy.sceneBoundingRect().contains(pos):
                            on_popup = True
                            break
                    if on_popup:
                        super().mousePressEvent(event)
                        return
                    # Click outside popups: close them all
                    for key in list(self._viewer._note_popups.keys()):
                        self._viewer._close_note_popup(key)
                    event.accept()
                    return

            # Existing mode handlers
            if mode == MODE_FRAMING:
                self._viewer._on_frame_press(pos)
                event.accept()
                return
            elif mode == MODE_MANAGING:
                self._viewer._on_manage_press(pos)
                event.accept()
                return
            else:
                self._viewer._on_scene_press(pos)
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        # Resize priority: forward directly to resizing proxy
        for proxy in self._viewer._note_popups.values():
            if getattr(proxy, '_resizing', False):
                proxy.mouseMoveEvent(event)
                return

        # In note-creation mode, don't override the note icon cursor
        if self._viewer._zone_mode == MODE_NOTING:
            if self._viewer._note_dragging_idx >= 0:
                self._viewer._on_note_move(event.scenePos())
                return
            super().mouseMoveEvent(event)
            return

        # Cursor management for note proxy edges
        pos = event.scenePos()
        popups = self._viewer._note_popups
        if popups:
            for proxy in popups.values():
                if proxy.sceneBoundingRect().contains(pos):
                    local = proxy.mapFromScene(pos)
                    w, h = proxy.size().width(), proxy.size().height()
                    on_right = local.x() >= w - 18
                    on_bottom = local.y() >= h - 18
                    if on_right and on_bottom:
                        c = Qt.CursorShape.SizeFDiagCursor
                    elif on_right:
                        c = Qt.CursorShape.SizeHorCursor
                    elif on_bottom:
                        c = Qt.CursorShape.SizeVerCursor
                    else:
                        c = Qt.CursorShape.ArrowCursor
                    self._viewer._view.viewport().setCursor(c)
                    if on_right or on_bottom:
                        return
                    break
            else:
                if self._viewer._zone_mode == MODE_READING:
                    self._viewer._view.viewport().setCursor(
                        Qt.CursorShape.PointingHandCursor)
                elif self._viewer._zone_mode == MODE_NOTING:
                    pass
                else:
                    self._viewer._view.viewport().unsetCursor()

        mode = self._viewer._zone_mode
        if mode == MODE_NOTING and self._viewer._note_dragging_idx >= 0:
            self._viewer._on_note_move(event.scenePos())
            event.accept()
            return
        elif mode == MODE_FRAMING and self._viewer._frame_start is not None:
            self._viewer._on_frame_move(event.scenePos())
            event.accept()
            return
        elif mode == MODE_MANAGING and self._viewer._selected_zone_idx >= 0 and self._viewer._zone_dragging:
            self._viewer._on_manage_move(event.scenePos())
            event.accept()
            return
        elif self._viewer._dragging:
            self._viewer._on_scene_move(event.scenePos())
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        mode = self._viewer._zone_mode
        if mode == MODE_NOTING and self._viewer._note_dragging_idx >= 0:
            self._viewer._on_note_release(event.scenePos())
            event.accept()
            return
        elif mode == MODE_FRAMING and self._viewer._frame_start is not None:
            self._viewer._on_frame_release(event.scenePos())
            event.accept()
            return
        elif mode == MODE_MANAGING and self._viewer._selected_zone_idx >= 0:
            self._viewer._on_manage_release(event.scenePos())
            event.accept()
            return
        elif self._viewer._dragging:
            self._viewer._on_scene_release(event.scenePos())
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def contextMenuEvent(self, event):
        mode = self._viewer._zone_mode
        if mode == MODE_MANAGING:
            self._viewer._on_manage_right_click(event.scenePos())
            event.accept()
            return
        # Note mode: no context menu (right-click already handled in press)
        if mode == MODE_NOTING:
            event.accept()
            return
        self._viewer._on_scene_context_menu(event.scenePos())
        event.accept()
