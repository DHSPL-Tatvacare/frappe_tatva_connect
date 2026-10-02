# syntax=docker/dockerfile:1.17
# One cached step per app, slowest-moving first; each step reruns only when its own pinned commit (from APPS_RESOLVED_B64) moves.
# Base images pinned by multi-arch digest so a republished tag never voids the cache; bump the digest deliberately.
ARG BUILD_IMAGE=frappe/build:version-16@sha256:5c9cfa1b14797e56954efc8aca7206ee0eff37082bed1cf38957bc4cda74c245
ARG BASE_IMAGE=frappe/base:version-16@sha256:4e0b32192d2f8fe8af24891691f7e2b525f4614c541a80f30c1f353f6e27e80e

# Splits CI's resolved manifest into one "url ref commit" file per repo; downstream COPYs key on each file's bytes.
FROM ${BUILD_IMAGE} AS pins
ARG APPS_RESOLVED_B64
RUN test -n "${APPS_RESOLVED_B64}" || { echo "APPS_RESOLVED_B64 is required" >&2; exit 1; }; \
  mkdir -p /tmp/pins && echo "${APPS_RESOLVED_B64}" | base64 -d > /tmp/all.json && \
  python3 -c 'import json; [print(a["url"], a["ref"], a["commit"], file=open("/tmp/pins/" + a["url"].rstrip("/").rsplit("/", 1)[1], "w")) for a in json.load(open("/tmp/all.json"))]'

FROM ${BUILD_IMAGE} AS core
USER root
RUN chown -R frappe:frappe /home/frappe/.nvm
USER frappe
SHELL ["/bin/bash", "-c"]
ENV UV_LINK_MODE=copy
RUN . "$NVM_DIR/nvm.sh" && nvm install 24 && nvm use 24 && nvm alias default 24 && npm install -g yarn

# Private apps: CI passes a PAT as a BuildKit secret (not part of any cache key), readable by the frappe uid.
RUN --mount=type=secret,id=gh_pat,required=false,uid=1000,gid=1000,mode=0400 \
    if [ -f /run/secrets/gh_pat ]; then \
      GH_PAT="$(cat /run/secrets/gh_pat)" && \
      git config --global url."https://x-access-token:${GH_PAT}@github.com/".insteadOf "https://github.com/"; \
    fi

COPY --chmod=755 <<'EOF' /usr/local/bin/get-pinned-app
#!/bin/bash
set -euo pipefail
read -r url ref commit < "/opt/pins/$1"
. "$NVM_DIR/nvm.sh"
cd /home/frappe/frappe-bench
before=$(ls apps | sort)
bench get-app --branch "$ref" "$url"
app=$(comm -13 <(echo "$before") <(ls apps | sort))
head=$(git -C "apps/$app" rev-parse HEAD)
[ "$head" = "$commit" ] || { echo "$1: cloned $head, pinned $commit — branch moved, rerun the build" >&2; exit 1; }
rm -rf "apps/$app/.git"
EOF

# Frappe core pinned to a GA tag (floors: v16.27.0 OAuth loopback, v16.30.0 `ui/` for helpdesk).
ARG FRAPPE_CORE_REF=v16.36.0
ARG FRAPPE_PATH=https://github.com/frappe/frappe
RUN --mount=type=cache,target=/home/frappe/.cache,uid=1000,gid=1000 \
  . "$NVM_DIR/nvm.sh" && \
  bench init \
    --frappe-branch=${FRAPPE_CORE_REF} \
    --frappe-path=${FRAPPE_PATH} \
    --no-procfile \
    --no-backups \
    --skip-redis-config-generation \
    /home/frappe/frappe-bench && \
  rm -rf /home/frappe/frappe-bench/apps/frappe/.git

