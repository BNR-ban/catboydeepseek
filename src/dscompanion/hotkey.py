"""Global hotkeys, per platform.

* **Linux / X11** - XGrabKey on a private X connection, driven by a blocking
  select() so an idle companion does zero periodic work.
* **Windows** - RegisterHotKey plus a native event filter for WM_HOTKEY.
* **macOS** - whichever of pynput's global listeners is importable; if it is not
  installed the app says so and relies on its window-local shortcuts.
* **Wayland** - no unprivileged global shortcuts exist; the compositor has to
  bind them, so the app reports that and keeps working.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import os
import select
import threading
from typing import Callable, Optional

from PyQt5.QtCore import QObject, pyqtSignal

from . import x11

X11_CTRL, X11_SHIFT, X11_LOCK, X11_MOD1, X11_MOD2, X11_MOD4 = (
    1 << 2, 1 << 0, 1 << 1, 1 << 3, 1 << 4, 1 << 6,
)

_MODIFIER_NAMES = {
    "ctrl": X11_CTRL, "control": X11_CTRL,
    "shift": X11_SHIFT,
    "alt": X11_MOD1, "mod1": X11_MOD1,
    "super": X11_MOD4, "meta": X11_MOD4, "win": X11_MOD4, "mod4": X11_MOD4,
}

_KEYSYM_ALIASES = {
    "space": "space", "enter": "Return", "return": "Return", "esc": "Escape",
    "escape": "Escape", "tab": "Tab", "backspace": "BackSpace",
    "slash": "slash", "comma": "comma", "period": "period",
    "grave": "grave", "minus": "minus", "equal": "equal",
}

# Key names whose X keysym name is not simply the upper-case letter/digit.
_LOCK_COMBOS = (0, X11_LOCK, X11_MOD2, X11_LOCK | X11_MOD2)


def parse_hotkey(spec: str) -> Optional[tuple[int, str]]:
    """'ctrl+shift+space' -> (modifier mask, keysym name). None if unusable."""
    if not spec or not spec.strip():
        return None
    parts = [p.strip().lower() for p in spec.replace("-", "+").split("+") if p.strip()]
    if not parts:
        return None
    mask = 0
    key: Optional[str] = None
    for part in parts:
        if part in _MODIFIER_NAMES:
            mask |= _MODIFIER_NAMES[part]
        else:
            key = part
    if key is None:
        return None
    if key in _KEYSYM_ALIASES:
        return mask, _KEYSYM_ALIASES[key]
    if len(key) == 1:
        return mask, key
    if key.startswith("f") and key[1:].isdigit():
        return mask, key.upper()
    return mask, key.capitalize()


class _X11KeyListener:
    """Blocking XGrabKey listener running on its own thread."""

    def __init__(self, bindings: dict[str, tuple[int, str]], on_fire: Callable[[str], None]):
        self._bindings = bindings
        self._on_fire = on_fire
        self._thread: Optional[threading.Thread] = None
        self._wake_r: Optional[int] = None
        self._wake_w: Optional[int] = None
        self._lib = None
        self._xext = None
        self._display = None
        self._grabbed: list[tuple[int, int]] = []
        self.error: Optional[str] = None

    # ------------------------------------------------------------------ setup
    def start(self) -> bool:
        try:
            self._lib = ctypes.CDLL(ctypes.util.find_library("X11") or "libX11.so.6")
        except OSError as exc:
            self.error = f"libX11 unavailable: {exc}"
            return False

        lib = self._lib
        lib.XOpenDisplay.restype = ctypes.c_void_p
        lib.XOpenDisplay.argtypes = [ctypes.c_char_p]
        self._display = lib.XOpenDisplay(None)
        if not self._display:
            self.error = "cannot open X display"
            return False

        lib.XDefaultRootWindow.restype = ctypes.c_ulong
        lib.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
        lib.XStringToKeysym.restype = ctypes.c_ulong
        lib.XStringToKeysym.argtypes = [ctypes.c_char_p]
        lib.XKeysymToKeycode.restype = ctypes.c_ubyte
        lib.XKeysymToKeycode.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        lib.XGrabKey.argtypes = [
            ctypes.c_void_p, ctypes.c_int, ctypes.c_uint, ctypes.c_ulong,
            ctypes.c_int, ctypes.c_int, ctypes.c_int,
        ]
        lib.XNextEvent.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        lib.XSelectInput.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_long]
        lib.XConnectionNumber.restype = ctypes.c_int
        lib.XConnectionNumber.argtypes = [ctypes.c_void_p]

        root = lib.XDefaultRootWindow(ctypes.c_void_p(self._display))
        self._root = root

        # Swallow BadAccess instead of letting the default handler abort us -
        # another client may already own the combination - but remember that it
        # happened, so "the hotkey silently does nothing" cannot occur.
        handler_type = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)
        self._grab_errors = 0

        def on_x_error(_display, event_ptr):
            self._grab_errors += 1
            return 0

        self._error_handler = handler_type(on_x_error)
        lib.XSetErrorHandler.argtypes = [handler_type]
        lib.XSetErrorHandler(self._error_handler)

        self._keysym_to_action: dict[tuple[int, int], str] = {}
        for name, parsed in self._bindings.items():
            if not parsed:
                continue
            mask, keysym_name = parsed
            keysym = lib.XStringToKeysym(keysym_name.encode())
            if not keysym:
                self.error = f"unknown key in hotkey {name!r}: {keysym_name}"
                continue
            keycode = lib.XKeysymToKeycode(ctypes.c_void_p(self._display), ctypes.c_ulong(keysym))
            if not keycode:
                self.error = f"no keycode for {keysym_name}"
                continue
            for extra in _LOCK_COMBOS:
                # owner_events=False so the combination always comes back to us,
                # even when one of the companion's own windows holds focus.
                lib.XGrabKey(
                    ctypes.c_void_p(self._display), keycode, mask | extra, root, 0, 1, 1
                )
                self._grabbed.append((keycode, mask | extra))
                self._keysym_to_action[(keycode, mask | extra)] = name

        lib.XSync(ctypes.c_void_p(self._display), 0)

        if self._grab_errors:
            # BadAccess: somebody else owns that combination (usually another
            # companion instance, or the window manager)
            self.error = "the key combination is already grabbed by another program"
            return False
        if not self._keysym_to_action:
            self.error = self.error or "no hotkey could be grabbed"
            return False

        self._wake_r, self._wake_w = os.pipe()
        self._thread = threading.Thread(target=self._loop, name="hotkey", daemon=True)
        self._thread.start()
        return True

    # ------------------------------------------------------------------- loop
    def _loop(self) -> None:
        lib = self._lib
        fd = lib.XConnectionNumber(ctypes.c_void_p(self._display))
        event = ctypes.create_string_buffer(192)
        while True:
            try:
                readable, _, _ = select.select([fd, self._wake_r], [], [])
            except (OSError, ValueError):
                return
            if self._wake_r in readable:
                return  # shutting down
            while lib.XPending(ctypes.c_void_p(self._display)):
                lib.XNextEvent(ctypes.c_void_p(self._display), ctypes.byref(event))
                etype = ctypes.cast(event, ctypes.POINTER(ctypes.c_int))[0]
                if etype != 2:  # KeyPress
                    continue
                # XKeyEvent layout (64-bit): type 0, serial 8, send_event 16,
                # display 24, window 32, root 40, subwindow 48, time 56,
                # x 64, y 68, x_root 72, y_root 76, state 80, keycode 84
                state = int.from_bytes(event.raw[80:84], "little")
                keycode = event.raw[84]
                action = self._keysym_to_action.get((keycode, state & ~X11_LOCK & ~X11_MOD2))
                if action is None:
                    action = self._keysym_to_action.get((keycode, state))
                if action:
                    self._on_fire(action)

    # ---------------------------------------------------------------- shutdown
    def stop(self) -> None:
        if self._wake_w is not None:
            try:
                os.write(self._wake_w, b"x")
            except OSError:
                pass
        if self._thread is not None:
            self._thread.join(timeout=0.5)
        if self._display and self._lib:
            try:
                for keycode, mask in self._grabbed:
                    self._lib.XUngrabKey(
                        ctypes.c_void_p(self._display), keycode, mask, self._root
                    )
                self._lib.XSync(ctypes.c_void_p(self._display), 0)
                self._lib.XCloseDisplay(ctypes.c_void_p(self._display))
            except Exception:  # pragma: no cover
                pass
        for fd in (self._wake_r, self._wake_w):
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass


class _WindowsKeyListener:
    """RegisterHotKey + WM_HOTKEY through a Qt native event filter.

    RegisterHotKey is asynchronous: the key press is delivered to the thread
    that registered it as WM_HOTKEY, so this installs a filter on the
    application and turns those messages back into named actions.
    """

    MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN, MOD_NOREPEAT = 0x0001, 0x0002, 0x0004, 0x0008, 0x4000
    WM_HOTKEY = 0x0312

    def __init__(self, bindings: dict[str, tuple[int, str]], on_fire):
        self._bindings = bindings
        self._on_fire = on_fire
        self._ids: dict[int, str] = {}
        self._filter = None
        self.error: str | None = None

    @staticmethod
    def _parse(spec: str) -> tuple[int, int] | None:
        """'ctrl+shift+space' -> (modifiers, virtual key code)."""
        parts = [p.strip().lower() for p in spec.replace("-", "+").split("+") if p.strip()]
        mods = 0
        key = None
        for part in parts:
            if part in ("ctrl", "control"):
                mods |= _WindowsKeyListener.MOD_CONTROL
            elif part == "shift":
                mods |= _WindowsKeyListener.MOD_SHIFT
            elif part in ("alt", "mod1"):
                mods |= _WindowsKeyListener.MOD_ALT
            elif part in ("super", "win", "meta", "mod4"):
                mods |= _WindowsKeyListener.MOD_WIN
            elif part != "norepeat":
                key = part
        if key is None:
            return None
        vk = None
        if len(key) == 1:
            vk = ord(key.upper())
        elif key.startswith("f") and key[1:].isdigit():
            vk = 0x70 + int(key[1:]) - 1          # VK_F1
        else:
            named = {"space": 0x20, "enter": 0x0D, "return": 0x0D, "tab": 0x09,
                     "escape": 0x1B, "esc": 0x1B, "backspace": 0x08}
            vk = named.get(key)
        return (mods, vk) if vk else None

    def start(self) -> bool:
        import ctypes

        from PyQt5.QtCore import QAbstractNativeEventFilter

        try:
            user32 = ctypes.windll.user32
        except Exception as exc:                       # pragma: no cover
            self.error = f"no user32: {exc}"
            return False

        for index, (action, spec) in enumerate(self._bindings.items(), start=1):
            parsed = self._parse(spec) if spec else None
            if not parsed:
                continue
            mods, vk = parsed
            if user32.RegisterHotKey(None, index, mods | self.MOD_NOREPEAT, vk):
                self._ids[index] = action
            else:
                self.error = f"another program already owns {spec}"
        if not self._ids:
            return False

        ids = dict(self._ids)
        fire = self._on_fire

        class _Filter(QAbstractNativeEventFilter):
            def nativeEventFilter(self, _event_type, message):  # noqa: N802
                try:
                    msg = ctypes.cast(int(message), ctypes.POINTER(ctypes.c_void_p))
                    message_id, wparam = msg[1], msg[2]
                    if message_id == _WindowsKeyListener.WM_HOTKEY:
                        action = ids.get(int(wparam or 0))
                        if action:
                            fire(action)
                except Exception:
                    pass
                return False, 0

        from PyQt5.QtWidgets import QApplication

        self._filter = _Filter()
        QApplication.instance().installNativeEventFilter(self._filter)
        return True

    def stop(self) -> None:
        if not self._ids:
            return
        try:
            import ctypes

            user32 = ctypes.windll.user32
            for index in self._ids:
                user32.UnregisterHotKey(None, index)
        except Exception:
            pass
        try:
            from PyQt5.QtWidgets import QApplication

            if self._filter is not None:
                QApplication.instance().removeNativeEventFilter(self._filter)
        except Exception:
            pass
        self._ids.clear()


class _MacKeyListener:
    """macOS global hotkeys through pynput, when it is installed."""

    def __init__(self, bindings: dict[str, str], on_fire):
        self._bindings = bindings
        self._on_fire = on_fire
        self._listener = None
        self.error: str | None = None

    def start(self) -> bool:
        try:
            from pynput import keyboard
        except Exception:
            self.error = ("macOS global hotkeys need the optional 'pynput' package "
                          "(pip install pynput); window shortcuts still work")
            return False
        mapping: dict[str, str] = {}
        for action, spec in self._bindings.items():
            if not spec:
                continue
            pretty = "+".join(p.strip().lower() for p in spec.split("+"))
            mapping[pretty] = action

        def on_activate(spec_text: str) -> None:
            self._on_fire(mapping[spec_text])

        try:
            hotkeys = {spec: (lambda s=spec: on_activate(s)) for spec in mapping}
            self._listener = keyboard.GlobalHotKeys(hotkeys)
            self._listener.start()
            return True
        except Exception as exc:
            self.error = f"pynput could not register the hotkeys: {exc}"
            return False

    def stop(self) -> None:
        if self._listener is not None:
            try:
                self._listener.stop()
            except Exception:
                pass


class HotkeyManager(QObject):
    """Qt-facing wrapper: emits `triggered(action_name)` on the GUI thread."""

    triggered = pyqtSignal(str)

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self._listener: Optional[_X11KeyListener] = None
        self.available = False
        self.error: Optional[str] = None
        self.active: dict[str, str] = {}

    def register(self, bindings: dict[str, str]) -> bool:
        wanted = {action: spec for action, spec in bindings.items() if spec}
        if not wanted:
            self.error = "no hotkeys configured"
            return False

        from . import desktop

        if desktop.WINDOWS:
            listener = _WindowsKeyListener(wanted, self.triggered.emit)
            self.active = dict(wanted)
        elif desktop.MACOS:
            listener = _MacKeyListener(wanted, self.triggered.emit)
            self.active = dict(wanted)
        elif x11.is_x11():
            parsed = {}
            for action, spec in wanted.items():
                parsed[action] = parse_hotkey(spec)
                if parsed[action]:
                    self.active[action] = spec
            if not parsed:
                self.error = "no usable hotkeys configured"
                return False
            listener = _X11KeyListener(parsed, self.triggered.emit)
        else:
            self.error = ("global hotkeys need X11; on Wayland bind a compositor "
                          "shortcut to 'deepseek --toggle' instead")
            return False

        if not listener.start():
            self.error = listener.error
            return False
        self._listener = listener
        self.error = getattr(listener, "error", None)
        self.available = True
        return True

    def stop(self) -> None:
        if self._listener is not None:
            self._listener.stop()
            self._listener = None
        self.available = False
