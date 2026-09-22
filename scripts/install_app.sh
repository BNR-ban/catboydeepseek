#!/usr/bin/env bash
# Install the companion as a normal desktop app.
#
#   scripts/install_app.sh              # `deepseek` command + launcher entry
#   scripts/install_app.sh --autostart  # ...and start it with the session
#   scripts/install_app.sh --uninstall  # remove both again
#
# Nothing is copied anywhere: the entries point at this checkout, so editing the
# source and restarting is enough to see changes.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
action="install"
autostart=0
for arg in "$@"; do
  case "$arg" in
    --autostart) autostart=1 ;;
    --uninstall) action="uninstall" ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

run_py() {
  PYTHONPATH="$here/src${PYTHONPATH:+:$PYTHONPATH}" python3 - "$@"
}

if [ "$action" = "uninstall" ]; then
  run_py <<'PY'
from dscompanion import autostart

autostart.uninstall()
autostart.uninstall_launcher()
print("removed the deepseek command, menu entry and autostart entry")
PY
  exit 0
fi

run_py <<PY
from dscompanion import autostart

launcher = autostart.install_launcher()
print(f"command      : {launcher}")
menu = autostart.install(also_menu_entry=True, minimized=False)
_ = menu
print(f"menu entry   : {autostart.applications_path()}")
if $autostart:
    print(f"autostart    : {autostart.install(minimized=True)}")
else:
    print("autostart    : not installed (pass --autostart to enable)")
PY

case ":$PATH:" in
  *":$HOME/.local/bin:"*) ;;
  *) echo "note: add \$HOME/.local/bin to your PATH to use the 'deepseek' command" ;;
esac
echo "launch it with:  deepseek"
