"""Rename the Acefone-anchored telephony doctypes/fields to a provider-neutral spine and backfill provider='Acefone', preserving all data (pre-model-sync); idempotent."""
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
	# Routing: a standard Link field — rename the column; the DocField re-syncs from the new JSON.
	if _column_exists("tabCRM Telephony Routing", "acefone_account") and not _column_exists(
		"tabCRM Telephony Routing", "telephony_account"
	):
		frappe.db.sql_ddl(
			"ALTER TABLE `tabCRM Telephony Routing` CHANGE `acefone_account` `telephony_account` varchar(140)"
		)
	# Call Log: our Custom Field — drop the old Custom Field doc (not the column), then rename the column so data carries to the fixture-recreated custom_telephony_account.
	if frappe.db.exists("Custom Field", "CRM Call Log-custom_acefone_account"):
		frappe.delete_doc("Custom Field", "CRM Call Log-custom_acefone_account", force=True)
	if _column_exists("tabCRM Call Log", "custom_acefone_account") and not _column_exists(
		"tabCRM Call Log", "custom_telephony_account"
	):
		frappe.db.sql_ddl(
			"ALTER TABLE `tabCRM Call Log` CHANGE `custom_acefone_account` `custom_telephony_account` varchar(140)"
		)


def _seed_provider():
	"""Stamp existing accounts with provider 'Acefone'; add the column here so the backfill runs in this one pre-sync patch."""
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
