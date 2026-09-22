"""Optional autostart entries, per platform (never installed unless asked).

* Linux   - an XDG .desktop file in ~/.config/autostart
* Windows - a launcher script in the user's Startup folder
* macOS   - a LaunchAgent plist in ~/Library/LaunchAgents
"""

from __future__ import annotations

import os
import shlex
import sys
from pathlib import Path

from . import desktop


DESKTOP_TEMPLATE = """[Desktop Entry]
Type=Application
Name=DeepSeek
GenericName=AI Desktop Companion
Comment=A catboy companion that shows what DeepSeek is doing
Exec={exec_line}
Icon={icon}
Terminal=false
Categories=Utility;Development;
Keywords=ai;deepseek;catboy;companion;assistant;
StartupNotify=false
StartupWMClass=DeepSeek
X-GNOME-Autostart-enabled=true
"""

LAUNCHER_TEMPLATE = """#!/usr/bin/env bash
# DeepSeek companion launcher - works from a launcher, dmenu/rofi or a terminal.
#   deepseek            start him (detached; closing the terminal leaves him up)
#   deepseek --toggle   show/hide the running companion
#   deepseek --help     options
set -euo pipefail
PROJECT="{project}"
cd "$PROJECT"
if [ $# -gt 0 ]; then
  exec ./run.sh "$@"
fi
if command -v setsid >/dev/null 2>&1; then
  setsid nohup ./run.sh >/dev/null 2>&1 < /dev/null &
else
  nohup ./run.sh >/dev/null 2>&1 < /dev/null &
fi
"""


def autostart_path() -> Path:
    if desktop.WINDOWS:
        appdata = os.environ.get("APPDATA") or "~/AppData/Roaming"
        return (Path(appdata).expanduser() / "Microsoft" / "Windows" / "Start Menu"
                / "Programs" / "Startup" / "DeepSeek.cmd")
    if desktop.MACOS:
        return Path("~/Library/LaunchAgents/com.deepseek.companion.plist").expanduser()
    base = os.environ.get("XDG_CONFIG_HOME") or "~/.config"
    return Path(base).expanduser() / "autostart" / "deepseek.desktop"


def applications_path() -> Path:
    if desktop.WINDOWS:
        appdata = os.environ.get("APPDATA") or "~/AppData/Roaming"
        return (Path(appdata).expanduser() / "Microsoft" / "Windows" / "Start Menu"
                / "Programs" / "DeepSeek.lnk")
    if desktop.MACOS:
        return Path("~/Applications/DeepSeek.command").expanduser()
    base = os.environ.get("XDG_DATA_HOME") or "~/.local/share"
    return Path(base).expanduser() / "applications" / "deepseek.desktop"


def launcher_path() -> Path:
    """The `deepseek` command (or deepseek.cmd on Windows)."""
    base = os.environ.get("XDG_BIN_HOME")
    directory = Path(base).expanduser() if base else desktop.launcher_dir()
    return directory / ("deepseek.cmd" if desktop.WINDOWS else "deepseek")


def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent.parent


LAUNCHER_TEMPLATE_WINDOWS = """@echo off
rem DeepSeek companion launcher (Windows)
rem   deepseek            start him detached (no console window stays open)
rem   deepseek --toggle   show/hide the running companion
setlocal
set PROJECT={project}
if not "%~1"=="" (
  "%PYTHON%" -m dscompanion %*
  goto :eof
)
start "" /b "%PYTHON%" -m dscompanion
"""

PLIST_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.deepseek.companion</string>
  <key>ProgramArguments</key>
  <array>
    <string>{python}</string>
    <string>-m</string>
    <string>dscompanion</string>
    {extra}
  </array>
  <key>WorkingDirectory</key><string>{project}</string>
  <key>RunAtLoad</key><true/>
  <key>ProcessType</key><string>Interactive</string>
</dict>
</plist>
"""


def exec_line() -> str:
    """A command that works from a bare WM session (no shell profile)."""
    root = _project_root()
    launcher = root / "run.sh"
    if launcher.exists():
        return shlex.quote(str(launcher))
    python = sys.executable or "python3"
    return f"{shlex.quote(python)} -m dscompanion"


def install(*, also_menu_entry: bool = True, minimized: bool = True) -> Path:
    """Write the autostart entry for this platform."""
    if desktop.WINDOWS:
        return _install_windows(minimized)
    if desktop.MACOS:
        return _install_macos(minimized)
    icon = _project_root() / "assets" / "character" / "listening.png"
    entry = DESKTOP_TEMPLATE.format(
        exec_line=f"{exec_line()} --start-hidden" if minimized else exec_line(),
        icon=str(icon) if icon.exists() else "applications-utilities",
    )
    target = autostart_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(entry, encoding="utf-8")
    if also_menu_entry:
        menu = applications_path()
        menu.parent.mkdir(parents=True, exist_ok=True)
        menu.write_text(
            DESKTOP_TEMPLATE.format(
                exec_line=exec_line(),
                icon=str(icon) if icon.exists() else "applications-utilities",
            ).replace("X-GNOME-Autostart-enabled=true\n", ""),
            encoding="utf-8",
        )
    return target


def install_launcher() -> Path:
    """Write the `deepseek` command so the app starts like any other program."""
    target = launcher_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    if desktop.WINDOWS:
        target.write_text(
            LAUNCHER_TEMPLATE_WINDOWS.format(
                project=_project_root(), **{"PYTHON": sys.executable or "python"}
            ),
            encoding="utf-8",
        )
        return target
    if desktop.MACOS:
        target.write_text(
            "#!/bin/bash\n"
            f'cd "{_project_root()}"\n'
            'if [ $# -gt 0 ]; then exec ./run.sh "$@"; fi\n'
            f'nohup "{sys.executable or "python3"}" -m dscompanion '
            '>/dev/null 2>&1 &\n',
            encoding="utf-8",
        )
        target.chmod(0o755)
        return target
    target.write_text(
        LAUNCHER_TEMPLATE.format(project=_project_root()), encoding="utf-8"
    )
    target.chmod(0o755)
    return target


def uninstall() -> None:
    for path in (autostart_path(), applications_path()):
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def uninstall_launcher() -> None:
    try:
        launcher_path().unlink()
    except FileNotFoundError:
        pass


def is_installed() -> bool:
    return autostart_path().exists()


def _install_windows(minimized: bool) -> Path:
    """A .cmd in the Startup folder (no console window, no shortcut plumbing)."""
    launcher = install_launcher()
    target = autostart_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        "@echo off\r\n"
        "rem DeepSeek companion - start hidden with the session\r\n"
        f'call "{launcher}"' + (" --start-hidden\r\n" if minimized else "\r\n"),
        encoding="utf-8",
    )
    return target


def _install_macos(minimized: bool) -> Path:
    target = autostart_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    extra = "    <string>--start-hidden</string>" if minimized else ""
    target.write_text(
        PLIST_TEMPLATE.format(
            python=sys.executable or "/usr/bin/python3",
            project=_project_root(),
            extra=extra,
        ),
        encoding="utf-8",
    )
    return target


def start_hidden_supported() -> bool:
    """--start-hidden works everywhere; only the launch mechanism differs."""
    return True
