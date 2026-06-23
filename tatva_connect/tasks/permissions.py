"""CRM Task visibility — thin consumer of the shared row-visibility brain.

The policy (a task is visible iff its parent Lead/Deal is, or it's the user's own/assigned)
now lives once in `access/visibility.py`, driven per-doctype by its registry. These two
functions are the hook entry points hooks.py points at; they just delegate for "CRM Task".

Archive trace (invariant 14): the standalone CRM Task implementation that used to live here
(get_task_permission_query_conditions / has_task_permission with its own PARENT_DOCTYPES +
_visible_parent_subquery) folded into access/visibility.py as the "CRM Task" registry entry.
Behaviour is identical; there is no second copy.
"""
from tatva_connect.access import visibility


def get_task_permission_query_conditions(user=None):
	return visibility.scoped_pqc("CRM Task", user)


def has_task_permission(doc, ptype, user):
	return visibility.scoped_has_permission(doc, ptype, user)
