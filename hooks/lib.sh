#!/usr/bin/env bash
# ONE resolver for how to invoke `tcsec` (the repo's security/test harness), venv-aware.
# Sourced by hooks/pre-commit and hooks/pre-push so there is a single definition (A.8).
# tcsec itself resolves the pinned scanners from the isolated venv, so hooks == CI (no drift).
set -euo pipefail
# Isolated uv-managed venv for the security scanners (never the bench env). Override with TCSEC_VENV.
VENV_DIR="${TCSEC_VENV:-$HOME/.venvs/venv-python-frappe-sec}"

_venv_python() {
  echo "$VENV_DIR/bin/python"
}

tcsec_run() {
  cd "$(git rev-parse --show-toplevel)"
  if command -v tcsec >/dev/null 2>&1; then
    tcsec "$@"; return
  fi
  local py; py="$(_venv_python)"
  if [ -x "$py" ]; then
    "$py" tatva_connect/security/tcsec.py "$@"; return
  fi
  cat >&2 <<EOF
✖ tcsec / security venv not found ($VENV_DIR).
  Set it up once with uv:
     uv venv "$VENV_DIR" --python 3.12
     uv pip install --python "$VENV_DIR/bin/python" -r tatva_connect/security/requirements.txt
  Emergency bypass (use sparingly): git commit/push --no-verify
EOF
  return 1
}
