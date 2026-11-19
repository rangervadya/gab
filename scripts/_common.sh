#!/usr/bin/env bash
set -euo pipefail
umask 077
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
fail() { echo "Ошибка: $*" >&2; exit 1; }
need() { [[ -n "${!1:-}" ]] || fail "Задайте $1"; }
require_clean_revision() {
  need EXPECTED_COMMIT
  [[ $(git -C "$ROOT" branch --show-current) == main ]] || fail 'Требуется ветка main'
  [[ $(git -C "$ROOT" rev-parse HEAD) == "$EXPECTED_COMMIT" ]] || fail 'Commit не совпадает с EXPECTED_COMMIT'
  [[ -z $(git -C "$ROOT" status --porcelain) ]] || fail 'Рабочая копия содержит изменения'
}
load_compose() {
  need COMPOSE_FILE
  [[ -f "$COMPOSE_FILE" ]] || fail 'Полный серверный Compose не найден'
  COMPOSE=(docker compose -f "$COMPOSE_FILE")
  "${COMPOSE[@]}" config --quiet
}
service_exists() {
  "${COMPOSE[@]}" config --services | awk -v s="$1" '$0 == s { found=1 } END { exit !found }' || fail "Сервис $1 отсутствует в Compose"
}
container_id() {
  service_exists "$1"
  local cid
  cid=$("${COMPOSE[@]}" ps -q "$1")
  [[ -n "$cid" && "$cid" != *$'\n'* ]] || fail "Для $1 нужен один работающий контейнер"
  printf '%s' "$cid"
}
check_health() {
  local url=$1
  local args=(--fail --silent --show-error --max-time 20 --retry 5 --retry-delay 3 --retry-all-errors)
  if [[ -n "${HEALTH_NETRC:-}" ]]; then args+=(--netrc-file "$HEALTH_NETRC"); fi
  curl "${args[@]}" "$url" >/dev/null
}
