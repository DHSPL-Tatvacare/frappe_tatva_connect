#!/bin/bash
# Link image-layer baked assets into the mounted sites volume (frappe_docker pattern).
set -e

ASSETS_PATH="/home/frappe/frappe-bench/sites/assets"
BAKED_PATH="/home/frappe/frappe-bench/assets"

if [ -d "$BAKED_PATH" ]; then
	rm -rf "$ASSETS_PATH"
	mkdir -p "$(dirname "$ASSETS_PATH")"
	ln -s "$BAKED_PATH" "$ASSETS_PATH"
fi

exec "$@"
