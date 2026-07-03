#!/bin/bash
# Backend gunicorn — worker/thread counts from compose env (GUNICORN_WORKERS etc.).
set -euo pipefail

WORKERS="${GUNICORN_WORKERS:-9}"
THREADS="${GUNICORN_THREADS:-4}"
TIMEOUT="${GUNICORN_TIMEOUT:-120}"

exec /home/frappe/frappe-bench/env/bin/gunicorn \
	--chdir=/home/frappe/frappe-bench/sites \
	--bind=0.0.0.0:8000 \
	--threads="${THREADS}" \
	--workers="${WORKERS}" \
	--worker-class=gthread \
	--worker-tmp-dir=/dev/shm \
	--timeout="${TIMEOUT}" \
	--preload \
	frappe.app:application
