#!/usr/bin/env python3
"""Install the companion as a normal app - on Linux, Windows or macOS.

    python3 scripts/install_app.py                # launcher command + menu entry
    python3 scripts/install_app.py --autostart    # ...and start with the session
    python3 scripts/install_app.py --uninstall    # remove them again

What gets written depends on the platform:

    Linux    ~/.local/bin/deepseek, an XDG .desktop entry (menu + autostart)
    Windows  %LOCALAPPDATA%\\DeepSeek\\bin\\deepseek.cmd and a Start-menu/Startup entry
    macOS    ~/.local/bin/deepseek and a .command launcher (plus a LaunchAgent
             for autostart)
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from dscompanion import autostart, desktop  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--autostart", action="store_true",
                        help="also start him when you log in")
    parser.add_argument("--uninstall", action="store_true",
                        help="remove the launcher and autostart entries")
    args = parser.parse_args()

    print(f"platform: {desktop.session_name()}")

    if args.uninstall:
        autostart.uninstall()
        autostart.uninstall_launcher()
        print("removed the launcher, menu entry and autostart entry")
        return 0

    launcher = autostart.install_launcher()
    print(f"command    : {launcher}")
    menu = autostart.install(also_menu_entry=True, minimized=False)
    print(f"menu entry : {menu if menu != launcher else '(same as command)'}")
    if args.autostart:
        print(f"autostart  : {autostart.install(minimized=True)}")
    else:
        print("autostart  : not installed (pass --autostart to enable)")

    if desktop.LINUX:
        path_hint = str(desktop.launcher_dir())
        if path_hint not in (__import__("os").environ.get("PATH") or ""):
            print(f"note: add {path_hint} to your PATH to use the 'deepseek' command")
    elif desktop.WINDOWS:
        print("note: the Start-menu entry is named DeepSeek; the command is deepseek.cmd")
    print("launch it with: deepseek")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
