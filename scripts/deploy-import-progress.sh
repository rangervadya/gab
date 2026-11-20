#!/usr/bin/env bash
set -euo pipefail
# Coordinated CRM + OCR 0.4.8 + GAB 02 update; never sends mail.
exec bash "$(dirname "$0")/deploy-all.sh" --rebuild-ocr "$@"
