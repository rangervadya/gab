#!/usr/bin/env bash
set -euo pipefail
umask 077
SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
export PYTHONDONTWRITEBYTECODE=1
exec python3 "$SCRIPT_DIR/deploy.py" workflow "$@"
