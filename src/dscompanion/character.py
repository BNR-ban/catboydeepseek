"""The desktop companion window: transparent, borderless, draggable, cheap.

Design notes for the performance goal:

* the widget paints the current state pixmap in ``paintEvent`` and nothing else -
  there is no animation, no timer and no periodic repaint, so a resting
  companion costs 0% CPU;
* a repaint happens only when the state actually changes (``set_state``) or when
  the window is resized/opacity changes;
* all six sprites share one canvas size, so switching state never moves or
  resizes anything on screen.
"""

from __future__ import annotations

from PyQt5.QtCore import QPoint, QRect, QSize, Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QColor, QPainter, QPainterPath, QPixmap, QRadialGradient
from PyQt5.QtWidgets import QWidget

from . import x11
from .assets import AssetSet
from .states import State

#: On-screen height of the tallest pose at scale = 1.0
BASE_CONTENT_HEIGHT = 260
CORNER_GRAB_PX = 14
#: how far the pointer may travel and still count as a click, not a drag
CLICK_SLOP_PX = 5

#: Where the cheeks are on each pose, in sprite-canvas pixels (512x532).  Used
#: to paint the blush when the character is petted.  Poses that cover the face
#: with hands keep the blush next to the hands, still on the face.
CHEEKS: dict[str, tuple[tuple[int, int], tuple[int, int], int]] = {
    "listening": ((190, 378), (282, 378), 27),
    "thinking": ((243, 352), (330, 352), 24),
    "thinking_longer": ((220, 358), (302, 352), 24),
    "talking": ((198, 318), (284, 316), 23),
    "proud": ((176, 203), (252, 206), 22),
    "finished": ((188, 316), (262, 312), 22),
}
BLUSH_COLOUR = QColor(255, 118, 158)
HEART_COLOUR = QColor(255, 140, 175)


