#!/usr/bin/env bash
# Launch the companion from a source checkout (no installation needed).
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export PYTHONPATH="$here/src${PYTHONPATH:+:$PYTHONPATH}"
# Everything is painted with Qt's raster engine, so the xcb GL integration (and
# with it Mesa/LLVM, ~50 MB resident) is switched off deliberately.
export QT_XCB_GL_INTEGRATION="${QT_XCB_GL_INTEGRATION:-none}"
export QT_QPA_PLATFORMTHEME="${QT_QPA_PLATFORMTHEME:-}"
exec python3 -m dscompanion "$@"
