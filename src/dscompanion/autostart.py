"""Optional XDG autostart entry (never installed unless the user asks)."""

from __future__ import annotations

import os
import shlex
import sys
from pathlib import Path


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
    base = os.environ.get("XDG_CONFIG_HOME") or "~/.config"
    return Path(base).expanduser() / "autostart" / "deepseek.desktop"


def applications_path() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or "~/.local/share"
    return Path(base).expanduser() / "applications" / "deepseek.desktop"


def launcher_path() -> Path:
    """~/.local/bin/deepseek - the command that launches the app."""
    base = os.environ.get("XDG_BIN_HOME") or "~/.local/bin"
    return Path(base).expanduser() / "deepseek"


def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent.parent


def exec_line() -> str:
    """A command that works from a bare WM session (no shell profile)."""
    root = _project_root()
    launcher = root / "run.sh"
    if launcher.exists():
        return shlex.quote(str(launcher))
    python = sys.executable or "python3"
    return f"{shlex.quote(python)} -m dscompanion"


def install(*, also_menu_entry: bool = True, minimized: bool = True) -> Path:
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

