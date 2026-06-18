"""Fold: carry the legacy task-transition engine's master switch onto the unified automation engine,
then drop the orphaned legacy child table — safely.

The legacy `Task::CRM Task::transitions` automation was folded into the unified engine
(`Task::Automation::rules`): config rows migrated by the db-seeds SQL, code/doctype removed. This
one-time patch (post_model_sync, BEFORE seed.sync_catalog prunes the legacy registry row):

1. Carries the operator's switch (on stays on / off stays off). It UPSERTS the unified row so the
   carry survives a multi-version-jump migrate where sync_catalog hasn't seeded the unified row yet
   (sync_catalog's existing-row refresh never touches `enabled`, so a value set here holds).
2. Drops the orphaned legacy table — but FAILS LOUD if it still holds un-migrated rows, so a
   mis-ordered cutover (deploy before running the db-seeds migration SQL) can't silently destroy the
   source data. Idempotent; no-op on a fresh install.
"""
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
		# Unified row not seeded yet (DB predates the engine; sync_catalog runs later in after_migrate
		# and won't touch `enabled` on an existing row) — create it now carrying the legacy state.
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
