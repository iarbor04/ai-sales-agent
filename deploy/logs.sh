#!/usr/bin/env bash
# Последние строки лога. Без аргумента — 50 строк.
# Не используйте -f: агент не умеет читать бесконечный поток.
set -uo pipefail
journalctl -u ai-sales -n "${1:-50}" --no-pager