# Dependencies first (helpdesk needs telephony, lms needs payments), then tagged apps, then branch-tracking ones, ours last.
FROM core AS vendor
COPY --from=pins /tmp/pins/telephony /opt/pins/telephony
RUN --mount=type=cache,target=/home/frappe/.cache,uid=1000,gid=1000 get-pinned-app telephony
COPY --from=pins /tmp/pins/payments /opt/pins/payments
RUN --mount=type=cache,target=/home/frappe/.cache,uid=1000,gid=1000 get-pinned-app payments
COPY --from=pins /tmp/pins/helpdesk /opt/pins/helpdesk
RUN --mount=type=cache,target=/home/frappe/.cache,uid=1000,gid=1000 get-pinned-app helpdesk
COPY --from=pins /tmp/pins/lms /opt/pins/lms
RUN --mount=type=cache,target=/home/frappe/.cache,uid=1000,gid=1000 get-pinned-app lms
COPY --from=pins /tmp/pins/wiki /opt/pins/wiki
RUN --mount=type=cache,target=/home/frappe/.cache,uid=1000,gid=1000 get-pinned-app wiki
COPY --from=pins /tmp/pins/insights /opt/pins/insights
RUN --mount=type=cache,target=/home/frappe/.cache,uid=1000,gid=1000 get-pinned-app insights
COPY --from=pins /tmp/pins/frappe_whatsapp /opt/pins/frappe_whatsapp
RUN --mount=type=cache,target=/home/frappe/.cache,uid=1000,gid=1000 get-pinned-app frappe_whatsapp
COPY --from=pins /tmp/pins/frappe_tatva_crm /opt/pins/frappe_tatva_crm
RUN --mount=type=cache,target=/home/frappe/.cache,uid=1000,gid=1000 get-pinned-app frappe_tatva_crm

FROM vendor AS connect
COPY --from=pins /tmp/pins/frappe_tatva_connect /opt/pins/frappe_tatva_connect
RUN --mount=type=cache,target=/home/frappe/.cache,uid=1000,gid=1000 get-pinned-app frappe_tatva_connect
# Fails the build if the manifest names an app this file does not install.
COPY --from=pins /tmp/pins /tmp/all-pins
RUN diff <(ls /tmp/all-pins) <(ls /opt/pins)
# Everything but apps/, so the final image reuses vendor's apps layer and takes only this step's changes.
RUN mkdir /tmp/rest && tar -C /home/frappe/frappe-bench --exclude=./apps -cf - . | tar -C /tmp/rest -xf -

FROM ${BASE_IMAGE} AS backend

USER root

COPY docker/entrypoint.sh docker/start.sh /usr/local/bin/
RUN chmod 755 /usr/local/bin/entrypoint.sh /usr/local/bin/start.sh

# TATVA: bake our nginx template (adds /docs route + WATI/Acefone webhooks) over frappe_docker's default.
COPY nginx/frappe.conf.template /templates/nginx/frappe.conf.template
# TATVA: and our security headers (adds Permissions-Policy + report-only CSP) over theirs.
COPY nginx/security_headers.conf /etc/nginx/snippets/security_headers.conf

USER frappe

COPY --from=vendor --chown=frappe:frappe /home/frappe/frappe-bench/apps /home/frappe/frappe-bench/apps
COPY --from=connect --chown=frappe:frappe /home/frappe/frappe-bench/apps/tatva_connect /home/frappe/frappe-bench/apps/tatva_connect
COPY --from=connect --chown=frappe:frappe /tmp/rest /home/frappe/frappe-bench
# Provenance: the resolved pins ride inside the bench (cat apps.resolved.json in a running container).
COPY --from=pins --chown=frappe:frappe /tmp/all.json /home/frappe/frappe-bench/apps.resolved.json

WORKDIR /home/frappe/frappe-bench

# Bake assets into the image layer; entrypoint symlinks sites/assets → assets at container start.
RUN cp -r /home/frappe/frappe-bench/sites/assets /home/frappe/frappe-bench/assets && \
  rm -rf /home/frappe/frappe-bench/sites/assets

# Do NOT declare sites/assets as a separate VOLUME — entrypoint links to baked assets instead.
VOLUME [ \
  "/home/frappe/frappe-bench/sites", \
  "/home/frappe/frappe-bench/logs" \
]

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["/usr/local/bin/start.sh"]

# Labels last so a new pin set never voids a layer above; promote-prod checks this label against apps.json.
ARG APP_REPO_SHA=unknown
ARG APPS_RESOLVED_B64=""
LABEL org.opencontainers.image.source="https://github.com/DHSPL-Tatvacare/frappe_tatva_connect" \
  org.opencontainers.image.revision="${APP_REPO_SHA}" \
  in.tatvacare.apps.resolved.b64="${APPS_RESOLVED_B64}"
