"""Idempotent catalog reconcile — EXACTLY one `CRM Tatva Automation` row per registry entry.

Runs on after_migrate. Insert a missing row DORMANT (`enabled=0`); the operator
turns it on and it STAYS on. Code never owns `enabled`: existing rows refresh only
their descriptive/structural fields, so no deploy ever flips an operator's switch.
A row whose key has LEFT the registry (a retired automation) is pruned — the catalog
is the source of truth, and an unreferenced row does nothing, so its removal is a
no-op behaviourally. No one-off patch is ever needed to retire an automation.
"""
import frappe

from tatva_connect.automation.registry import AUTOMATIONS, area_of
from tatva_connect.automation.settings import is_enabled


def _scheduled_job(auto):
	# For scheduled rows the dotted method IS the Scheduled Job Type.method.
	return auto.backs[0] if auto.fires_on == "Schedule" and auto.backs else ""


def _structural_values(auto):
	return {
		"area": area_of(auto.key),
		"fires_on": auto.fires_on,
		"trigger_detail": auto.trigger_detail,
		"description": auto.purpose,
		"requires": auto.requires,
		"scheduled_job": _scheduled_job(auto),
	}


def _parents_first(autos):
	# `requires` is a Link, so a row's target must already exist when it is written. One level deep.
	return [auto for auto in autos if not auto.requires] + [auto for auto in autos if auto.requires]


def sync_catalog():
	for auto in _parents_first(AUTOMATIONS):
		if frappe.db.exists("CRM Tatva Automation", auto.key):
			doc = frappe.get_doc("CRM Tatva Automation", auto.key)
			for field, value in _structural_values(auto).items():
				doc.set(field, value)
			# NEVER touch `enabled` — config is the operator's, code only refreshes labels.
			doc.save(ignore_permissions=True)  # authz-ok: tier-a — seed, runs at migrate
		else:
			doc = frappe.new_doc("CRM Tatva Automation")
			doc.automation_key = auto.key
			for field, value in _structural_values(auto).items():
				doc.set(field, value)
			doc.enabled = 0  # ships dormant (invariant 6); operator enables, it stays.
			doc.insert(ignore_permissions=True)  # authz-ok: tier-a — seed, runs at migrate

	# Prune rows whose key left the registry (a retired automation). The catalog is the
	# source of truth; an unreferenced row is a dead toggle — deleting it changes nothing.
	# No delete order is needed: `force=True` bypasses the link check (frappe delete_doc.py:45), and a
	# stale parent under a live child cannot exist because `assert_valid_graph` refuses a dead `requires`.
	live = {auto.key for auto in AUTOMATIONS}
	for name in frappe.get_all("CRM Tatva Automation", pluck="name"):
		if name not in live:
			frappe.delete_doc("CRM Tatva Automation", name, ignore_permissions=True, force=True)  # authz-ok: tier-a — seed, runs at migrate

	_disarm_orphans()


def _disarm_orphans():
	"""A row left armed above a dormant parent is switched off — the SAME rule the cascade applies.

	Only a code change can create this state: declaring a NEW `requires` over a switch an operator already
	armed. The desk cannot, because arming is refused and switching a parent off takes its children with it.
	When it happens the switch is ALREADY off in every sense that matters — `is_enabled` walks the chain and
	answers False — so this writes nothing new, it stops the row claiming otherwise.

	Code never ARMS an automation here; it only ever disarms one to match what is already enforced.
	"""
	for auto in AUTOMATIONS:
		if not auto.requires:
			continue
		if not frappe.db.get_value("CRM Tatva Automation", auto.key, "enabled"):
			continue
		if is_enabled(auto.requires):
			continue
		frappe.db.set_value("CRM Tatva Automation", auto.key, "enabled", 0, update_modified=False)
		frappe.log_error(
			title="automation disarmed: its declared parent is off",
			message=f"{auto.key} was armed but {auto.requires} is off, so it was switched off to match.",
		)


def reconcile_activations():
	# Deploy-time authoritative sync: set each toggle-owned infrastructure to match its current
	# enabled state (runtime flips are handled by CRM Tatva Automation.on_update).
	for auto in AUTOMATIONS:
		if auto.activator:
			frappe.get_attr(auto.activator)(is_enabled(auto.key))
