# Validated image recipe (proven green building tatva-frappe:v16-rehearsal).
# Build:
#   export APPS_JSON_BASE64=$(base64 < apps.json | tr -d '\n')
#   docker build \
#     --build-arg=FRAPPE_PATH=https://github.com/frappe/frappe \
#     --build-arg=FRAPPE_BRANCH=version-16 --build-arg=FRAPPE_CORE_REF=v16.22.0 \
#     --build-arg=APPS_JSON_BASE64="$APPS_JSON_BASE64" \
#     --tag=<registry>/tatva-frappe:v16-1 --file=Containerfile .

ARG FRAPPE_BRANCH=version-16

FROM frappe/build:${FRAPPE_BRANCH} AS builder

ARG FRAPPE_BRANCH=version-16
# Frappe CORE pinned to the exact GA tag local proved (v16.22.0). NOT the version-16
# branch tip: tip drifted ahead and dropped frappe/public/js/lib/posthog.js, which
# breaks helpdesk's vite asset build. Base image stays :version-16 (no tagged base img).
ARG FRAPPE_CORE_REF=v16.22.0
ARG FRAPPE_PATH=https://github.com/frappe/frappe
ARG APPS_JSON_BASE64

USER root

RUN if [ -n "${APPS_JSON_BASE64}" ]; then \
    mkdir /opt/frappe && echo "${APPS_JSON_BASE64}" | base64 -d > /opt/frappe/apps.json; \
  fi

RUN chown -R frappe:frappe /home/frappe/.nvm

USER frappe

SHELL ["/bin/bash", "-c"]

RUN export APP_INSTALL_ARGS="" && \
  if [ -n "${APPS_JSON_BASE64}" ]; then \
    export APP_INSTALL_ARGS="--apps_path=/opt/frappe/apps.json"; \
  fi && \
  . "$NVM_DIR/nvm.sh" && nvm install 24 && nvm use 24 && nvm alias default 24 && npm install -g yarn && \
  bench init ${APP_INSTALL_ARGS}\
    --frappe-branch=${FRAPPE_CORE_REF} \
    --frappe-path=${FRAPPE_PATH} \
    --no-procfile \
    --no-backups \
    --skip-redis-config-generation \
    --verbose \
    /home/frappe/frappe-bench && \
  cd /home/frappe/frappe-bench && \
  echo "{}" > sites/common_site_config.json && \
  find apps -mindepth 1 -path "*/.git" | xargs rm -fr

FROM frappe/base:${FRAPPE_BRANCH} AS backend

USER frappe

COPY --from=builder --chown=frappe:frappe /home/frappe/frappe-bench /home/frappe/frappe-bench

WORKDIR /home/frappe/frappe-bench

VOLUME [ \
  "/home/frappe/frappe-bench/sites", \
  "/home/frappe/frappe-bench/sites/assets", \
  "/home/frappe/frappe-bench/logs" \
]

CMD [ \
  "/home/frappe/frappe-bench/env/bin/gunicorn", \
  "--chdir=/home/frappe/frappe-bench/sites", \
  "--bind=0.0.0.0:8000", \
  "--threads=4", \
  "--workers=4", \
  "--worker-class=gthread", \
  "--worker-tmp-dir=/dev/shm", \
  "--timeout=120", \
  "--preload", \
  "frappe.app:application" \
]
