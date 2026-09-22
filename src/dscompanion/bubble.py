"""The little speech bubble that pops up above the character.

When an answer completes the companion shows a one-line summary in the
character's own voice ("okay daddy, i improved the safety of your game, can i
get pets now?").  This is that bubble: frameless, transparent, click-through and
focus-less, so it decorates the desktop without ever getting in the way.

It costs nothing while hidden - a single-shot timer hides it, and no repaint
happens in between.
"""

from __future__ import annotations

from PyQt5.QtCore import QRect, QSize, Qt, QTimer
from PyQt5.QtGui import QColor, QFont, QFontMetrics, QPainter, QPainterPath, QPen
from PyQt5.QtWidgets import QWidget

from . import x11

BG = QColor(16, 20, 31, 105)
BORDER = QColor(120, 160, 255, 90)
TEXT = QColor(226, 232, 248)
RADIUS = 12
TAIL_W = 18
TAIL_H = 11
PAD_X = 12
PAD_Y = 9


class SpeechBubble(QWidget):
    """A rounded bubble with a tail, anchored to the character."""

    def __init__(self, config, parent: QWidget | None = None):
        super().__init__(parent)
        self._config = config
        self._text = ""
        self._tail_down = True
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.hide)

        self.setWindowTitle("DeepSeek")
        self.setWindowFlags(
            Qt.FramelessWindowHint
            | Qt.Tool
            | Qt.WindowStaysOnTopHint
            | Qt.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setFocusPolicy(Qt.NoFocus)

    # ------------------------------------------------------------------ public
    @property
    def text(self) -> str:
        return self._text

    def show_message(self, text: str, anchor: QRect, avoid: QRect | None = None,
                     duration_ms: int | None = None) -> None:
        """Show `text` next to `anchor` (the character) and auto-hide.

        `avoid` is the chat panel when it is on screen: both windows are
        always-on-top, so the bubble picks a spot that does not slide under it.
        """
        text = " ".join(str(text).split())
        if not text or not self._config.get("bubble.enabled", True):
            return
        self._text = text
        self.setWindowOpacity(float(self._config.get("bubble.opacity", 0.9)))
        self._resize_for_text()
        self._place(anchor, avoid)
        self.show()
        self.raise_()
        self.update()
        duration = int(duration_ms if duration_ms is not None
                       else self._config.get("bubble.duration_ms", 7000))
        if duration > 0:
            self._timer.start(duration)

    def dismiss(self) -> None:
        self._timer.stop()
        self.hide()

    def reposition(self, anchor: QRect, avoid: QRect | None = None) -> None:
        if self.isVisible():
            self._place(anchor, avoid)

    # ------------------------------------------------------------------ layout
    def _font(self) -> QFont:
        font = QFont()
        font.setPointSize(int(self._config.get("bubble.font_size", 12)))
        return font

    def _text_rect_size(self) -> QSize:
        metrics = QFontMetrics(self._font())
        max_width = int(self._config.get("bubble.max_width_px", 280)) - PAD_X * 2
        rect = metrics.boundingRect(
            QRect(0, 0, max_width, 1000),
            Qt.TextWordWrap | Qt.AlignLeft,
            self._text,
        )
        return QSize(min(max_width, rect.width()), rect.height())

    def _resize_for_text(self) -> None:
        size = self._text_rect_size()
        self.resize(size.width() + PAD_X * 2, size.height() + PAD_Y * 2 + TAIL_H)

    def _candidates(self, anchor: QRect, avoid: QRect | None) -> list[QRect]:
        gap = 6
        width, height = self.width(), self.height()
        centred = anchor.center().x() - width // 2
        middle = anchor.center().y() - height // 2
        # nearest-to-the-character spots first: the scoring below only compares
        # penalties, so order decides between equally clear candidates
        options = [
            QRect(centred, anchor.top() - height - gap, width, height),      # above
            QRect(centred, anchor.bottom() + gap, width, height),            # below
            QRect(anchor.right() + gap, middle, width, height),              # right
            QRect(anchor.left() - width - gap, middle, width, height),       # left
        ]
        if avoid is not None and not avoid.isNull():
            options += [
                QRect(centred, avoid.top() - height - gap, width, height),
                QRect(centred, avoid.bottom() + gap, width, height),
                QRect(avoid.left() - width - gap, middle, width, height),
                QRect(avoid.right() + gap, middle, width, height),
            ]
        return options

    def _place(self, anchor: QRect, avoid: QRect | None = None) -> None:
        """Pick the first spot that covers neither the panel nor the character.

        Both the panel and the character are always-on-top windows, so a bubble
        that lands on them is simply invisible.  Candidates are therefore
        scored: covering the panel is worst, covering the character is bad,
        being clipped by the screen edge is mildly bad.
        """
        geo = x11.available_geometry()
        best: QRect | None = None
        best_score = 1 << 30
        for index, option in enumerate(self._candidates(anchor, avoid)):
            if not geo.contains(option):
                clipped = QRect(option)
                clipped.setWidth(min(clipped.width(), geo.width()))
                clipped.setHeight(min(clipped.height(), geo.height()))
                moved = QRect(
                    min(max(option.x(), geo.left() + 4),
                        max(geo.left() + 4, geo.right() - option.width() - 4)),
                    min(max(option.y(), geo.top() + 4),
                        max(geo.top() + 4, geo.bottom() - option.height() - 4)),
                    option.width(), option.height(),
                )
                penalty = 4
                option = moved
            else:
                penalty = 0
            if avoid is not None and not avoid.isNull() and option.intersects(avoid):
                penalty += 2
            if option.intersects(anchor):
                penalty += 1
            score = penalty * 100 + index
            if score < best_score:
                best_score, best = score, option
        chosen = best or self._candidates(anchor, avoid)[0]
        x = min(max(chosen.x(), geo.left() + 4),
                max(geo.left() + 4, geo.right() - self.width() - 4))
        y = min(max(chosen.y(), geo.top() + 4),
                max(geo.top() + 4, geo.bottom() - self.height() - 4))
        self._tail_down = y + self.height() <= anchor.center().y() + self.height() // 2
        self.move(x, y)

    # ------------------------------------------------------------------- paint
    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        body = QRect(0, 0, self.width(), self.height() - TAIL_H) if self._tail_down \
            else QRect(0, TAIL_H, self.width(), self.height() - TAIL_H)

        path = QPainterPath()
        path.addRoundedRect(
            body.x() + 0.5, body.y() + 0.5, body.width() - 1, body.height() - 1,
            RADIUS, RADIUS,
        )
        tail_x = self.width() // 2
        tail = QPainterPath()
        if self._tail_down:
            tail.moveTo(tail_x - TAIL_W // 2, body.bottom() - 2)
            tail.lineTo(tail_x, body.bottom() + TAIL_H - 1)
            tail.lineTo(tail_x + TAIL_W // 2, body.bottom() - 2)
        else:
            tail.moveTo(tail_x - TAIL_W // 2, body.top() + 2)
            tail.lineTo(tail_x, body.top() - TAIL_H + 1)
            tail.lineTo(tail_x + TAIL_W // 2, body.top() + 2)
        tail.closeSubpath()
        path = path.united(tail)

        try:
            alpha = max(0, min(255, int(self._config.get("bubble.background_alpha",
                                                         BG.alpha()))))
        except (TypeError, ValueError):
            alpha = BG.alpha()
        background = QColor(BG)
        background.setAlpha(alpha)
        border = QColor(BORDER)
        try:
            border.setAlpha(max(0, min(255, int(
                self._config.get("bubble.border_alpha", BORDER.alpha())))))
        except (TypeError, ValueError):
            pass
        painter.setPen(QPen(border, 1.4))
        painter.setBrush(background)
        painter.drawPath(path)

        painter.setPen(TEXT)
        painter.setFont(self._font())
        painter.drawText(
            body.adjusted(PAD_X, PAD_Y, -PAD_X, -PAD_Y),
            Qt.TextWordWrap | Qt.AlignLeft | Qt.AlignVCenter,
            self._text,
        )
