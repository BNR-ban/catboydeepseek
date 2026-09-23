"""One place where the desktop-specific behaviour lives.

The companion is a normal Qt application, so most of it is already portable.
What is *not* portable is the small set of things that make him feel native:

======================  =====================  ==================  ==============
feature                 Linux (X11/Wayland)    Windows             macOS
======================  =====================  ==================  ==============
transparent window      Qt ARGB (+compositor)  Qt ARGB             Qt ARGB
no taskbar entry        Qt.Tool                Qt.Tool             Qt.Tool
always on top           Qt hint / EWMH         Qt hint             Qt hint
click-through           XShape input region    Qt flag (+WS_EX)    Qt flag
show on every desktop   _NET_WM_DESKTOP        -- (not offered)    -- (see note)
global hotkeys          XGrabKey               RegisterHotKey      pynput (optional)
autostart               XDG .desktop           Startup folder      LaunchAgent
======================  =====================  ==================  ==============

Everything degrades gracefully: when a feature is unavailable the app says so in
the status line or on the console and keeps working.

Note on "every desktop": Windows has no equivalent concept for a normal window,
and macOS needs `NSWindowCollectionBehaviorCanJoinAllSpaces`, which requires
PyObjC - so both simply keep him always-on-top instead.
"""

from __future__ import annotations

import ctypes
import os
import sys
from pathlib import Path

from PyQt5.QtCore import QPoint, QRect, Qt
from PyQt5.QtWidgets import QApplication

from . import x11

WINDOWS = sys.platform.startswith("win")
MACOS = sys.platform == "darwin"
LINUX = not WINDOWS and not MACOS


# --------------------------------------------------------------------- basics
def os_name() -> str:
    return "windows" if WINDOWS else "macos" if MACOS else "linux"


def session_name() -> str:
    """linux-x11, linux-wayland, windows, macos or unknown."""
    if WINDOWS:
        return "windows"
    if MACOS:
        return "macos"
    kind = x11.session_kind()
    return f"linux-{kind}" if kind != "unknown" else "unknown"


def supports_sticky() -> bool:
    """Only X11 window managers understand "show on every desktop"."""
    return x11.is_x11()


def supports_global_hotkeys() -> bool:
    if WINDOWS or x11.is_x11():
        return True
    if MACOS:
        import importlib.util

        return importlib.util.find_spec("pynput") is not None
    return False


# ------------------------------------------------------------------ windowing
def have_compositor() -> bool:
    """Whether per-pixel alpha will actually be composited.

    Windows and macOS always composite; on Linux it depends on picom/compton.
    """
    if WINDOWS or MACOS:
        return True
    return x11.have_compositor()


def supports_input_handle() -> bool:
    """Whether a *partial* click-through (a grabbable handle) is possible."""
    return x11.is_x11()


def set_click_through(widget, on: bool, handle: tuple[int, int, int, int] | None = None) -> None:
    """Let mouse events fall through the window (best effort per platform).

    With `handle` on X11 only that rectangle accepts the mouse, so a gaming
    companion never eats clicks meant for the game while still being draggable.
    Platforms without partial input regions get all-or-nothing click-through
    (and the app tells the user how to get him back).
    """
    partial = bool(on and handle and supports_input_handle())
    widget.setWindowFlag(Qt.WindowTransparentForInput, on and not partial)
    widget.show()
    if x11.is_x11():
        if partial:
            x11.set_input_shape_rect(int(widget.winId()), handle)
        else:
            x11.set_input_shape_empty(int(widget.winId()), on)
    elif WINDOWS:
        _windows_click_through(int(widget.winId()), on)


def click_through_supported() -> bool:
    return x11.is_x11() or WINDOWS or MACOS


def set_sticky(widget, on: bool = True) -> bool:
    """Keep the window on every desktop, where the OS allows it."""
    if not supports_sticky():
        return False
    x11.set_sticky(int(widget.winId()), on)
    return True


def is_sticky(widget) -> bool | None:
    if not supports_sticky():
        return None
    return x11.is_sticky(int(widget.winId()))


def apply_shape_mask(widget, pixmap) -> bool:
    """Compositor-less Linux: clip the window to the sprite silhouette."""
    if not x11.is_x11():
        return False
    mask = pixmap.mask()
    if mask.isNull():
        return False
    from PyQt5.QtGui import QRegion

    widget.setMask(QRegion(mask))
    return True


def _windows_click_through(win_id: int, on: bool) -> bool:
    """WS_EX_TRANSPARENT | WS_EX_LAYERED on top of Qt's own flag.

    Qt's WindowTransparentForInput usually suffices, but some Windows versions
    ignore it for layered windows, so the extended style is applied as well.
    """
    if not WINDOWS:
        return False
    try:
        user32 = ctypes.windll.user32
        GWL_EXSTYLE = -20
        WS_EX_TRANSPARENT = 0x00000020
        WS_EX_LAYERED = 0x00080000
        style = user32.GetWindowLongW(win_id, GWL_EXSTYLE)
        if on:
            style |= WS_EX_TRANSPARENT | WS_EX_LAYERED
        else:
            style &= ~WS_EX_TRANSPARENT
        user32.SetWindowLongW(win_id, GWL_EXSTYLE, style)
        return True
    except Exception:
        return False


# ------------------------------------------------------------------- geometry
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


# ------------------------------------------------------------------ platform bits
def data_dir() -> Path:
    """Where per-user app data belongs (config, launchers, autostart files)."""
    if WINDOWS:
        base = os.environ.get("APPDATA") or "~/AppData/Roaming"
        return Path(base).expanduser() / "DeepSeek"
    if MACOS:
        return Path("~/Library/Application Support/DeepSeek").expanduser()
    base = os.environ.get("XDG_CONFIG_HOME") or "~/.config"
    return Path(base).expanduser() / "deepseek-companion"


def launcher_dir() -> Path:
    """Where a `deepseek` command should be written."""
    if WINDOWS:
        base = os.environ.get("LOCALAPPDATA") or "~/AppData/Local"
        return Path(base).expanduser() / "DeepSeek" / "bin"
    return Path("~/.local/bin").expanduser()
