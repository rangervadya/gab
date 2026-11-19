#!/usr/bin/env bash
set -euo pipefail
component=${1:?Use crm or ocr}
shift
case "$component" in crm|ocr) exec bash "$(dirname "$0")/deploy-$component.sh" "$@" ;; *) exit 2 ;; esac
