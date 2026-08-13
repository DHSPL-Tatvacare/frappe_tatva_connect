"""Workflow journey visibility — a thin consumer of the shared row-visibility brain.

A `CRM Workflow Journey` carries its lead's field values in `state_json`, an event carries them in
`payload_json`, and a step log carries them in `detail`. None of them were scoped, so any user who could
open the workflow lists could read every other grain's lead data — bypassing the entitlement the rest of
the app enforces.

Policy lives once in `access/visibility.py`; these are only the hook entry points.
"""
from tatva_connect.access import visibility


def get_workflow_permission_query_conditions(user=None):
	"""The Definition itself. HOW it is scoped — by the grain it declares, because it has no parent lead —
	is declared in `visibility.SCOPED`, not decided here; these stay one line each, like their siblings."""
	return visibility.scoped_pqc("CRM Workflow", user)


def has_workflow_permission(doc, ptype, user):
	return visibility.scoped_has_permission(doc, ptype, user)


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


# The Workflows surface gate lives in access/surfaces.py with every other one; this endpoint answered only its permission half and would have drifted the day the liveness switch shipped.
