"""Idempotent catalog sync — one `CRM Tatva Automation` row per registry entry.

Runs on after_migrate. Insert missing rows with their structural fields + the
intrinsic `enabled` (1 for guard/infra always-run rows, 0 for switches — switches
ship DORMANT, the operator enables them via the cutover SQL). Existing rows refresh
structural fields ONLY — never the operator-set `enabled` — except guard/infra rows
are forced to 1 (they always run; the row is visibility-only).
"""
import frappe

from tatva_connect.automation.registry import AUTOMATIONS


def _scheduled_job(auto):
	# For scheduled rows the dotted method IS the Scheduled Job Type.method.
	return auto.backs[0] if auto.trigger == "Scheduled" and auto.backs else ""


def _structural_values(auto):
	return {
		"label": auto.label,
		"entity": auto.entity,
		"kind": auto.kind,
		"trigger": auto.trigger,
		"trigger_detail": auto.trigger_detail,
		"requires": auto.requires,
		"scheduled_job": _scheduled_job(auto),
	}


def sync_catalog():
	for auto in AUTOMATIONS:
		always_on = auto.kind in ("Guard", "Infra")
		if frappe.db.exists("CRM Tatva Automation", auto.key):
			doc = frappe.get_doc("CRM Tatva Automation", auto.key)
			for field, value in _structural_values(auto).items():
				doc.set(field, value)
			# never touch operator-set `enabled` for switches; guards/infra are forced on.
			if always_on:
				doc.enabled = 1
			doc.save(ignore_permissions=True)
		else:
			doc = frappe.new_doc("CRM Tatva Automation")
			doc.automation_key = auto.key
			for field, value in _structural_values(auto).items():
				doc.set(field, value)
			doc.enabled = 1 if always_on else 0
			doc.insert(ignore_permissions=True)
