#!/usr/bin/env bash
set -euo pipefail

CONTROL_HOME="${MINING_CONTROL_HOME:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
cd "$CONTROL_HOME"

if [ -f "$CONTROL_HOME/.env" ]; then
  set -a
  # shellcheck disable=SC1091
  . "$CONTROL_HOME/.env"
  set +a
fi

exec /usr/bin/python3 "$CONTROL_HOME/control.py"
