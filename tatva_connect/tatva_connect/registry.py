"""Automation registry — a read-only inventory of everything that fires.

One call answers "what automation is live?" across both layers:
  * code layer  — doc_events, scheduler_events, override_* (from hooks, all apps)
  * config layer — Assignment Rule, Server Script, Notification (from the DB)

System Manager only (it introspects every app's handlers). Scheduled Job state
has its own endpoints (openapi `Jobs` tag) — this links, it doesn't duplicate.
"""
import frappe
from frappe import _
from frappe.utils import cint


def _app_of(dotted):
	return (dotted or "").split(".", 1)[0]


def _flatten(handlers):
	return list(handlers) if isinstance(handlers, (list, tuple)) else [handlers]


@frappe.whitelist()
def list_automations(app=None, doctype=None, include_disabled=0):
	if "System Manager" not in frappe.get_roles():
		frappe.throw(_("Only System Manager can read the automation registry."), frappe.PermissionError)
	include_disabled = cint(include_disabled)

	# --- code layer (hooks) ---
	doc_events = []
	for dt, events in (frappe.get_hooks("doc_events") or {}).items():
		# When filtering by a doctype, keep the "*" wildcard too — those handlers
		# (assignment rule, workflow, etc.) DO fire on that doctype.
		if doctype and dt != doctype and dt != "*":
			continue
		if not isinstance(events, dict):
			continue
		for event, handlers in events.items():
			for h in _flatten(handlers):
				if app and _app_of(h) != app:
					continue
				doc_events.append({"doctype": dt, "event": event, "handler": h, "app": _app_of(h)})

	scheduler_events = []
	for freq, val in (frappe.get_hooks("scheduler_events") or {}).items():
		if isinstance(val, dict):  # cron: {expr: [handlers]}
			for expr, handlers in val.items():
				for h in _flatten(handlers):
					if app and _app_of(h) != app:
						continue
					scheduler_events.append({"frequency": f"{freq}:{expr}", "handler": h, "app": _app_of(h)})
		else:
			for h in _flatten(val):
				if app and _app_of(h) != app:
					continue
				scheduler_events.append({"frequency": freq, "handler": h, "app": _app_of(h)})

	def _overrides(hook):
		out = []
		for target, impl in (frappe.get_hooks(hook) or {}).items():
			handler = _flatten(impl)[-1]  # last app wins
			if app and _app_of(handler) != app:
				continue
			out.append({"target": target, "handler": handler, "app": _app_of(handler)})
		return out

	overrides = {
		"doctype_class": _overrides("override_doctype_class"),
		"whitelisted_methods": _overrides("override_whitelisted_methods"),
	}

	# --- config layer (DB); frappe.get_all bypasses user perms for introspection ---
	def _db(dt, fields, enabled_field, extra=None):
		filters = dict(extra or {})
		if not include_disabled and enabled_field:
			filters[enabled_field] = 0 if enabled_field == "disabled" else 1
		return frappe.get_all(dt, filters=filters, fields=fields)

	assignment_rules = _db(
		"Assignment Rule",
		["name", "document_type", "rule", "assign_condition", "disabled", "priority"],
		"disabled",
		{"document_type": doctype} if doctype else None,
	)
	server_scripts = _db(
		"Server Script",
		["name", "script_type", "reference_doctype", "doctype_event", "disabled"],
		"disabled",
		{"reference_doctype": doctype} if doctype else None,
	)
	notifications = _db(
		"Notification",
		["name", "document_type", "event", "channel", "enabled"],
		"enabled",
		{"document_type": doctype} if doctype else None,
	)

	return {
		"doc_events": doc_events,
		"scheduler_events": scheduler_events,
		"overrides": overrides,
		"assignment_rules": assignment_rules,
		"server_scripts": server_scripts,
		"notifications": notifications,
	}
