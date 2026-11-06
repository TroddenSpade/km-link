#!/usr/bin/env bash
# Double-click-able launcher: runs the Alt+S switcher with the rights it needs,
# in a terminal, so the status messages stay visible.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="$(command -v python3 || echo /usr/bin/python3)"
exec sudo -- "$PY" "$HERE/km_link_switch_linux.py" "$@"
