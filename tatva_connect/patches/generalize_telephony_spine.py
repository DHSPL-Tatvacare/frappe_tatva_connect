"""Rename the Acefone-anchored telephony doctypes/fields to a provider-neutral spine and backfill provider='Acefone', preserving all data (pre-model-sync); idempotent."""
import frappe

from tatva_connect.patches import _schema

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
	if frappe.db.has_column("CRM Telephony Routing", "acefone_account") and not frappe.db.has_column(
		"CRM Telephony Routing", "telephony_account"
	):
		_schema.rename_column("CRM Telephony Routing", "acefone_account", "telephony_account")
	# Call Log: our Custom Field — drop the old Custom Field doc (not the column), then rename the column so data carries to the fixture-recreated custom_telephony_account.
	if frappe.db.exists("Custom Field", "CRM Call Log-custom_acefone_account"):
		frappe.delete_doc("Custom Field", "CRM Call Log-custom_acefone_account", force=True)
	if frappe.db.has_column("CRM Call Log", "custom_acefone_account") and not frappe.db.has_column(
		"CRM Call Log", "custom_telephony_account"
	):
		_schema.rename_column("CRM Call Log", "custom_acefone_account", "custom_telephony_account")


def _seed_provider():
	"""Stamp existing accounts with provider 'Acefone'; add the column here so the backfill runs in this one pre-sync patch."""
	if not frappe.db.table_exists("CRM Telephony Account"):
		return
	if not frappe.db.has_column("CRM Telephony Account", "provider"):
		# ALLOWLIST: raw ADD COLUMN pre-model-sync — the column must exist before the JSON syncs the field; Frappe ships no DDL helper that adds a column.
		_schema.ddl("ALTER TABLE `tabCRM Telephony Account` ADD COLUMN `provider` varchar(140)", "tabCRM Telephony Account")
	# ALLOWLIST: raw bulk backfill pre-model-sync — the DocField isn't synced yet, so set_value/ORM can't reach `provider`; the value is a constant literal, no interpolation.
	frappe.db.sql(
		"UPDATE `tabCRM Telephony Account` SET provider = 'Acefone' WHERE COALESCE(provider, '') = ''"
	)
