#!/usr/bin/env bash
# Build the Zudoku API docs and deploy them to https://one.tatvacare.in/docs
#
# Content-only: builds this project and ships the static output into the Frappe
# sites volume. Does NOT touch nginx/compose — the /docs route is permanent
# (nginx/frappe.conf.template, bind-mounted via crm-compose.yml; runbook Phase 17).
#
# Update docs = edit pages/*.mdx or openapi.json -> run this script. Seconds, no restart.
# Requires: node, npm, expect.   Run:  ./deploy-docs.sh   (from this api-docs/ dir)
set -euo pipefail

VM=10.50.16.9 ; SSHUSER=frappe ; PW='nVm6YgmX8j6V'
DEST=/home/frappe/frappe-bench/sites/crm.local/public
PROJ="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJ"

echo "[1/4] build"
[ -d node_modules ] || npm install
npm run build

echo "[2/4] package dist/docs"
tar czf /tmp/zudoku-dist.tgz -C dist/docs .

echo "[3/4] upload tarball"
expect <<EXP
set timeout 240
spawn scp -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR /tmp/zudoku-dist.tgz $SSHUSER@$VM:/tmp/zudoku-dist.tgz
expect "password:"; send "$PW\r"; expect eof
EXP

echo "[4/4] deploy into Frappe sites volume (atomic swap)"
expect <<EXP
set timeout 240
spawn ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR $SSHUSER@$VM "rm -rf /tmp/zdocs && mkdir -p /tmp/zdocs && tar xzf /tmp/zudoku-dist.tgz -C /tmp/zdocs && docker compose -p crm exec -T backend rm -rf $DEST/docs $DEST/docs_staging && docker compose -p crm cp /tmp/zdocs backend:$DEST/docs_staging && docker compose -p crm exec -T backend sh -c 'mv $DEST/docs_staging $DEST/docs && chown -R frappe:frappe $DEST/docs' && echo DOCS_DEPLOYED"
expect "password:"; send "$PW\r"; expect eof
EXP

echo "Done -> verify https://one.tatvacare.in/docs"
