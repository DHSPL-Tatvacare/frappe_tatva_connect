"""Carry inbound webhook tokens from the plaintext column into the encrypted Password store (set_encrypted_password + `***` placeholder) after the Data->Password flip; migrates only real values, idempotent."""
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
