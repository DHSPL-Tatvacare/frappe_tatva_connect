"""Move the inbound webhook secrets from Data to Password storage, preserving existing values.

`WhatsApp Account.custom_webhook_token` and `CRM Telephony Account.webhook_token` flip from
Data to Password (fixture + doctype JSON). Frappe's migrate changes the field meta but does NOT
move an existing row's plaintext into the encrypted `__Auth` store — the cleartext just sits in
the table column. This patch carries each existing token forward through the Password store
(set_encrypted_password) and replaces the column with the dummy `***` placeholder, so the value
survives the Data->Password switch and reads back via get_password().

Merge-not-clobber + idempotent: a row is migrated ONLY when its column still holds a real value
(non-empty, not the all-`*` placeholder Frappe writes once a Password is in `__Auth`); otherwise
it is left untouched. So a re-run (column already `***`), a fresh install (blank tokens), or a
row already saved through the form is a clean no-op. The placeholder is Frappe's own
`is_dummy_password` marker — once set, the real secret lives in the encrypted store and
get_password() reads it. Runs on after_migrate (via schema_setup) and in post_model_sync.
"""
import frappe
from frappe.utils.password import set_encrypted_password

_TARGETS = (
	("WhatsApp Account", "custom_webhook_token"),
	("CRM Telephony Account", "webhook_token"),
)


def execute():
	for doctype, fieldname in _TARGETS:
		if not frappe.db.table_exists(doctype):
			continue
		for name in frappe.get_all(doctype, pluck="name"):
			_migrate_one(doctype, name, fieldname)


def _migrate_one(doctype, name, fieldname):
	raw = frappe.db.get_value(doctype, name, fieldname)
	if not raw or _is_placeholder(raw):
		return  # blank, or already moved to the Password store (column holds the dummy)
	set_encrypted_password(doctype, name, raw, fieldname)
	frappe.db.set_value(doctype, name, fieldname, "*" * len(raw), update_modified=False)


def _is_placeholder(value):
	return "".join(set(value)) == "*"
