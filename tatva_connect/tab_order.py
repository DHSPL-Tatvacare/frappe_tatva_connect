# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""THE tab-order seam: the order ONE person arranged a set of tabs into.

NO DOCTYPE. Frappe already ships a per-user preference store and Desk itself keeps list filters, page
length and last-view in it: `frappe.model.utils.user_settings`, keyed by `(session user, doctype)`,
cached in redis and flushed to `__UserSettings`. "Where the tabs sit on MY screen" is precisely that,
so this is ~40 lines over a native store rather than a new table, a migrate and a drift-gate row.
The write follows `desk/doctype/event/event.py:234-235` — update, then sync so it survives the cache.

GENERIC BY CONSTRUCTION, like `presets.py`: the surface is named by a `reference_doctype`, so one door
serves the Smart View strip today and any other reorderable listing later. Nothing here knows what a
Smart View is.

PERSONAL BY CONSTRUCTION, which is the point. The store keys on the session user itself — there is no
`user` parameter to forget to gate. It is deliberately NOT `CRM Smart View.view_order`: that column is
a fact about the VIEW and writing it needs `can_write`, which every rep fails on every standard view,
so a drag handle there would always refuse.

AN ORDER IS A HINT, NEVER A FILTER. `apply` returns every row it was given: a name the order does not
mention sorts to the end, one that no longer resolves is ignored. A view created, shared or unshared
after the arrangement was saved can never vanish from the strip because of it.
"""
import frappe
from frappe import _
from frappe.model.utils.user_settings import get_user_settings, sync_user_settings, update_user_settings

# The one key inside this person's settings for that doctype. Named, so it sits beside whatever else
# frappe or Desk keeps there and a write can never clobber a neighbour.
KEY = "tab_order"


def _assert_may_use(reference_doctype):
	"""The one gate: you may arrange a surface you can open. Fail-closed."""
	if not reference_doctype:
		frappe.throw(_("An order needs a surface."))
	if not frappe.has_permission(reference_doctype, "read"):
		frappe.throw(_("Not permitted."), frappe.PermissionError)


def get_order(reference_doctype):
	"""This person's arrangement for one surface, as a list of names. [] when they have none."""
	try:
		settings = frappe.parse_json(get_user_settings(reference_doctype)) or {}
		return [str(n) for n in (settings.get(KEY) or [])]
	except Exception:
		frappe.write_only()(frappe.log_error)(title="tab_order: unreadable user settings",
		                                      message=f"{reference_doctype} / {frappe.session.user}")
		return []


def apply(rows, reference_doctype, key="name"):
	"""`rows` in this person's order — the ONE reader, so every surface arranges the same way.

	Stable: rows the order names come first in that order, everything else keeps the order it arrived
	in, which is the server's own. A row is never dropped."""
	order = get_order(reference_doctype)
	if not order:
		return rows
	rank = {name: i for i, name in enumerate(order)}
	return sorted(rows, key=lambda r: rank.get(r.get(key), len(rank)))


@frappe.whitelist()
def save_order(reference_doctype, order):
	"""Remember this arrangement. Replaces it wholesale — an order is one fact, not a set of rows.

	`sync_user_settings` right after the update, the shape `event.py` uses: without it the arrangement
	lives only in redis until the scheduled sweep, and a cache flush loses what someone just dragged."""
	_assert_may_use(reference_doctype)
	order = frappe.parse_json(order) if isinstance(order, str) else (order or [])
	if not isinstance(order, list):
		frappe.throw(_("An order must be a list of names."))
	update_user_settings(reference_doctype, {KEY: [str(n) for n in order]})
	sync_user_settings()
	return {"saved": True, "count": len(order)}
