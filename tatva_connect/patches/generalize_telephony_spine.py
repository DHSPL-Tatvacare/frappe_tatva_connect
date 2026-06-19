"""Provider-neutral telephony spine.

Renames the Acefone-anchored doctypes to a neutral channel spine, renames the
provider-named fields, and seeds the new `provider` Select — preserving all account,
routing and call-log data. Runs before model sync so the renamed doctypes and fields are
in place when the JSON and fixtures sync. Idempotent; a fresh install (nothing to rename)
is a clean no-op.
"""
import frappe

_DOCTYPE_RENAMES = [
	("CRM Acefone Account", "CRM Telephony Account"),
	("CRM Acefone Account Routing", "CRM Telephony Routing"),
	("CRM Acefone Settings", "CRM Telephony Settings"),
]


def execute():
	for old, new in _DOCTYPE_RENAMES:
		if frappe.db.exists("DocType", old) and not frappe.db.exists("DocType", new):
			frappe.rename_doc("DocType", old, new, force=True)
	_rename_fields()
	_seed_provider()
	frappe.db.commit()


def _rename_fields():
	# Routing: a standard Link field (defined in the doctype JSON) — rename the column; the
	# DocField is re-synced from the new JSON.
	if _column_exists("tabCRM Telephony Routing", "acefone_account") and not _column_exists(
		"tabCRM Telephony Routing", "telephony_account"
	):
		frappe.db.sql_ddl(
			"ALTER TABLE `tabCRM Telephony Routing` CHANGE `acefone_account` `telephony_account` varchar(140)"
		)
	# Call Log: our Custom Field — drop the old Custom Field doc (this does not drop the column),
	# then rename the column so the data carries over to the fixture-recreated custom_telephony_account.
	if frappe.db.exists("Custom Field", "CRM Call Log-custom_acefone_account"):
		frappe.delete_doc("Custom Field", "CRM Call Log-custom_acefone_account", force=True)
	if _column_exists("tabCRM Call Log", "custom_acefone_account") and not _column_exists(
		"tabCRM Call Log", "custom_telephony_account"
	):
		frappe.db.sql_ddl(
			"ALTER TABLE `tabCRM Call Log` CHANGE `custom_acefone_account` `custom_telephony_account` varchar(140)"
		)


def _seed_provider():
	"""Stamp existing accounts with provider 'Acefone'. The column is a standard field added
	by model sync; add it here so the backfill runs in this one pre-sync patch."""
	if not frappe.db.table_exists("CRM Telephony Account"):
		return
	if not _column_exists("tabCRM Telephony Account", "provider"):
		frappe.db.sql_ddl("ALTER TABLE `tabCRM Telephony Account` ADD COLUMN `provider` varchar(140)")
	frappe.db.sql(
		"UPDATE `tabCRM Telephony Account` SET provider = 'Acefone' WHERE COALESCE(provider, '') = ''"
	)


def _column_exists(table, column):
	return bool(
		frappe.db.sql(
			"""SELECT 1 FROM information_schema.COLUMNS
			   WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s AND COLUMN_NAME = %s""",
			(table, column),
		)
	)
