"""The small chat panel: one input line, a compact answer, nothing more.

Deliberately not a chat application.  The character is the interface; this panel
is a keyboard target that can be hidden entirely (gaming mode) and summoned with
a hotkey.
"""

from __future__ import annotations

from pathlib import Path

from PyQt5.QtCore import QPoint, QRect, Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QColor, QPainter, QPainterPath, QTextCursor
from PyQt5.QtWidgets import (
    QApplication,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from . import desktop

PANEL_BG = QColor(17, 20, 29, 26)        # replaced from config at construction
PANEL_BORDER = QColor(120, 160, 255, 60)
TEXT = "#dfe5f5"
DIM = "#8b95b5"
ACCENT = "#5b8cff"
ERROR = "#ff8a8a"
RADIUS = 10
INPUT_DOC_MARGIN = 2
INPUT_PADDING = 3
INPUT_BORDER = 1


def document_line_height(doc) -> float:
    """Real line height from a text layout - font metrics under-report it."""
    block = doc.firstBlock()
    if block.isValid():
        height = doc.documentLayout().blockBoundingRect(block).height()
        if height > 1:
            return float(height)
    return float(QApplication.fontMetrics().lineSpacing())


class InputEdit(QTextEdit):
    """Enter sends, Shift+Enter inserts a newline, and it grows to 3 lines."""

    submitted = pyqtSignal()
    escaped = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setPlaceholderText("Ask DeepSeek…  (Shift+Enter = newline)")
        self.setTabChangesFocus(True)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        # the document margin plus the stylesheet padding and border all sit
        # *inside* the widget, so they must be added or the last line is clipped
        self.document().setDocumentMargin(INPUT_DOC_MARGIN)
        self.setFixedHeight(self._height_for_lines(1))
        self.textChanged.connect(self._autosize)

    def _line_height(self) -> float:
        return document_line_height(self.document())

    def _height_for_lines(self, lines: int) -> int:
        chrome = 2 * (INPUT_PADDING + INPUT_BORDER) + 4
        return int(round(self._line_height() * lines + chrome))

    def _autosize(self) -> None:
        lines = min(3, max(1, self.document().blockCount()))
        height = self._height_for_lines(lines)
        if height != self.height():
            self.setFixedHeight(height)

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() in (Qt.Key_Return, Qt.Key_Enter):
            if event.modifiers() & Qt.ShiftModifier:
                super().keyPressEvent(event)
            else:
                self.submitted.emit()
            return
        if event.key() == Qt.Key_Escape:
            self.escaped.emit()
            return
        super().keyPressEvent(event)


class ChatPanel(QWidget):
    """Compact, frameless, draggable panel: status, answer, input."""

    submitted = pyqtSignal(str)
    cancel_requested = pyqtSignal()
    clear_requested = pyqtSignal()
    hidden_by_user = pyqtSignal()
    user_activity = pyqtSignal()
    api_key_entered = pyqtSignal(str)
    model_changed = pyqtSignal(str)
    models_refresh_requested = pyqtSignal()
    access_changed = pyqtSignal(str)
    approval_decision = pyqtSignal(str, str)  # call id, allow|always|deny
    attachments_changed = pyqtSignal(list)

    def __init__(self, config, parent: QWidget | None = None):
        super().__init__(parent)
        self._config = config
        self._panel_bg = QColor(PANEL_BG)
        self._panel_bg.setAlpha(self._alpha("chat.background_alpha", 26))
        self._input_bg = QColor(28, 34, 52)
        self._input_bg.setAlpha(self._alpha("chat.input_alpha", 24))
        self._border = QColor(PANEL_BORDER)
        self._border.setAlpha(self._alpha("chat.border_alpha", 60))
        self._drag_offset: QPoint | None = None
        self._press_pos: QPoint | None = None
        #: when pinned the box keeps the spot the user dragged it to; otherwise
        #: it follows the character around the desktop
        self.pinned = bool(config.get("chat.pinned", False))
        self.status_is_error = False
        self.last_error = ""
        self.busy = False
        self.attachments: list[str] = []
        self.setAcceptDrops(True)
        self._error_timer = QTimer(self)
        self._error_timer.setSingleShot(True)
        self._error_timer.timeout.connect(lambda: self.set_status(""))
        self._stream_buffer = ""
        self._idle_timer = QTimer(self)
        self._idle_timer.setSingleShot(True)
        self._idle_timer.timeout.connect(self._auto_hide)
        self._flush_timer = QTimer(self)
        self._flush_timer.setInterval(120)  # coalesce token bursts
        self._flush_timer.timeout.connect(self._flush_stream)

        self.setWindowTitle("DeepSeek")
        flags = Qt.FramelessWindowHint | Qt.Tool
        if config.get("chat.always_on_top", True):
            flags |= Qt.WindowStaysOnTopHint
        self.setWindowFlags(flags)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setWindowOpacity(float(config.get("chat.opacity", 0.96)))
        self.setMinimumWidth(280)
        self.setMaximumWidth(460)

        self._build_ui()
        self.resize(330, 120)

    @staticmethod
    def _alpha_colour(base: QColor, key: str, fallback: int) -> QColor:
        colour = QColor(base)
        colour.setAlpha(max(0, min(255, int(base.alpha()))))
        return colour

    def panel_alpha(self) -> int:
        """Current see-through level of the box background (0-255)."""
        return self._panel_bg.alpha()

    def apply_transparency(self) -> None:
        """(Re)read the see-through levels from the config."""
        self._panel_bg = QColor(PANEL_BG)
        self._panel_bg.setAlpha(self._alpha("chat.background_alpha", 26))
        self._input_bg = QColor(28, 34, 52)
        self._input_bg.setAlpha(self._alpha("chat.input_alpha", 24))
        self._border = QColor(PANEL_BORDER)
        self._border.setAlpha(self._alpha("chat.border_alpha", 60))
        self.input.setStyleSheet(self._input_style())
        self.key_input.setStyleSheet(self._key_style())
        self.update()

    def _alpha(self, key: str, fallback: int) -> int:
        try:
            return max(0, min(255, int(self._config.get(key, fallback))))
        except (TypeError, ValueError):
            return fallback

    def _input_style(self) -> str:
        border = self._border.name(QColor.HexArgb)
        return (
            f"QTextEdit {{ background:rgba({self._input_bg.red()},{self._input_bg.green()},"
            f"{self._input_bg.blue()},{self._input_bg.alpha()}); color:{TEXT};"
            f" border:1px solid {border};"
            f" border-radius:{RADIUS - 3}px; padding:{INPUT_PADDING}px 6px;"
            f" font-size:13px; }}"
        )

    def _key_style(self) -> str:
        border = self._border.name(QColor.HexArgb)
        return (
            f"QLineEdit {{ background:rgba({self._input_bg.red()},{self._input_bg.green()},"
            f"{self._input_bg.blue()},{self._input_bg.alpha()}); color:{TEXT};"
            f" border:1px solid {border};"
            f" border-radius:{RADIUS - 3}px; padding:5px 7px; font-size:12px; }}"
        )

    # --------------------------------------------------------------------- ui
    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(9, 8, 9, 8)
        outer.setSpacing(5)

        self.status = QLabel("")
        self.status.setStyleSheet(f"color:{DIM}; font-size:10px;")
        self.status.setTextInteractionFlags(Qt.NoTextInteraction)

        self.answer = QTextEdit()
        self.answer.setReadOnly(True)
        self.answer.setFrameStyle(0)
        self.answer.setStyleSheet(
            f"QTextEdit {{ background:transparent; color:{TEXT}; font-size:12px; }}"
        )
        self.answer.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.answer.document().setDocumentMargin(INPUT_DOC_MARGIN)
        max_lines = int(self._config.get("chat.max_response_lines", 6))
        self._answer_min_height = int(round(
            document_line_height(self.answer.document()) + 2 * (INPUT_PADDING + INPUT_BORDER) + 4
        ))
        self._answer_max_height = int(round(
            document_line_height(self.answer.document()) * max_lines
            + 2 * (INPUT_PADDING + INPUT_BORDER) + 4
        ))
        self.answer.setFixedHeight(self._answer_min_height)
        self.answer.hide()

        self.input = InputEdit()
        self.input.setStyleSheet(self._input_style())
        self.input.submitted.connect(self._on_submit)
        self.input.escaped.connect(self._on_escape)
        self.input.textChanged.connect(self._on_typing)

        self.send_button = QPushButton("→")
        self.send_button.setFixedSize(34, 34)
        self.send_button.setToolTip("Send (Enter)")
        self.send_button.setCursor(Qt.PointingHandCursor)
        self.send_button.setStyleSheet(
            f"QPushButton {{ background:rgba(70,110,225,120); color:white;"
            f" border:1px solid rgba(140,175,255,90);"
            f" border-radius:{RADIUS - 3}px; font-size:15px; }}"
            f"QPushButton:hover {{ background:rgba(90,130,245,190); }}"
        )
        self.send_button.clicked.connect(self._on_submit)

        self.attach_button = QPushButton("＋")
        self.attach_button.setFixedSize(26, 30)
        self.attach_button.setCursor(Qt.PointingHandCursor)
        self.attach_button.setToolTip("Attach files (or drop them on him / on this box)")
        self.attach_button.setStyleSheet(
            f"QPushButton {{ background:transparent; color:{DIM}; border:1px solid "
            f"rgba(140,175,255,55); border-radius:{RADIUS - 3}px; font-size:13px; }}"
            f"QPushButton:hover {{ color:{ACCENT}; }}"
        )
        self.attach_button.clicked.connect(self.pick_files)

        row = QHBoxLayout()
        row.setSpacing(6)
        row.addWidget(self.attach_button, 0)
        row.addWidget(self.input, 1)
        row.addWidget(self.send_button, 0)

        # one tiny row: status text plus a single close cross, so the box stays
        # small enough to feel like a speech input rather than a chat window
        tools = QHBoxLayout()
        tools.setSpacing(6)
        self.hide_button = QPushButton("✕")
        self.hide_button.setCursor(Qt.PointingHandCursor)
        self.hide_button.setToolTip("Close (Esc)")
        self.hide_button.setFixedSize(18, 18)
        self.hide_button.setStyleSheet(
            f"QPushButton {{ background:transparent; color:{DIM}; border:none;"
            f" font-size:11px; padding:0; }}"
            f"QPushButton:hover {{ color:{ERROR}; }}"
        )
        self.hide_button.clicked.connect(self.hide_panel)

        # model switcher: lists every model the key can use, one click away
        self.model_combo = QComboBox()
        self.model_combo.setCursor(Qt.PointingHandCursor)
        self.model_combo.setToolTip("DeepSeek model — ↻ refreshes the list from the API")
        self.model_combo.setStyleSheet(
            f"QComboBox {{ background:transparent; color:{DIM};"
            f" border:1px solid rgba(140,175,255,50);"
            f" border-radius:{RADIUS - 4}px; padding:1px 4px; font-size:10px; }}"
            f"QComboBox QAbstractItemView {{ background:{PANEL_BG.name(QColor.HexArgb)};"
            f" color:{TEXT}; selection-background-color:{ACCENT}; font-size:11px; }}"
        )
        self.model_combo.setEditable(True)
        self.model_combo.setInsertPolicy(QComboBox.NoInsert)
        self.model_combo.lineEdit().setPlaceholderText("model…")
        self.model_combo.lineEdit().setToolTip(
            "Pick a model, or type any name DeepSeek accepts"
        )
        # commit on a real choice or when typing finishes, not on every keystroke
        self.model_combo.activated.connect(
            lambda _index: self.model_changed.emit(self.model_combo.currentText())
        )
        self.model_combo.lineEdit().editingFinished.connect(
            lambda: self.model_changed.emit(self.model_combo.currentText())
        )
        self.refresh_button = QPushButton("↻")
        self.refresh_button.setFixedSize(18, 18)
        self.refresh_button.setCursor(Qt.PointingHandCursor)
        self.refresh_button.setToolTip("Fetch the model list from DeepSeek")
        self.refresh_button.setStyleSheet(
            f"QPushButton {{ background:transparent; color:{DIM}; border:none;"
            f" font-size:12px; padding:0; }}"
            f"QPushButton:hover {{ color:{ACCENT}; }}"
        )
        self.refresh_button.clicked.connect(self.models_refresh_requested.emit)

        # desktop access: off / ask / full - the DeepSeek-Harness-style switch
        self.access_combo = QComboBox()
        self.access_combo.setCursor(Qt.PointingHandCursor)
        self.access_combo.addItems(["off", "ask", "full"])
        self.access_combo.setToolTip(
            "Desktop access — off: chat only\n"
            "ask: he asks before every command or file change\n"
            "full: he just does it"
        )
        self.access_combo.setStyleSheet(self.model_combo.styleSheet())
        self.access_combo.currentTextChanged.connect(self.access_changed.emit)

        tools.addWidget(self.status, 1)
        tools.addWidget(self.model_combo, 0)
        tools.addWidget(self.refresh_button, 0)
        tools.addWidget(self.access_combo, 0)
        tools.addWidget(self.hide_button, 0)

        # Paste-the-key row: shown when no key is configured (or on demand from
        # the context menu) so the key never has to be put in place by hand.
        self.key_input = QLineEdit()
        self.key_input.setEchoMode(QLineEdit.Password)
        self.key_input.setPlaceholderText("paste your DeepSeek API key (sk-…)")
        self.key_input.setStyleSheet(self._key_style())
        self.key_save = QPushButton("save")
        self.key_save.setCursor(Qt.PointingHandCursor)
        self.key_save.setStyleSheet(
            f"QPushButton {{ background:rgba(60,92,190,220); color:white; border:none;"
            f" border-radius:{RADIUS - 3}px; font-size:12px; padding:5px 10px; }}"
            f"QPushButton:hover {{ background:rgba(80,120,235,240); }}"
        )
        self.key_input.returnPressed.connect(self._emit_key)
        self.key_save.clicked.connect(self._emit_key)
        self.key_note = QLabel("stored locally with mode 600 — or set $DEEPSEEK_API_KEY")
        self.key_note.setStyleSheet(f"color:{DIM}; font-size:10px;")
        self.key_row = QWidget()
        key_layout = QVBoxLayout(self.key_row)
        key_layout.setContentsMargins(0, 0, 0, 0)
        key_layout.setSpacing(3)
        key_line = QHBoxLayout()
        key_line.setSpacing(6)
        key_line.addWidget(self.key_input, 1)
        key_line.addWidget(self.key_save, 0)
        key_layout.addLayout(key_line)
        key_layout.addWidget(self.key_note)
        self.key_row.hide()

        # approval bar: shown when he wants to run something and access is "ask"
        self.approval_call_id = ""
        self.approval_label = QLabel("")
        self.approval_label.setWordWrap(True)
        self.approval_label.setStyleSheet(f"color:{TEXT}; font-size:11px;")
        self.allow_button = QPushButton("allow")
        self.always_button = QPushButton("always")
        self.deny_button = QPushButton("deny")
        for button, colour in ((self.allow_button, "rgba(60,140,90,220)"),
                               (self.always_button, "rgba(60,92,190,220)"),
                               (self.deny_button, "rgba(150,60,70,220)")):
            button.setCursor(Qt.PointingHandCursor)
            button.setStyleSheet(
                f"QPushButton {{ background:{colour}; color:white; border:none;"
                f" border-radius:{RADIUS - 3}px; font-size:11px; padding:4px 9px; }}"
            )
        self.allow_button.clicked.connect(lambda: self._decide("allow"))
        self.always_button.clicked.connect(lambda: self._decide("always"))
        self.deny_button.clicked.connect(lambda: self._decide("deny"))
        approval_buttons = QHBoxLayout()
        approval_buttons.setSpacing(6)
        approval_buttons.addWidget(self.allow_button, 0)
        approval_buttons.addWidget(self.always_button, 0)
        approval_buttons.addWidget(self.deny_button, 0)
        approval_buttons.addStretch(1)
        self.approval_row = QWidget()
        approval_layout = QVBoxLayout(self.approval_row)
        approval_layout.setContentsMargins(0, 0, 0, 0)
        approval_layout.setSpacing(4)
        approval_layout.addWidget(self.approval_label)
        approval_layout.addLayout(approval_buttons)
        self.approval_row.hide()

        self.attachment_label = QLabel("")
        self.attachment_label.setWordWrap(True)
        self.attachment_label.setStyleSheet(f"color:{ACCENT}; font-size:10px;")
        self.attachment_clear = QPushButton("✕")
        self.attachment_clear.setFixedSize(16, 16)
        self.attachment_clear.setCursor(Qt.PointingHandCursor)
        self.attachment_clear.setStyleSheet(
            f"QPushButton {{ background:transparent; color:{DIM}; border:none; font-size:10px; }}"
            f"QPushButton:hover {{ color:{ERROR}; }}"
        )
        self.attachment_clear.clicked.connect(self.clear_attachments)
        self.attachment_row = QWidget()
        attachment_layout = QHBoxLayout(self.attachment_row)
        attachment_layout.setContentsMargins(0, 0, 0, 0)
        attachment_layout.setSpacing(4)
        attachment_layout.addWidget(self.attachment_label, 1)
        attachment_layout.addWidget(self.attachment_clear, 0)
        self.attachment_row.hide()

        outer.addWidget(self.answer)
        outer.addWidget(self.attachment_row)
        outer.addWidget(self.approval_row)
        outer.addWidget(self.key_row)
        outer.addLayout(tools)
        outer.addLayout(row)

    # ------------------------------------------------------------------ paint
    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        path = QPainterPath()
        path.addRoundedRect(0.5, 0.5, self.width() - 1, self.height() - 1, RADIUS, RADIUS)
        painter.fillPath(path, self._panel_bg)
        painter.setPen(self._border)
        painter.drawPath(path)

    # ---------------------------------------------------------------- dragging
    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton:
            self._press_pos = event.globalPos()
            self._drag_offset = event.globalPos() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._drag_offset is not None and event.buttons() & Qt.LeftButton:
            self.move(event.globalPos() - self._drag_offset)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if self._drag_offset is None:
            return
        # A click on the box body must not count as a drag: pinning the position
        # on every click is what left the box behind when the character moved.
        travelled = 0
        if self._press_pos is not None:
            travelled = (event.globalPos() - self._press_pos).manhattanLength()
        self._drag_offset = None
        self._press_pos = None
        if travelled <= 5:
            self.input.setFocus(Qt.OtherFocusReason)
            return
        self.pinned = True
        self._config.set("chat.pinned", True)
        self._config.set("chat.x", self.x())
        self._config.set("chat.y", self.y())
        self._config.save()

    def move_by(self, delta: QPoint) -> None:
        """Shift with the character, keeping a pinned box at the same offset."""
        if delta.isNull() or not self.isVisible():
            return
        point = desktop.clamp_to_screens(QRect(self.pos() + delta, self.size()))
        self.move(point)
        if self.pinned:
            self._config.set("chat.x", self.x())
            self._config.set("chat.y", self.y())

    def unpin(self) -> None:
        """Let the box follow the character again."""
        self.pinned = False
        self._config.set("chat.pinned", False)
        self._config.set("chat.x", -1)
        self._config.set("chat.y", -1)
        self._config.save()

    def dragMoveEvent(self, event) -> None:  # noqa: N802
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    # ------------------------------------------------------------------ answer
    def begin_answer(self) -> None:
        self._stream_buffer = ""
        self.answer.clear()
        if self._config.get("chat.show_response", True):
            self.answer.show()
            self._relayout()

    def append_token(self, text: str) -> None:
        self._stream_buffer += text
        if not self._flush_timer.isActive():
            self._flush_timer.start()

    def _flush_stream(self) -> None:
        if not self._stream_buffer:
            self._flush_timer.stop()
            return
        cursor = self.answer.textCursor()
        cursor.movePosition(QTextCursor.End)
        cursor.insertText(self._stream_buffer)
        self._stream_buffer = ""
        self.answer.setTextCursor(cursor)
        self.answer.ensureCursorVisible()
        self._fit_answer()

    def end_answer(self, note: str = "") -> None:
        self._flush_stream()
        self._flush_timer.stop()
        if note:
            self.set_status(note)

    def _keep_on_screen(self) -> None:
        """Growing the answer must not push the panel off the display."""
        geo = desktop.available_geometry()
        y = min(self.y(), geo.bottom() - self.height() - 4)
        x = min(max(self.x(), geo.left() + 4), geo.right() - self.width() - 4)
        if (x, y) != (self.x(), self.y()):
            self.move(x, max(y, geo.top() + 4))

    def clear_answer(self) -> None:
        self.answer.clear()
        self.answer.hide()
        self.answer.setFixedHeight(self._answer_min_height)
        self.set_status("")
        self._relayout()

    # ------------------------------------------------------------------ status
    def set_status(self, text: str, *, error: bool = False) -> None:
        """Show a short status/error line.  Errors auto-expire."""
        self._error_timer.stop()
        self.status_is_error = bool(error and text)
        if self.status_is_error:
            self.last_error = text
        elif not text:
            self.last_error = ""
        self.status.setText(text)
        self.status.setStyleSheet(f"color:{ERROR if error else DIM}; font-size:11px;")
        if self.status_is_error:
            self._error_timer.start(int(self._config.get("behavior.error_notice_ms", 6000)))
        self._relayout()

    # ------------------------------------------------------------------ layout
    def _fit_answer(self) -> None:
        """Keep the answer area as small as its text allows (up to the max)."""
        doc = self.answer.document()
        doc.setTextWidth(max(1, self.answer.viewport().width()))
        needed = int(round(doc.size().height() + 2 * (INPUT_PADDING + INPUT_BORDER) + 4))
        height = max(self._answer_min_height, min(self._answer_max_height, needed))
        if height != self.answer.height():
            self.answer.setFixedHeight(height)

    def _relayout(self) -> None:
        self.layout().activate()
        if self.answer.isVisible():
            self._fit_answer()
        self.adjustSize()
        if self.isVisible():
            self._keep_on_screen()

    def move_near(self, anchor: QRect) -> None:
        """Place the panel next to the character, never on top of it.

        Preferred spot is directly under the character; if the screen ends
        first it goes above, and failing that beside it.  A position the user
        dragged the panel to always wins (clamped back on screen).
        """
        width, height = self.width(), self.height()
        stored_x = int(self._config.get("chat.x", -1))
        stored_y = int(self._config.get("chat.y", -1))
        if self.pinned and stored_x >= 0 and stored_y >= 0:
            point = desktop.clamp_to_screens(QRect(stored_x, stored_y, width, height))
            self.move(point)
            return

        geo = desktop.available_geometry()
        gap = 8
        x = anchor.center().x() - width // 2
        y = anchor.bottom() + gap
        if y + height > geo.bottom():
            if anchor.top() - height - gap >= geo.top():
                y = anchor.top() - height - gap
            else:
                y = min(max(anchor.center().y() - height // 2, geo.top() + 4),
                        geo.bottom() - height - 4)
                x = anchor.right() + gap
                if x + width > geo.right():
                    x = anchor.left() - width - gap
        x = min(max(x, geo.left() + 4), max(geo.left() + 4, geo.right() - width - 4))
        y = min(max(y, geo.top() + 4), max(geo.top() + 4, geo.bottom() - height - 4))
        self.move(x, y)

    # ------------------------------------------------------------------ events
    def _on_submit(self) -> None:
        text = self.input.toPlainText().strip()
        if not text:
            return
        self.input.clear()
        self.submitted.emit(text)

    def _on_escape(self) -> None:
        self.cancel_requested.emit()

    def _on_typing(self) -> None:
        # The app uses this to relax FINISHED back to LISTENING.
        self.user_activity.emit()

    # ------------------------------------------------------------- attachments
    def pick_files(self) -> None:
        # imported here on purpose: Qt's file-dialog stack (models, storage
        # info, locale data) costs ~20 MB resident, and only this button needs it
        from PyQt5.QtWidgets import QFileDialog

        paths, _filter = QFileDialog.getOpenFileNames(self, "Attach files to your message")
        if paths:
            self.attach_paths(paths)

    def attach_paths(self, paths) -> None:
        added = []
        for raw in paths:
            path = str(raw)
            if path and path not in self.attachments:
                self.attachments.append(path)
                added.append(path)
        if not added:
            return
        self._refresh_attachments()
        self.attachments_changed.emit(list(self.attachments))

    def clear_attachments(self) -> None:
        if not self.attachments:
            return
        self.attachments = []
        self._refresh_attachments()
        self.attachments_changed.emit([])

    def take_attachments(self) -> list[str]:
        """Hand the attachments to the app and clear the row."""
        current = list(self.attachments)
        self.attachments = []
        self._refresh_attachments()
        self.attachments_changed.emit([])
        return current

    def _refresh_attachments(self) -> None:
        if not self.attachments:
            self.attachment_row.hide()
            self._relayout()
            return
        names = [Path(p).name for p in self.attachments]
        shown = ", ".join(names[:4]) + ("…" if len(names) > 4 else "")
        self.attachment_label.setText(f"📎 {len(names)} attached: {shown}")
        self.attachment_row.show()
        self._relayout()

    # ------------------------------------------------------- drag and drop
    @staticmethod
    def _drop_paths(event) -> list[str]:
        data = event.mimeData()
        if not data.hasUrls():
            return []
        paths = []
        for url in data.urls():
            local = url.toLocalFile()
            if local:
                paths.append(local)
        return paths

    def dragEnterEvent(self, event) -> None:  # noqa: N802
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dropEvent(self, event) -> None:  # noqa: N802
        paths = self._drop_paths(event)
        if paths:
            self.attach_paths(paths)
            event.acceptProposedAction()

    # --------------------------------------------------------------- approvals
    def _decide(self, decision: str) -> None:
        call_id = self.approval_call_id
        self.hide_approval()
        if call_id:
            self.approval_decision.emit(call_id, decision)

    def ask_approval(self, call_id: str, description: str) -> None:
        self.approval_call_id = call_id
        self.approval_label.setText(f"let him run this?\n{description}")
        self.approval_row.show()
        self._relayout()
        self.allow_button.setFocus(Qt.OtherFocusReason)

    def hide_approval(self) -> None:
        self.approval_call_id = ""
        self.approval_row.hide()
        self._relayout()

    def access_mode(self) -> str:
        return self.access_combo.currentText() or "ask"

    def set_access_mode(self, mode: str) -> None:
        blocked = self.access_combo.blockSignals(True)
        if mode in ("off", "ask", "full"):
            self.access_combo.setCurrentText(mode)
        self.access_combo.blockSignals(blocked)
        self._relayout()

    def note_tool(self, text: str) -> None:
        """Write a one-line record of a tool call into the answer area."""
        self.answer.show()
        text = " ".join(str(text).split())
        if len(text) > 130:
            text = text[:127] + "…"
        cursor = self.answer.textCursor()
        cursor.movePosition(QTextCursor.End)
        if self.answer.toPlainText().strip():
            cursor.insertText("\n")
        # trailing newline so the model's next words start on their own line
        cursor.insertText(f"▸ {text}\n")
        self.answer.setTextCursor(cursor)
        self.answer.ensureCursorVisible()
        self._fit_answer()
        self._relayout()

    def _emit_key(self) -> None:
        key = self.key_input.text().strip()
        if key:
            self.api_key_entered.emit(key)

    def set_models(self, models, current: str = "") -> None:
        """Fill the model dropdown without re-emitting model_changed for it."""
        models = [str(m) for m in models if str(m).strip()]
        blocked = self.model_combo.blockSignals(True)
        self.model_combo.clear()
        self.model_combo.addItems(models)
        if current and current in models:
            self.model_combo.setCurrentText(current)
        elif current:
            self.model_combo.addItem(current)
            self.model_combo.setCurrentText(current)
        self.model_combo.blockSignals(blocked)
        self._relayout()

    def current_model(self) -> str:
        return self.model_combo.currentText()

    def show_key_row(self, *, focus: bool = True) -> None:
        self.key_row.show()
        self._relayout()
        if focus:
            self.key_input.setFocus(Qt.OtherFocusReason)

    def hide_key_row(self) -> None:
        self.key_input.clear()
        self.key_row.hide()

        # approval bar: shown when he wants to run something and access is "ask"
        self.approval_call_id = ""
        self.approval_label = QLabel("")
        self.approval_label.setWordWrap(True)
        self.approval_label.setStyleSheet(f"color:{TEXT}; font-size:11px;")
        self.allow_button = QPushButton("allow")
        self.always_button = QPushButton("always")
        self.deny_button = QPushButton("deny")
        for button, colour in ((self.allow_button, "rgba(60,140,90,220)"),
                               (self.always_button, "rgba(60,92,190,220)"),
                               (self.deny_button, "rgba(150,60,70,220)")):
            button.setCursor(Qt.PointingHandCursor)
            button.setStyleSheet(
                f"QPushButton {{ background:{colour}; color:white; border:none;"
                f" border-radius:{RADIUS - 3}px; font-size:11px; padding:4px 9px; }}"
            )
        self.allow_button.clicked.connect(lambda: self._decide("allow"))
        self.always_button.clicked.connect(lambda: self._decide("always"))
        self.deny_button.clicked.connect(lambda: self._decide("deny"))
        approval_buttons = QHBoxLayout()
        approval_buttons.setSpacing(6)
        approval_buttons.addWidget(self.allow_button, 0)
        approval_buttons.addWidget(self.always_button, 0)
        approval_buttons.addWidget(self.deny_button, 0)
        approval_buttons.addStretch(1)
        self.approval_row = QWidget()
        approval_layout = QVBoxLayout(self.approval_row)
        approval_layout.setContentsMargins(0, 0, 0, 0)
        approval_layout.setSpacing(4)
        approval_layout.addWidget(self.approval_label)
        approval_layout.addLayout(approval_buttons)
        self.approval_row.hide()
        self._relayout()

    def hide_panel(self) -> None:
        self._idle_timer.stop()
        self.hide()
        self.hidden_by_user.emit()

    def _auto_hide(self) -> None:
        """Close the box when it has been sitting idle and unused."""
        if self.approval_call_id:
            return  # he is waiting for an answer - never hide the question
        if self.isVisible() and not self.busy and not self.input.toPlainText().strip():
            self.hide_panel()

    def changeEvent(self, event) -> None:  # noqa: N802
        """Hide when the user clicks somewhere else."""
        super().changeEvent(event)
        if event.type() != event.ActivationChange:
            return
        if not self.isVisible() or self.isActiveWindow():
            return
        if not self._config.get("chat.hide_on_focus_loss", True):
            return
        if self.approval_call_id:
            return  # an approval is pending: it must stay readable
        if self.busy or self.input.toPlainText().strip():
            return  # never yank it away mid-question
        self.hide_panel()

    def note_activity(self) -> None:
        """Called by the app when the conversation is busy, to hold it open."""
        if self.busy:
            self._idle_timer.stop()
        else:
            delay = int(self._config.get("chat.auto_hide_ms", 45000))
            if delay > 0:
                self._idle_timer.start(delay)

    def set_busy(self, busy: bool) -> None:
        self.busy = busy
        self.send_button.setText("■" if busy else "→")
        self.send_button.setToolTip("Stop (Esc)" if busy else "Send (Enter)")
        self.note_activity()

    def reveal(self, *, focus: bool = True) -> None:
        self.show()
        if focus:
            self.raise_()
            self.activateWindow()
            self.input.setFocus(Qt.OtherFocusReason)
