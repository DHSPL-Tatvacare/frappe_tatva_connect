"""Idempotent catalog sync — one `CRM Tatva Automation` row per registry entry.

Runs on after_migrate. Insert a missing row DORMANT (`enabled=0`); the operator
turns it on and it STAYS on. Code never owns `enabled`: existing rows refresh only
their descriptive/structural fields, so no deploy ever flips an operator's switch.
"""
import frappe

from tatva_connect.automation.registry import AUTOMATIONS


def _scheduled_job(auto):
	# For scheduled rows the dotted method IS the Scheduled Job Type.method.
	return auto.backs[0] if auto.fires_on == "Schedule" and auto.backs else ""


def _structural_values(auto):
	return {
		"area": auto.key.split("::")[0],
		"fires_on": auto.fires_on,
		"trigger_detail": auto.trigger_detail,
		"description": auto.purpose,
		"requires": auto.requires,
		"scheduled_job": _scheduled_job(auto),
	}


def sync_catalog():
	for auto in AUTOMATIONS:
		if frappe.db.exists("CRM Tatva Automation", auto.key):
			doc = frappe.get_doc("CRM Tatva Automation", auto.key)
			for field, value in _structural_values(auto).items():
				doc.set(field, value)
			# NEVER touch `enabled` — config is the operator's, code only refreshes labels.
			doc.save(ignore_permissions=True)
		else:
			doc = frappe.new_doc("CRM Tatva Automation")
			doc.automation_key = auto.key
			for field, value in _structural_values(auto).items():
				doc.set(field, value)
			doc.enabled = 0  # ships dormant (invariant 6); operator enables, it stays.
			doc.insert(ignore_permissions=True)