class CharacterWindow(QWidget):
    """A frameless, always-on-top window showing the character."""

    clicked = pyqtSignal()
    files_dropped = pyqtSignal(list)  # files dragged onto him
    petted = pyqtSignal()          # a tap: one short purr line
    pet_progress = pyqtSignal(int)  # while held down: 0, 1, 2 ... ticks
    context_menu_requested = pyqtSignal(QPoint)
    geometry_changed = pyqtSignal()
    scale_changed = pyqtSignal(float)
    opacity_changed = pyqtSignal(float)

    def __init__(self, assets: AssetSet, config, parent: QWidget | None = None):
        super().__init__(parent)
        self._assets = assets
        self._config = config
        self._state = State.LISTENING
        self._scale = float(config.get("character.scale", 1.0))
        self._click_through = False
        self._gaming = False
        self._drag_offset: QPoint | None = None
        self._press_pos: QPoint | None = None
        self._resize_origin: tuple[QPoint, float, int] | None = None
        self._pixmap: QPixmap | None = None
        self._composited = x11.have_compositor()
        self._drag_hover = False
        self._blush = 0.0          # 0..1, decays after a pet
        self._blush_steps = 0
        self._blush_timer = QTimer(self)
        self._blush_timer.setInterval(45)
        self._blush_timer.timeout.connect(self._decay_blush)
        self._pet_hold = False
        self._pet_ticks = 0
        self._pet_timer = QTimer(self)
        self._pet_timer.setInterval(int(config.get("pet.hold_tick_ms", 480)))
        self._pet_timer.timeout.connect(self._pet_tick)

        self.setWindowTitle("DeepSeek")
        flags = Qt.FramelessWindowHint | Qt.Tool | Qt.WindowDoesNotAcceptFocus
        if config.get("character.always_on_top", True):
            flags |= Qt.WindowStaysOnTopHint
        self.setWindowFlags(flags)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WA_NoSystemBackground, True)
        self.setFocusPolicy(Qt.NoFocus)
        self.setMouseTracking(True)
        self.setAcceptDrops(True)  # drop a file on him to attach it
        self.setWindowOpacity(float(config.get("character.opacity", 1.0)))

        self._apply_geometry()
        self.set_state(self._state, force=True)
        if config.get("character.sticky", True) and x11.is_x11():
            # after the window is mapped, tell the WM to keep it everywhere
            QTimer.singleShot(0, lambda: x11.set_sticky(int(self.winId()), True))

    # ------------------------------------------------------------------ layout
    def canvas_size(self) -> QSize:
        canvas = self._assets.canvas
        if not canvas.isValid() or canvas.height() == 0:
            return QSize(BASE_CONTENT_HEIGHT, BASE_CONTENT_HEIGHT)
        return canvas

    def size_for_scale(self, scale: float) -> QSize:
        canvas = self.canvas_size()
        factor = (BASE_CONTENT_HEIGHT * scale) / max(canvas.height(), 1)
        return QSize(max(48, int(canvas.width() * factor)),
                     max(48, int(canvas.height() * factor)))

    def desired_size(self) -> QSize:
        return self.size_for_scale(self._scale)

    def _apply_geometry(self) -> None:
        size = self.desired_size()
        x = int(self._config.get("character.x", -1))
        y = int(self._config.get("character.y", -1))
        if x < 0 or y < 0:
            geo = x11.available_geometry()
            x = geo.right() - size.width() - 30
            y = geo.bottom() - size.height() - 10
        else:
            point = x11.clamp_to_screens(QRect(x, y, size.width(), size.height()))
            x, y = point.x(), point.y()
        self.setGeometry(x, y, size.width(), size.height())

    # ------------------------------------------------------------------ state
    @property
    def state(self) -> State:
        return self._state

    def set_state(self, state: State, *, force: bool = False) -> None:
        if state is self._state and not force:
            return
        self._state = state
        pixmap = self._assets.pixmap(state)
        self._pixmap = pixmap.scaled(
            self.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation
        )
        if not self._composited:
            # Without a compositor an ARGB window paints as a black box, so clip
            # the window to the sprite silhouette instead (1-bit transparency).
            x11.apply_shape_mask(self, self._pixmap)
        self.update()

    def reload_assets(self, assets: AssetSet) -> None:
        self._assets = assets
        self.set_state(self._state, force=True)

    # ------------------------------------------------------------------ paint
    def paintEvent(self, event) -> None:  # noqa: N802 (Qt naming)
        if self._pixmap is None or self._pixmap.isNull():
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
        # The sprite canvas is bottom-centre anchored inside the widget, which
        # keeps every pose on the same baseline.
        x = (self.width() - self._pixmap.width()) // 2
        y = self.height() - self._pixmap.height()
        painter.drawPixmap(x, y, self._pixmap)
        if self._blush > 0.01:
            painter.setRenderHint(QPainter.Antialiasing, True)
            self._draw_blush(painter, x, y)

    # ------------------------------------------------------------------ mouse
    def _in_resize_corner(self, pos: QPoint) -> bool:
        return (pos.x() >= self.width() - CORNER_GRAB_PX
                and pos.y() >= self.height() - CORNER_GRAB_PX)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton:
            self._press_pos = event.globalPos()
            if self._in_resize_corner(event.pos()):
                self._resize_origin = (event.globalPos(), self._scale, self.height())
                self._drag_offset = None
            else:
                self._drag_offset = event.globalPos() - self.frameGeometry().topLeft()
                self._resize_origin = None
                self.start_pet_hold()
            event.accept()
        elif event.button() == Qt.RightButton:
            self.context_menu_requested.emit(event.globalPos())
            event.accept()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._resize_origin is not None:
            origin, start_scale, start_height = self._resize_origin
            delta = event.globalPos() - origin
            grown = max(48, start_height + delta.y())
            self.set_scale(start_scale * grown / max(start_height, 1), emit=False)
            event.accept()
            return
        if self._drag_offset is not None and event.buttons() & Qt.LeftButton:
            if self._pet_hold and self._press_pos is not None:
                travelled = (event.globalPos() - self._press_pos).manhattanLength()
                if travelled > CLICK_SLOP_PX:
                    self.stop_pet_hold()  # he is being dragged, not petted
            self.move(event.globalPos() - self._drag_offset)
            event.accept()
            return
        if self._in_resize_corner(event.pos()):
            self.setCursor(Qt.SizeFDiagCursor)
        else:
            self.unsetCursor()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        # A press always arms a drag, so "did the pointer actually travel?" is
        # what separates a drag from a click - without this every click would
        # look like a (zero-length) drag and never pet him.
        travelled = 0
        if self._press_pos is not None:
            travelled = (event.globalPos() - self._press_pos).manhattanLength()
        was_drag = self._drag_offset is not None and travelled > CLICK_SLOP_PX
        was_resize = self._resize_origin is not None
        was_petting = self._pet_hold
        self.stop_pet_hold()
        self._drag_offset = None
        self._resize_origin = None
        self._press_pos = None
        if was_drag or was_resize:
            self._store_geometry()
        elif event.button() == Qt.LeftButton:
            if was_petting:
                self.pet()  # one last line once the hand comes off
            self.clicked.emit()
        event.accept()

    # ------------------------------------------------------------ drag & drop
    def dragEnterEvent(self, event) -> None:  # noqa: N802
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            self._drag_hover = True
            self.update()

    def dragLeaveEvent(self, event) -> None:  # noqa: N802
        self._drag_hover = False
        self.update()

    def dropEvent(self, event) -> None:  # noqa: N802
        self._drag_hover = False
        self.update()
        paths = [url.toLocalFile() for url in event.mimeData().urls() if url.toLocalFile()]
        if paths:
            self.files_dropped.emit(paths)
            event.acceptProposedAction()

    def wheelEvent(self, event) -> None:  # noqa: N802
        steps = event.angleDelta().y() / 120.0
        if event.modifiers() & Qt.ControlModifier:
            self.set_opacity(self.windowOpacity() + steps * 0.05)
        else:
            self.set_scale(self._scale * (1.0 + steps * 0.08))
        event.accept()

    # ---------------------------------------------------------------- geometry
    def _store_geometry(self) -> None:
        self._config.set("character.x", self.x())
        self._config.set("character.y", self.y())
        self._config.set("character.scale", round(self._scale, 3))
        self.geometry_changed.emit()

    def set_scale(self, scale: float, *, emit: bool = True) -> None:
        scale = max(0.35, min(3.0, scale))
        if abs(scale - self._scale) < 1e-3:
            return
        self._scale = scale
        size = self.size_for_scale(scale)
        self.setGeometry(self.x(), self.y(), size.width(), size.height())
        self.set_state(self._state, force=True)
        if emit:
            self.scale_changed.emit(scale)
            self._store_geometry()

    def set_opacity(self, opacity: float) -> None:
        opacity = max(0.15, min(1.0, opacity))
        self.setWindowOpacity(opacity)
        if not self._gaming:
            self._config.set("character.opacity", round(opacity, 2))
        self.opacity_changed.emit(opacity)

    def store_position(self) -> None:
        self._store_geometry()

    # ------------------------------------------------------------- win features
    def set_always_on_top(self, on: bool) -> None:
        self.setWindowFlag(Qt.WindowStaysOnTopHint, on)
        self.show()  # re-mapping is required for the WM to notice the change
        self.ensure_sticky(150)

    def set_click_through(self, on: bool) -> None:
        """Let mouse events fall through to whatever is underneath."""
        if on == self._click_through:
            return
        self._click_through = on
        self.setWindowFlag(Qt.WindowTransparentForInput, on)
        self.show()
        self.ensure_sticky(150)
        if x11.is_x11():
            x11.set_input_shape_empty(int(self.winId()), on)

    @property
    def click_through(self) -> bool:
        return self._click_through

    def set_gaming(self, on: bool) -> None:
        self._gaming = on

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self.ensure_sticky()

    def ensure_sticky(self, delay_ms: int = 0) -> None:
        """Re-assert the sticky hint; a re-map (flag change, re-show) drops it.

        Touching a window flag makes Qt recreate the native window, and the
        window manager finishes re-managing it a moment later, so the hint is
        applied twice: immediately and once things have settled.
        """
        if not self._config.get("character.sticky", True) or not x11.is_x11():
            return

        def apply() -> None:
            try:
                x11.set_sticky(int(self.winId()), True)
            except RuntimeError:  # window already gone
                pass

        QTimer.singleShot(delay_ms, apply)
        QTimer.singleShot(delay_ms + 250, apply)

    # ------------------------------------------------------------------- petting
    @property
    def blush(self) -> float:
        return self._blush

    def pet(self) -> None:
        """A tap: blush, one purr line (no sound), and look pleased."""
        if not self._config.get("pet.enabled", True):
            return
        self._blush = 1.0
        self._blush_steps = 0
        if not self._blush_timer.isActive():
            self._blush_timer.start()
        self.update()
        self.petted.emit()

    # -- held down: keep blushing and keep purring, growing the purr as it goes
    def start_pet_hold(self) -> None:
        if not self._config.get("pet.enabled", True) or self._pet_hold:
            return
        self._pet_hold = True
        self._pet_ticks = 0
        self._blush = 1.0
        self._blush_steps = 0
        if self._blush_timer.isActive():
            self._blush_timer.stop()  # full blush the whole time he is petted
        self.update()
        self.pet_progress.emit(0)
        self._pet_timer.start()

    def stop_pet_hold(self) -> None:
        if not self._pet_hold:
            return
        self._pet_hold = False
        self._pet_timer.stop()
        if not self._blush_timer.isActive():
            self._blush_timer.start()  # now he may cool down again
        self.update()

    @property
    def petting(self) -> bool:
        return self._pet_hold

    def _pet_tick(self) -> None:
        self._pet_ticks += 1
        self._blush = 1.0
        self.update()
        self.pet_progress.emit(self._pet_ticks)

    def _decay_blush(self) -> None:
        step = max(0.02, 45.0 / max(200.0, float(self._config.get("pet.blush_ms", 1400))))
        self._blush -= step
        if self._blush <= 0.0:
            self._blush = 0.0
            self._blush_timer.stop()
        self.update()

    def _draw_blush(self, painter: QPainter, offset_x: int, offset_y: int) -> None:
        """Soft pink cheeks plus a couple of hearts while the blush lasts."""
        pose = CHEEKS.get(self._state.value)
        if pose is None or self._pixmap is None:
            return
        scale = self._pixmap.width() / max(1.0, float(self._assets.canvas.width()))
        level = self._blush
        painter.setPen(Qt.NoPen)

        for cx, cy in pose[:2]:
            radius = pose[2] * scale * 1.85
            x = offset_x + cx * scale
            y = offset_y + cy * scale
            gradient = QRadialGradient(x, y, radius)
            inner = QColor(BLUSH_COLOUR)
            inner.setAlphaF(min(0.72, 0.66 * level))
            mid = QColor(BLUSH_COLOUR)
            mid.setAlphaF(min(0.38, 0.36 * level))
            outer = QColor(BLUSH_COLOUR)
            outer.setAlphaF(0.0)
            gradient.setColorAt(0.0, inner)
            gradient.setColorAt(0.55, mid)
            gradient.setColorAt(1.0, outer)
            painter.setBrush(gradient)
            painter.drawEllipse(QPoint(int(x), int(y)), int(radius), int(radius * 0.78))

        # a few hearts drifting up from the head
        rise = (1.0 - level) * 14.0
        for index, (hx, hy, size) in enumerate(
            ((pose[0][0] + 26, pose[0][1] - 78, 12), (pose[1][0] - 16, pose[1][1] - 104, 15),
             ((pose[0][0] + pose[1][0]) // 2, pose[1][1] - 138, 9))
        ):
            colour = QColor(HEART_COLOUR)
            colour.setAlphaF(min(0.9, level * (0.9 - index * 0.18)))
            painter.setBrush(colour)
            x = offset_x + hx * scale
            y = offset_y + (hy - rise - index * 4) * scale
            self._draw_heart(painter, x, y, size * scale)

    @staticmethod
    def _draw_heart(painter: QPainter, x: float, y: float, size: float) -> None:
        path = QPainterPath()
        path.moveTo(x, y + size * 0.35)
        path.cubicTo(x - size, y - size * 0.5, x - size * 0.35, y - size, x, y - size * 0.35)
        path.cubicTo(x + size * 0.35, y - size, x + size, y - size * 0.5, x, y + size * 0.35)
        painter.drawPath(path)
