#!/bin/bash
# Launch the companion from a source checkout on macOS (double-clickable).
cd "$(dirname "$0")"
export PYTHONPATH="$(pwd)/src${PYTHONPATH:+:$PYTHONPATH}"
exec python3 -m dscompanion "$@"
