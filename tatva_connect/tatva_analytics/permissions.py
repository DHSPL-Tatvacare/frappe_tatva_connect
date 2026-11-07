"""Fact visibility: the hook entry points hooks.py points at, delegating to the shared row-visibility brain (`access/visibility.py`)."""
from tatva_connect.access import visibility


def get_lead_allocation_permission_query_conditions(user=None):
	return visibility.scoped_pqc("Tatva Lead Allocation Fact", user)


def get_shift_activity_permission_query_conditions(user=None):
	return visibility.scoped_pqc("Tatva Shift Activity Fact", user)


def has_fact_permission(doc, ptype, user):
	return visibility.scoped_has_permission(doc, ptype, user)
