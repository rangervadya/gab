#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "$0")/_common.sh"
require_clean_revision
load_compose
for key in BACKUP_DIR AGE_RECIPIENT POSTGRES_SERVICE POSTGRES_USER N8N_SERVICE IMPORT_STORAGE N8N_SETTINGS_DIR CONFIG_DIR; do need "$key"; done
command -v age >/dev/null || fail 'Установите age для шифрования резервной копии'
for path in "$IMPORT_STORAGE" "$N8N_SETTINGS_DIR" "$CONFIG_DIR"; do [[ -d "$path" ]] || fail "Не найден каталог $path"; done
mkdir -p "$BACKUP_DIR"
work=$(mktemp -d)
remote="/tmp/gab-backup-$(date -u +%Y%m%dT%H%M%SZ)-$$"
n8n_id=$(container_id "$N8N_SERVICE")
cleanup() { docker exec "$n8n_id" rm -rf "$remote" >/dev/null 2>&1 || true; rm -rf "$work"; }
trap cleanup EXIT
pg_id=$(container_id "$POSTGRES_SERVICE")
[[ $(docker exec "$pg_id" psql -U "$POSTGRES_USER" -d gab_crm -Atc 'SELECT current_database()') == gab_crm ]] || fail 'Проверка базы gab_crm не пройдена'
docker exec "$pg_id" pg_dump -U "$POSTGRES_USER" -d gab_crm -Fc > "$work/gab_crm.dump"
docker exec "$n8n_id" mkdir -p "$remote"
docker exec "$n8n_id" n8n export:workflow --all --output="$remote/workflows.json" >/dev/null
# Without --decrypted: credentials remain encrypted. The encryption key in
# N8N_SETTINGS_DIR/config is enclosed in the age-encrypted archive as well.
docker exec "$n8n_id" n8n export:credentials --all --output="$remote/credentials.json" >/dev/null
docker cp "$n8n_id:$remote/workflows.json" "$work/workflows.json" >/dev/null
docker cp "$n8n_id:$remote/credentials.json" "$work/credentials.json" >/dev/null
cp "$COMPOSE_FILE" "$work/compose.yml"
printf '%s\n' "$EXPECTED_COMMIT" > "$work/commit.txt"
for key in CRM_SERVICE OCR_SERVICE; do
  if [[ -n "${!key:-}" ]]; then
    cid=$(container_id "${!key}")
    docker inspect --format '{{.Image}}' "$cid" > "$work/$key.image"
    docker image save "$(cat "$work/$key.image")" > "$work/$key.image.tar"
  fi
done
tar -czf "$work/state.tar.gz" "$IMPORT_STORAGE" "$N8N_SETTINGS_DIR" "$CONFIG_DIR"
out="$BACKUP_DIR/gab-$(date -u +%Y%m%dT%H%M%SZ)-$$.tar.age"
tar -cf - -C "$work" . | age -r "$AGE_RECIPIENT" -o "$out.partial"
mv "$out.partial" "$out"
shasum -a 256 "$out" > "$out.sha256"
echo "Резервная копия: $out"
