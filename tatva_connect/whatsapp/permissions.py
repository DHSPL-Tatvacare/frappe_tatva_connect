"""WhatsApp Message visibility — thin consumer of the shared row-visibility brain.

crm scopes Leads/Deals but never the WhatsApp messages threaded onto them, so an agent
otherwise sees every conversation. This restores parity: a message is visible iff its
parent Lead/Deal is, or it is the user's own record. Policy lives once in
`access/visibility.py`; these are just the hook entry points hooks.py points at,
delegating for "WhatsApp Message".
"""
from tatva_connect.access import visibility


def get_whatsapp_message_permission_query_conditions(user=None):
	return visibility.scoped_pqc("WhatsApp Message", user)


def has_whatsapp_message_permission(doc, ptype, user):
	return visibility.scoped_has_permission(doc, ptype, user)
