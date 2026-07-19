"""Derive the token digest for every existing webhook account.

Inbound authentication looks an account up by digest in one indexed read. A Password field cannot be
indexed, so the digest is derived and stored alongside it. Accounts that predate the column carry no
digest and would fail to authenticate, which would silently reject live webhooks — hence the backfill.

Idempotent, and run from both `patches.txt` (existing sites) and `schema_setup` (fresh installs,
where `install-app` baselines patches.txt without running it).
"""
import frappe

from tatva_connect.webhooks import ingress, registry


def execute():
	for cfg in registry.CHANNELS.values():
		doctype = cfg["account_doctype"]
		if not frappe.db.exists("DocType", doctype):
			continue
		for name in frappe.get_all(doctype, pluck="name"):
			_backfill(doctype, cfg, name)


def _backfill(doctype, cfg, name):
	doc = frappe.get_doc(doctype, name)
	for suffix in ("token", "token_previous"):
		hash_field = ingress.field(cfg, f"{suffix}_hash")
		if not frappe.db.has_column(doctype, hash_field):
			continue
		token = doc.get_password(ingress.field(cfg, suffix), raise_exception=False)
		digest = ingress.token_digest(token) if token else None
		if doc.get(hash_field) != digest:
			frappe.db.set_value(doctype, name, hash_field, digest, update_modified=False)
