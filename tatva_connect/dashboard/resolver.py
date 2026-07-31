# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt

"""Which dashboard does this person get? One question, one answer, asked in one place.

Every real role profile on this site grants several roles at once — a CRM Field Manager holds CRM User,
CRM Field User, CRM Manager and CRM Manager Lite — so matching a layout by role matches SEVERAL, always.
`priority desc, role asc` makes the pick deterministic: the highest priority wins, and an operator who
leaves two at the same priority still gets the same answer on every load rather than whatever the
database felt like returning.

NO MATCH IS `None`, AND `None` IS AN ANSWER. A role nobody has configured has no dashboard; it does not
inherit somebody else's. Falling back to a manager layout would show a rep a manager's cards — narrowed
to the rep's own rows, so nothing would leak, but a dashboard nobody chose for them all the same.

The read is operator CONFIG, not user data: which cards a role is shown is not a secret from the people
holding that role, and the row gate that matters is applied to every chart's own query in `executor.py`.
"""

import frappe

from tatva_connect.access import request_cache

LAYOUT_DOCTYPE = "CRM Dashboard Layout"

_FIELDS = ("name", "role", "title", "priority", "layout", "exposed_filters")


def layout_for(user=None):
	"""The one layout this person's roles grant, or None. Request-cached per user."""
	user = user or frappe.session.user
	return request_cache("tatva_connect:dashboard_layout", user, lambda: _resolve(user))


def _resolve(user):
	roles = frappe.get_roles(user)
	if not roles:
		return None
	rows = frappe.get_list(
		LAYOUT_DOCTYPE,
		filters={"enabled": 1, "role": ["in", roles]},
		fields=list(_FIELDS),
		# Stated in both directions: an unstated direction flips to ASC under db_query_compat.
		order_by="priority desc, role asc",
		limit=1,
		ignore_permissions=True,  # authz-ok: tier-c — reads operator dashboard config for the session user's own roles
	)
	return dict(rows[0]) if rows else None
