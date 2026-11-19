#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "$0")/_common.sh"
kind=${1:?crm or ocr}
require_clean_revision
load_compose
case "$kind" in
  crm) need CRM_SERVICE; need CRM_HEALTH_URL; service=$CRM_SERVICE; health=$CRM_HEALTH_URL ;;
  ocr) need OCR_SERVICE; need OCR_HEALTH_URL; service=$OCR_SERVICE; health=$OCR_HEALTH_URL ;;
  *) fail 'Неизвестный компонент' ;;
esac
service_exists "$service"
python3 -m compileall -q "$ROOT/app" "$ROOT/services/gab-ocr"
for file in "$ROOT"/app/static/*.js; do node --check "$file"; done
bash "$ROOT/scripts/backup.sh"
if [[ "$kind" == crm ]]; then
  need POSTGRES_SERVICE; need POSTGRES_USER
  pg_id=$(container_id "$POSTGRES_SERVICE")
  docker exec -i "$pg_id" psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d gab_crm < "$ROOT/migrations/20261119_import_editor.sql"
fi
"${COMPOSE[@]}" build "$service"
"${COMPOSE[@]}" up -d --no-deps --no-build "$service"
check_health "$health"
echo "Обновлён только $service; health проверен. Commit: $EXPECTED_COMMIT"
