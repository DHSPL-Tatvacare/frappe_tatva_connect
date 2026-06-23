"""FCRM Note visibility — thin consumer of the shared row-visibility brain.

crm scopes Leads/Deals but never the notes pinned to them, so an agent otherwise sees
every team note. This restores parity: a note is visible iff its parent Lead/Deal is, or
it is the user's own record. Policy lives once in `access/visibility.py`; these are just
the hook entry points hooks.py points at, delegating for "FCRM Note".
"""
from tatva_connect.access import visibility


def get_note_permission_query_conditions(user=None):
	return visibility.scoped_pqc("FCRM Note", user)


def has_note_permission(doc, ptype, user):
	return visibility.scoped_has_permission(doc, ptype, user)
