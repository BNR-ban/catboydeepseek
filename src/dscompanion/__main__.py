"""Allow `python3 -m dscompanion`."""

import sys

from .app import configure_qt_environment, run_app

if __name__ == "__main__":
    configure_qt_environment()  # before Qt is imported and a display opens
    raise SystemExit(run_app(sys.argv[1:]))
