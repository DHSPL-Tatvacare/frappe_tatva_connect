"""CRM Call Log visibility — thin consumer of the shared row-visibility brain.

crm scopes Leads/Deals but never their call logs, so an agent otherwise sees every call.
This restores parity: a call log is visible iff its parent Lead/Deal is, or it is the
user's own/assigned. Policy lives once in `access/visibility.py`; these are just the hook
entry points hooks.py points at, delegating for "CRM Call Log".
"""
from tatva_connect.access import visibility


def get_call_log_permission_query_conditions(user=None):
	return visibility.scoped_pqc("CRM Call Log", user)


def has_call_log_permission(doc, ptype, user):
	return visibility.scoped_has_permission(doc, ptype, user)
