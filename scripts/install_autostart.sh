#!/usr/bin/env bash
# Optional autostart entry - never installed unless you run this script.
#
#   scripts/install_autostart.sh install     # start with the session (hidden)
#   scripts/install_autostart.sh uninstall   # remove it again
#   scripts/install_autostart.sh status      # what is installed right now
#
# The companion starts hidden and waits for the hotkey (ctrl+shift+space) so it
# never pops a window into your session at login.  The same entry is also
# written to ~/.local/share/applications so it shows up in dmenu/rofi launchers.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
action="${1:-status}"

run_py() {
  PYTHONPATH="$here/src${PYTHONPATH:+:$PYTHONPATH}" python3 - "$@"
}

case "$action" in
  install)
    run_py <<'PY'
import sys
from dscompanion import autostart

path = autostart.install(also_menu_entry=True, minimized=True)
print(f"autostart entry: {path}")
print(f"launcher entry : {autostart.applications_path()}")
print("the companion will start hidden; press ctrl+shift+space to show it")
PY
    ;;
  uninstall)
    run_py <<'PY'
from dscompanion import autostart

autostart.uninstall()
print("autostart entry removed")
PY
    ;;
  status)
    run_py <<'PY'
from dscompanion import autostart

if autostart.is_installed():
    print(f"installed: {autostart.autostart_path()}")
else:
    print("not installed")
print(f"launch command: {autostart.exec_line()}")
PY
    ;;
  *)
    echo "usage: $0 {install|uninstall|status}" >&2
    exit 2
    ;;
esac
