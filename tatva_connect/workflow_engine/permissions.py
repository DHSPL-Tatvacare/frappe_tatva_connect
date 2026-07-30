"""Workflow journey visibility — a thin consumer of the shared row-visibility brain.

A `CRM Workflow Journey` carries its lead's field values in `state_json`, an event carries them in
`payload_json`, and a step log carries them in `detail`. None of them were scoped, so any user who could
open the workflow lists could read every other grain's lead data — bypassing the entitlement the rest of
the app enforces.

Policy lives once in `access/visibility.py`; these are only the hook entry points.
"""
import frappe

from tatva_connect.access import visibility


def get_journey_permission_query_conditions(user=None):
	return visibility.scoped_pqc("CRM Workflow Journey", user)


def has_journey_permission(doc, ptype, user):
	return visibility.scoped_has_permission(doc, ptype, user)


def get_signal_permission_query_conditions(user=None):
	return visibility.scoped_pqc("CRM Workflow Signal", user)


def has_signal_permission(doc, ptype, user):
	return visibility.scoped_has_permission(doc, ptype, user)


def get_step_log_permission_query_conditions(user=None):
	return visibility.scoped_pqc("CRM Workflow Step Log", user)


def has_step_log_permission(doc, ptype, user):
	return visibility.scoped_has_permission(doc, ptype, user)


@frappe.whitelist()
def workflow_access():
	"""May this user author workflows? The sidebar link asks before it renders.

	Answered by `has_permission` on the doctype, never by a hardcoded role list: the DocPerms ARE the
	declaration, and roles are only how they are granted. A role list here would be a second answer to
	the same question and would drift the day an operator adds a role.

	Mirrors `near_me.api.near_me_access` — one server rule authorising both the page and its link, so a
	link can never appear for a page that will refuse.
	"""
	return {"visible": bool(frappe.has_permission("CRM Workflow", "read"))}
