"""Fold: carry the legacy task-transition engine's master switch onto the unified automation engine.

The legacy `Task::CRM Task::transitions` automation was folded into the unified engine
(`Task::Automation::rules`) — its config rows are migrated by the db-seeds SQL, its code/doctype are
removed. This one-time patch preserves the operator's choice: whatever the legacy switch was
(on/off), the unified engine's switch becomes the same. Runs post_model_sync, BEFORE
seed.sync_catalog (after_migrate) prunes the now-orphaned legacy registry row. Idempotent.
"""
import frappe

LEGACY = "Task::CRM Task::transitions"
UNIFIED = "Task::Automation::rules"


def execute():
	# Carry the operator's switch choice onto the unified engine (on stays on / off stays off).
	if frappe.db.exists("CRM Tatva Automation", LEGACY) and frappe.db.exists("CRM Tatva Automation", UNIFIED):
		enabled = frappe.db.get_value("CRM Tatva Automation", LEGACY, "enabled")
		frappe.db.set_value("CRM Tatva Automation", UNIFIED, "enabled", enabled)
	# Drop the now-orphaned legacy child table. Frappe's orphan sweep deletes the DocType but leaves
	# the physical table; its rows were migrated to CRM Automation Rule by the db-seeds SQL before
	# this deploy. Idempotent; no-op on a fresh install.
	frappe.db.sql("DROP TABLE IF EXISTS `tabCRM Task Type Transition`")
