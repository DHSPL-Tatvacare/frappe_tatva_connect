#!/usr/bin/env bash
# Build the Zudoku docs and copy dist/docs into a running stack at sites/<SITE>/public/docs (served at /docs).
# Content-only: never touches nginx/compose/image. LOCAL if VM_SSH_HOST unset, else REMOTE over SSH.
# Requires node+npm (+ expect & scp for REMOTE). Run from api-docs/. See docs/DEPLOY-POSTURE.md sec.4 for usage.
set -euo pipefail

DC_PROJECT="${DC_PROJECT:?Set DC_PROJECT (compose project, e.g. tatvalocal | crm-uat | crm-prod)}"
DC_COMPOSE="${DC_COMPOSE:?Set DC_COMPOSE (path to the compose file for that stack)}"
SITE="${SITE:?Set SITE (Frappe site folder, e.g. dev.localhost | one-uat.tatvacare.in)}"
DEST="/home/frappe/frappe-bench/sites/${SITE}/public"
PROJ="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJ"

echo "[1/4] build"
[ -d node_modules ] || npm install
npm run build

# Strip Zudoku's CDN preconnect hint from the built HTML.
find dist/docs -name '*.html' -print0 | xargs -0 perl -i -pe 's{<link[^>]*cdn\.zudoku\.dev[^>]*>}{}g'

if [ -z "${VM_SSH_HOST:-}" ]; then
  # LOCAL: docker compose cp into the container (writes the sites volume).
  echo "[2/4] deploy locally into ${DC_PROJECT} (${SITE}) — atomic swap"
  dc() { docker compose -p "$DC_PROJECT" -f "$DC_COMPOSE" "$@"; }
  dc exec -T backend rm -rf "$DEST/docs_staging"
  dc cp dist/docs "backend:$DEST/docs_staging"
  dc exec -T backend sh -c "rm -rf $DEST/docs && mv $DEST/docs_staging $DEST/docs && chown -R frappe:frappe $DEST/docs"
  echo "[3/4] (local — no upload)"
  echo "[4/4] done -> verify http://localhost:8080/docs"
  exit 0
fi

# REMOTE: VM connection from env — never hardcode (this repo is public).
VM="$VM_SSH_HOST" ; SSHUSER="${VM_SSH_USER:-frappe}" ; PW="${VM_SSH_PW:?Set VM_SSH_PW (VM SSH password)}"

echo "[2/4] package dist/docs"
tar czf /tmp/zudoku-dist.tgz -C dist/docs .

echo "[3/4] upload tarball to ${VM}"
expect <<EXP
set timeout 240
spawn scp -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR /tmp/zudoku-dist.tgz $SSHUSER@$VM:/tmp/zudoku-dist.tgz
expect "password:"; send "$PW\r"; expect eof
EXP

echo "[4/4] deploy into ${DC_PROJECT} (${SITE}) sites volume — atomic swap"
expect <<EXP
set timeout 240
spawn ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR $SSHUSER@$VM "rm -rf /tmp/zdocs && mkdir -p /tmp/zdocs && tar xzf /tmp/zudoku-dist.tgz -C /tmp/zdocs && docker compose -p $DC_PROJECT -f $DC_COMPOSE exec -T backend rm -rf $DEST/docs $DEST/docs_staging && docker compose -p $DC_PROJECT -f $DC_COMPOSE cp /tmp/zdocs backend:$DEST/docs_staging && docker compose -p $DC_PROJECT -f $DC_COMPOSE exec -T backend sh -c 'mv $DEST/docs_staging $DEST/docs && chown -R frappe:frappe $DEST/docs' && echo DOCS_DEPLOYED"
expect "password:"; send "$PW\r"; expect eof
EXP

echo "Done -> verify https://${SITE}/docs"
