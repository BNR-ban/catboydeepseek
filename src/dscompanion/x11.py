"""X11 plumbing: compositor detection, click-through, shape masks, placement.

Everything here degrades gracefully: if a call is unavailable the app keeps
running with the plain Qt behaviour instead of crashing.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import os
from typing import Optional

from PyQt5.QtCore import QPoint, QRect
from PyQt5.QtGui import QPixmap, QRegion
from PyQt5.QtWidgets import QApplication

# X11 constants
SHAPE_INPUT = 2
SHAPE_SET = 0
SHAPE_UNSORTED = 0

CTRL, SHIFT, LOCK, MOD1, MOD2, MOD4 = 1 << 2, 1 << 0, 1 << 1, 1 << 3, 1 << 4, 1 << 6

_lib: Optional[ctypes.CDLL] = None
_xext: Optional[ctypes.CDLL] = None
_display = None


def session_kind() -> str:
    """'x11', 'wayland' or 'unknown' - based on the environment, not on luck."""
    if os.environ.get("WAYLAND_DISPLAY"):
        return "wayland"
    if os.environ.get("DISPLAY"):
        return "x11"
    return "unknown"


def is_x11() -> bool:
    return session_kind() == "x11"


def _load() -> bool:
    """Load libX11/libXext once and open a private display connection."""
    global _lib, _xext, _display
    if _display is not None:
        return True
    if not is_x11():
        return False
    try:
        x11_name = ctypes.util.find_library("X11") or "libX11.so.6"
        xext_name = ctypes.util.find_library("Xext") or "libXext.so.6"
        _lib = ctypes.CDLL(x11_name)
        _xext = ctypes.CDLL(xext_name)
        _lib.XOpenDisplay.restype = ctypes.c_void_p
        _lib.XOpenDisplay.argtypes = [ctypes.c_char_p]
        _display = _lib.XOpenDisplay(None)
    except OSError:
        _lib = _xext = _display = None
        return False
    return bool(_display)


def have_compositor(screen: int = 0) -> bool:
    """True when a compositing manager owns _NET_WM_CM_S<n>.

    Without one, an ARGB window is drawn as an opaque black rectangle, so the
    caller falls back to a 1-bit shape mask instead.
    """
    if not _load():
        return True  # not X11: assume the platform composites (Wayland does)
    _lib.XInternAtom.restype = ctypes.c_ulong
    _lib.XInternAtom.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
    _lib.XGetSelectionOwner.restype = ctypes.c_ulong
    _lib.XGetSelectionOwner.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    atom = _lib.XInternAtom(ctypes.c_void_p(_display), f"_NET_WM_CM_S{screen}".encode(), 0)
    return bool(_lib.XGetSelectionOwner(ctypes.c_void_p(_display), ctypes.c_ulong(atom)))


def set_input_shape_empty(win_id: int, empty: bool) -> bool:
    """Click-through at the X level: set the ShapeInput region to nothing.

    Qt's Qt.WindowTransparentForInput does this too; calling XShape directly
    makes the behaviour explicit and lets us verify it.
    """
    if not _load() or not win_id:
        return False
    class XRectangle(ctypes.Structure):
        _fields_ = [
            ("x", ctypes.c_short),
            ("y", ctypes.c_short),
            ("width", ctypes.c_ushort),
            ("height", ctypes.c_ushort),
        ]

    _xext.XShapeCombineRectangles.restype = ctypes.c_int
    _xext.XShapeCombineRectangles.argtypes = [
        ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        ctypes.POINTER(XRectangle), ctypes.c_int, ctypes.c_int, ctypes.c_int,
    ]
    rects = (XRectangle * 1)()
    if not empty:
        # one huge rect == "accept input everywhere"
        rects[0].x, rects[0].y = 0, 0
        rects[0].width, rects[0].height = 32767, 32767
    _xext.XShapeCombineRectangles(
        ctypes.c_void_p(_display), ctypes.c_ulong(win_id), SHAPE_INPUT, 0, 0,
        rects, 0 if empty else 1, SHAPE_SET, SHAPE_UNSORTED,
    )
    _lib.XFlush(ctypes.c_void_p(_display))
    return True


def input_shape_is_empty(win_id: int) -> Optional[bool]:
    """Verify click-through. None = cannot tell (no X11 / no shape extension).

    Measured behaviour of XShapeGetRectangles on Xorg: a window whose input
    region was explicitly emptied returns a NULL pointer, while a window that
    never had an input shape returns a valid pointer with zero rectangles
    (meaning "the default, i.e. the whole window").
    """
    if not _load() or not win_id:
        return None

    class XRectangle(ctypes.Structure):
        _fields_ = [
            ("x", ctypes.c_short),
            ("y", ctypes.c_short),
            ("width", ctypes.c_ushort),
            ("height", ctypes.c_ushort),
        ]

    _xext.XShapeGetRectangles.restype = ctypes.POINTER(XRectangle)
    _xext.XShapeGetRectangles.argtypes = [
        ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int,
        ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int),
    ]
    count = ctypes.c_int(0)
    ordering = ctypes.c_int(0)
    ptr = _xext.XShapeGetRectangles(
        ctypes.c_void_p(_display), ctypes.c_ulong(win_id), SHAPE_INPUT,
        ctypes.byref(count), ctypes.byref(ordering),
    )
    if not ptr:
        return True  # explicitly emptied: mouse events pass through
    if count.value == 0:
        return False  # unshaped window: input region is the whole window
    if count.value == 1 and ptr[0].width >= 32767 and ptr[0].height >= 32767:
        return False  # our "accept input everywhere" rectangle
    return False


def apply_shape_mask(widget, pixmap: QPixmap) -> bool:
    """Compositor-less transparency: clip the window to the sprite silhouette."""
    mask = pixmap.mask()
    if mask.isNull():
        return False
    widget.setMask(QRegion(mask))
    return True


def set_sticky(win_id: int, on: bool = True) -> bool:
    """Ask the window manager to show this window on every desktop.

    Sets _NET_WM_DESKTOP to 0xFFFFFFFF (EWMH's "all desktops"), which bspwm, i3
    and most other EWMH window managers honour.  Without it the companion only
    exists on whichever desktop happened to launch it.
    """
    if not _load() or not win_id:
        return False
    _lib.XInternAtom.restype = ctypes.c_ulong
    _lib.XInternAtom.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
    _lib.XChangeProperty.restype = ctypes.c_int
    _lib.XChangeProperty.argtypes = [
        ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong,
        ctypes.c_int, ctypes.c_int, ctypes.c_char_p, ctypes.c_int,
    ]
    atom = _lib.XInternAtom(ctypes.c_void_p(_display), b"_NET_WM_DESKTOP", 0)
    cardinals = (ctypes.c_ulong * 1)()
    cardinals[0] = 0xFFFFFFFF if on else 0
    _lib.XChangeProperty(
        ctypes.c_void_p(_display), ctypes.c_ulong(win_id), ctypes.c_ulong(atom),
        ctypes.c_ulong(6),  # XA_CARDINAL
        32, 0,  # PropModeReplace
        ctypes.cast(cardinals, ctypes.c_char_p), 1,
    )
    _lib.XFlush(ctypes.c_void_p(_display))
    return True


def is_sticky(win_id: int) -> Optional[bool]:
    """Read _NET_WM_DESKTOP back: True when the window is on every desktop."""
    if not _load() or not win_id:
        return None
    _lib.XInternAtom.restype = ctypes.c_ulong
    _lib.XInternAtom.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
    _lib.XGetWindowProperty.restype = ctypes.c_int
    _lib.XGetWindowProperty.argtypes = [
        ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_long, ctypes.c_long,
        ctypes.c_int, ctypes.c_ulong, ctypes.POINTER(ctypes.c_ulong),
        ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_ulong),
        ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_void_p),
    ]
    atom = _lib.XInternAtom(ctypes.c_void_p(_display), b"_NET_WM_DESKTOP", 0)
    actual_type = ctypes.c_ulong(0)
    actual_format = ctypes.c_int(0)
    nitems = ctypes.c_ulong(0)
    bytes_after = ctypes.c_ulong(0)
    data = ctypes.c_void_p(0)
    status = _lib.XGetWindowProperty(
        ctypes.c_void_p(_display), ctypes.c_ulong(win_id), ctypes.c_ulong(atom),
        0, 1, 0, ctypes.c_ulong(6),  # XA_CARDINAL
        ctypes.byref(actual_type), ctypes.byref(actual_format),
        ctypes.byref(nitems), ctypes.byref(bytes_after), ctypes.byref(data),
    )
    if status != 0 or not data.value or nitems.value == 0:
        return False
    value = ctypes.cast(data, ctypes.POINTER(ctypes.c_ulong))[0]
    _lib.XFree(data)
    # Xlib widens format-32 cardinals into longs and sign-extends them, so
    # 0xFFFFFFFF comes back as 0xFFFFFFFFFFFFFFFF: compare the low 32 bits only
    return (value & 0xFFFFFFFF) == 0xFFFFFFFF


def screen_for_point(point: QPoint):
    screen = QApplication.screenAt(point)
    if screen is None:
        screen = QApplication.primaryScreen()
    return screen


def available_geometry(point: QPoint | None = None) -> QRect:
    screen = screen_for_point(point) if point is not None else QApplication.primaryScreen()
    if screen is None:  # pragma: no cover
        return QRect(0, 0, 1920, 1080)
    return screen.availableGeometry()


def clamp_to_screens(rect: QRect) -> QPoint:
    """Keep a window reachable: if it is fully off-screen, pull it back."""
    for screen in QApplication.screens():
        if screen.availableGeometry().intersects(rect):
            geo = screen.availableGeometry()
            x = min(max(rect.x(), geo.left()), max(geo.left(), geo.right() - rect.width()))
            y = min(max(rect.y(), geo.top()), max(geo.top(), geo.bottom() - rect.height()))
            return QPoint(x, y)
    geo = available_geometry()
    return QPoint(geo.right() - rect.width() - 40, geo.bottom() - rect.height() - 40)
