# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Where the harness gets its inputs.

Secrets are never held here. LSQ keys and partner tokens are read from the operator's gitignored
creds directory, which the migration bundle already owns. The field maps are read as data, not
imported as code: this harness shares the signed-off LSQ->Frappe map, never the migration's loader.
"""
import json
import os
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent.parent  # partner_api_load -> tests -> tatva_connect -> repo
MIGRATION = REPO / "docs" / "go-live" / "7-migrate-data"
CREDS = MIGRATION / ".creds"
DATA = HERE / "data"
REPORTS = HERE / "reports"

BASE_URL = os.environ.get("LOADTEST_BASE_URL", "http://localhost:8080")
SITE_HOST = os.environ.get("LOADTEST_SITE", "dev.localhost")

# A deployment's tokens are its own. The local bench's keys are not UAT's keys, so the file is named
# per environment rather than overwritten, and a run says which one it read.
TOKENS_FILE = os.environ.get("LOADTEST_TOKENS", "partner-api-tokens.local.json")

# Account names and the partner user each one's traffic is sent as. These are LIVE identifiers (real
# service-account logins and customer names), so they are operator config, not source: this repo is
# PUBLIC. Read from the gitignored creds dir, with a non-identifying fallback so the module still
# imports on a machine that has no creds (the run then fails loudly at token lookup, not at import).
_ACCOUNTS_FILE = CREDS / "partner-accounts.json"


def _load_accounts():
	"""{account_name: partner_user_email} from .creds/partner-accounts.json. Absent -> empty, so the
	harness reports 'no accounts configured' rather than leaking a default into the public repo."""
	if not _ACCOUNTS_FILE.exists():
		return {}
	try:
		return json.loads(_ACCOUNTS_FILE.read_text())
	except (OSError, ValueError):
		return {}


PARTNER_USER = _load_accounts()
ACCOUNTS = tuple(PARTNER_USER)

# LSQ read endpoints this harness is allowed to reach. Read-only is enforced by allowlist, not by
# HTTP verb, because LSQ's retrieve APIs are POST-based. Nothing that creates, updates, captures or
# deletes is reachable from this process.
LSQ_READ_ENDPOINTS = {
	"LeadManagement.svc/Leads.Get": "POST",
	"LeadManagement.svc/RetrieveNote": "POST",
	"LeadManagement.svc/RetrieveTaskByLeadId": "GET",
	"ProspectActivity.svc/CustomActivity/RetrieveByActivityEvent": "POST",
}

# LSQ throttles the bulk activity read on a separate, lower limit — pace it slower.
LSQ_BULK_ENDPOINTS = {"ProspectActivity.svc/CustomActivity/RetrieveByActivityEvent"}


def lsq_creds(account):
	"""Parse .creds/<account>.env literally. No shell expansion: an LSQ access key contains '$'."""
	path = CREDS / f"{account}.env"
	if not path.exists():
		raise SystemExit(f"missing LSQ creds: {path}")
	out = {}
	for raw in path.read_text().splitlines():
		line = raw.strip()
		if not line or line.startswith("#") or "=" not in line:
			continue
		key, _, val = line.partition("=")
		val = val.strip()
		if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
			val = val[1:-1]
		out[key.strip()] = val
	for required in ("LSQ_HOST", "LSQ_ACCESS_KEY", "LSQ_SECRET_KEY"):
		if not out.get(required):
			raise SystemExit(f"{path} is missing {required}")
	return out


def partner_token(account, tokens_file=None):
	"""The Authorization header value for this account's grain-scoped partner key."""
	path = CREDS / (tokens_file or TOKENS_FILE)
	if not path.exists():
		raise SystemExit(f"missing partner tokens: {path}")
	users = json.loads(path.read_text())["users"]
	user = PARTNER_USER[account]
	if user not in users:
		raise SystemExit(f"{user} has no token in {path}")
	entry = users[user]
	return f"token {entry['api_key']}:{entry['api_secret']}", entry.get("grain", "?")


def field_map(account):
	"""The signed-off LSQ->Frappe map. Data, not code."""
	path = MIGRATION / account / "mapping.json"
	if not path.exists():
		raise SystemExit(f"missing field map: {path}")
	return json.loads(path.read_text())


def account_dir(account):
	d = DATA / account
	d.mkdir(parents=True, exist_ok=True)
	return d
