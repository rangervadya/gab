#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "$0")/_common.sh"
require_clean_revision
need N8N_URL
need N8N_API_KEY
bash "$ROOT/scripts/backup.sh"
python3 "$ROOT/scripts/workflow_api.py"
