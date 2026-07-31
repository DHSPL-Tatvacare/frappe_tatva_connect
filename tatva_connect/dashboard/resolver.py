# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt

"""Which dashboard does this person get? One question, one answer.

A role profile grants several roles at once, so matching by role always matches several: `priority desc,
role asc` makes the pick deterministic. No match is None, and None is an answer — a role nobody configured
has no dashboard rather than somebody else's.
"""

import frappe

from tatva_connect.access import request_cache
from tatva_connect.dashboard import declaration


def layout_for(user=None):
	"""The one layout this person's roles grant, or None. Request-cached per user."""
	user = user or frappe.session.user
	return request_cache("tatva_connect:dashboard_layout", user, lambda: _resolve(user))


def _resolve(user):
	roles = frappe.get_roles(user)
	if not roles:
		return None
	rows = frappe.get_list(
		declaration.LAYOUT,
		filters={"enabled": 1, "role": ["in", roles]},
		fields=list(declaration.LAYOUT_FIELDS),
		# Stated in both directions: an unstated direction flips to ASC under db_query_compat.
		order_by="priority desc, role asc",
		limit=1,
		ignore_permissions=True,  # authz-ok: tier-c — reads operator dashboard config for the session user's own roles
	)
	return dict(rows[0]) if rows else None
