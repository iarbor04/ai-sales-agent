#!/bin/sh
set -eu
cd "$(dirname "$0")"
PYTHON_BIN="${PYTHON_BIN:-python3}"
"$PYTHON_BIN" -c 'import sys; assert sys.version_info >= (3, 10), "Python 3.10+ required"'
export PORTABLE_MODE=1
exec "$PYTHON_BIN" run.py --portable
