#!/usr/bin/env bash
# One-time local bring-up: config, site, the nine apps, migrate, bundles, then the whole stack.
# Safe to re-run: every step checks what already exists and skips it.
set -euo pipefail
cd "$(dirname "$0")"

die() { echo "first-boot: $*" >&2; exit 1; }
dc() { docker compose -f compose.yml --env-file .env "$@"; }
bench_in() { dc exec -T backend bench "$@"; }

[ -f .env ] || die "no .env: run 'cp .env.example .env' in .localdev/ and fill in DB_PASSWORD and ADMIN_PASSWORD"
set -a; . ./.env; set +a
SITE="${SITE_NAME:-${SITE:-dev.localhost}}"
[ -n "${DB_PASSWORD:-}" ] && [ -n "${ADMIN_PASSWORD:-}" ] || die "DB_PASSWORD and ADMIN_PASSWORD must be set in .env"
IMAGE="${CUSTOM_IMAGE:-tatva-frappe}:${CUSTOM_TAG:-local}"
docker image inspect "$IMAGE" >/dev/null 2>&1 || die "image $IMAGE not found: build it from the Containerfile first (see LOCAL-DEV.md, 'Build the image')"
[ -d "${CRM_REPO:-../../frappe_tatva_crm}" ] || die "CRM repo not found at ${CRM_REPO:-../../frappe_tatva_crm}: clone frappe_tatva_crm next to this repo or set CRM_REPO"

echo "== 1/5  database, redis and the configurator =="
dc up -d db redis-cache redis-queue
dc up --no-log-prefix configurator

echo "== 2/5  backend =="
dc up -d backend
for _ in $(seq 1 30); do bench_in --version >/dev/null 2>&1 && break; sleep 2; done
bench_in --version >/dev/null 2>&1 || die "backend did not come up: check 'docker compose -f compose.yml --env-file .env logs backend'"

if dc exec -T backend test -f "sites/$SITE/site_config.json"; then
  echo "== 3/5  site $SITE exists, skipping create and install =="
else
  echo "== 3/5  create $SITE and install the nine apps (crm before tatva_connect) =="
  bench_in new-site "$SITE" --no-mariadb-socket --db-host db \
    --mariadb-root-password "$DB_PASSWORD" --admin-password "$ADMIN_PASSWORD"
  bench_in --site "$SITE" install-app payments telephony frappe_whatsapp helpdesk lms wiki insights crm tatva_connect
fi

echo "== 4/5  migrate, scheduler on, tests allowed =="
bench_in --site "$SITE" migrate
bench_in --site "$SITE" enable-scheduler
bench_in --site "$SITE" set-config -p allow_tests true

echo "== 5/5  bundles (the bind mounts hide the ones baked into the image) =="
dc exec -T backend sh -c 'cd apps/crm/frontend && [ -d node_modules ] || yarn install --frozen-lockfile'
dc exec -T -e NODE_OPTIONS=--max-old-space-size=6144 backend sh -c 'cd apps/crm/frontend && yarn build'
bench_in build --app tatva_connect

dc up -d
dc restart frontend
echo
echo "Ready: http://${SITE}:${APP_PUBLISH:-8080}  (use the site name, not localhost; log in as Administrator with ADMIN_PASSWORD from .env)"
