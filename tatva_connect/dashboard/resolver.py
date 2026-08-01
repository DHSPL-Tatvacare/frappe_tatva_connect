# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt

"""Which dashboard does this person get? One question, one answer.

A role profile grants several roles at once, so matching by role always matches several: `priority desc,
role asc` makes the pick deterministic. No match is None, and None is an answer — a role nobody configured
has no dashboard rather than somebody else's.
"""

import frappe
from frappe.utils.caching import request_cache

from tatva_connect.dashboard import declaration


def layout_for(user=None):
	"""The one layout this person's roles grant, or None."""
	return _resolve(user or frappe.session.user)


@request_cache
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
	if not rows:
		return None
	# get_list settles WHICH layout wins; the placements are child rows, and loading the document is how
	# frappe reads those. Cached, and an operator's save invalidates it — no second cache to keep in step.
	doc = frappe.get_cached_doc(declaration.LAYOUT, rows[0]["name"])
	layout = dict(rows[0])
	layout["charts"] = [
		{field: row.get(field) for field in declaration.PLACEMENT} for row in doc.charts
	]
	return layout
