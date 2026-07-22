# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Layer-2/3 config — the UAT target and its credentials. Code is tracked; creds are NOT.

Reads `tatva_connect/tests/vapt_live/.creds/uat.json`:

    {
      "base": "https://<uat-host>",
      "host": "<uat-host>",
      "personas": {
        "admin":   {"email": "...", "password": "..."},
        "crm_user":{"email": "...", "password": "..."},
        "no_role": {"email": "...", "password": "..."},
        "guest":   {}
      },
      "targets": {"CRM Lead": "abc123"}         # optional; resolved via admin when absent
    }

The host guard is the whole point of this file: an ACTIVE attack run must never touch production.
"""
import json
import os

_HERE = os.path.dirname(os.path.abspath(__file__))
CREDS_PATH = os.path.join(_HERE, ".creds", "uat.json")

# An active attack lane refuses anything that is not an explicitly allowed non-prod target.
_ALLOWED_HOST_MARKERS = ("uat", "staging", "localhost", "127.0.0.1", "dev.")
_FORBIDDEN_HOST_MARKERS = ("prod", "www.", "one.tatvacare.in")


class ConfigError(RuntimeError):
	pass


def load(path=None, allow_unsafe_host=False):
	path = path or CREDS_PATH
	if not os.path.exists(path):
		raise ConfigError(f"no creds at {path} — copy the shape in this module's docstring (gitignored)")
	with open(path) as fh:
		cfg = json.load(fh)
	for key in ("base", "host", "personas"):
		if not cfg.get(key):
			raise ConfigError(f"creds missing required key: {key}")
	_guard_host(cfg["host"], cfg["base"], allow_unsafe_host)
	return cfg


def _guard_host(host, base, allow_unsafe_host):
	blob = f"{host} {base}".lower()
	if any(m in blob for m in _FORBIDDEN_HOST_MARKERS) and not allow_unsafe_host:
		raise ConfigError(f"REFUSING active attack run against what looks like production: {base}")
	if not any(m in blob for m in _ALLOWED_HOST_MARKERS) and not allow_unsafe_host:
		raise ConfigError(f"target {base} matches no known non-prod marker; pass allow_unsafe_host to override")


def engine_creds(cfg):
	"""Shape the personas into what http_engine.HttpEngine expects: {persona: {token, ...}}."""
	return {name: dict(p) for name, p in cfg["personas"].items()}
