#!/bin/sh
set -eu
cd "$(dirname "$0")"
PYTHON_BIN="${PYTHON_BIN:-python3}"
export PORTABLE_MODE=1
"$PYTHON_BIN" -c 'from app.portable import bootstrap; bootstrap(); from app.web.main import app; assert app'
"$PYTHON_BIN" -m app.cli check-db
if [ -n "${PORT:-}" ]; then
  "$PYTHON_BIN" -c "import json,urllib.request; d=json.load(urllib.request.urlopen('http://127.0.0.1:${PORT}/ready')); assert d['ok']"
fi
echo "PORTABLE_CHECK: PASS"
