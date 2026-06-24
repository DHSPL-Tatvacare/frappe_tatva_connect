"""Carry the legacy transition engine's master switch onto the unified automation row (upsert), then drop the orphaned legacy table — failing loud if it still holds un-migrated rows; idempotent."""
import frappe

LEGACY = "Task::CRM Task::transitions"
UNIFIED = "Task::Automation::rules"
LEGACY_TABLE = "tabCRM Task Type Transition"


def execute():
	_carry_switch()
	_drop_legacy_table()


def _carry_switch():
	if not frappe.db.exists("CRM Tatva Automation", LEGACY):
		return
	enabled = frappe.db.get_value("CRM Tatva Automation", LEGACY, "enabled")
	if frappe.db.exists("CRM Tatva Automation", UNIFIED):
		frappe.db.set_value("CRM Tatva Automation", UNIFIED, "enabled", enabled)
	else:
		# Unified row not seeded yet (sync_catalog runs later and won't touch `enabled`) — create it now carrying the legacy state.
		doc = frappe.new_doc("CRM Tatva Automation")
		doc.automation_key = UNIFIED
		doc.enabled = enabled
		doc.insert(ignore_permissions=True)


def _drop_legacy_table():
	if not frappe.db.table_exists("CRM Task Type Transition"):
		return
	rows = frappe.db.sql("SELECT COUNT(*) FROM `{0}`".format(LEGACY_TABLE))[0][0]
	migrated = frappe.db.count("CRM Automation Rule", {"description": ["like", "Migrated transition:%"]})
	if rows and not migrated:
		frappe.throw(
			"Automation fold aborted: `{0}` still holds {1} un-migrated transition rows. Run the "
			"db-seeds migration SQL (…migrate-transitions-to-automation-rules.sql) FIRST, then migrate "
			"again — refusing to drop the source data.".format(LEGACY_TABLE, rows)
		)
	frappe.db.sql("DROP TABLE IF EXISTS `{0}`".format(LEGACY_TABLE))
