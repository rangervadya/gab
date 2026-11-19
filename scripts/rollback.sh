#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "$0")/_common.sh"
require_clean_revision
load_compose
for key in BACKUP_ARCHIVE AGE_IDENTITY ROLLBACK_COMPONENT; do need "$key"; done
case "$ROLLBACK_COMPONENT" in
  crm) need CRM_SERVICE; need CRM_HEALTH_URL; key=CRM_SERVICE; service=$CRM_SERVICE; health=$CRM_HEALTH_URL ;;
  ocr) need OCR_SERVICE; need OCR_HEALTH_URL; key=OCR_SERVICE; service=$OCR_SERVICE; health=$OCR_HEALTH_URL ;;
  *) fail 'ROLLBACK_COMPONENT должен быть crm или ocr' ;;
esac
bash "$ROOT/scripts/backup.sh"
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
age -d -i "$AGE_IDENTITY" "$BACKUP_ARCHIVE" | tar -xf - -C "$work" "./$key.image" "./$key.image.tar"
docker image load -i "$work/$key.image.tar" >/dev/null
image=$(cat "$work/$key.image")
[[ "$image" =~ ^sha256:[0-9a-f]{64}$ ]] || fail 'Некорректный image ID'
service_exists "$service"
# Compose accepts JSON as YAML; names cannot inject an override document.
python3 - "$service" "$image" > "$work/override.json" <<'PY'
import json, sys
print(json.dumps({'services':{sys.argv[1]:{'image':sys.argv[2]}}}))
PY
"${COMPOSE[@]}" -f "$work/override.json" up -d --no-deps --no-build "$service"
check_health "$health"
echo "Образ $service восстановлен. База и документы не изменялись."
