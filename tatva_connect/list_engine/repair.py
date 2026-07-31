# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A listing request that names a field the list does not have is REPAIRED, not answered with a crash.

THE CASE THIS EXISTS FOR. A saved view stores fieldnames. A derived field is not stored on any record, so
retiring one is an operator deleting a row — and every view that referenced it is left naming a column
that no longer exists anywhere. `frappe.get_meta` has never heard of it, `DatabaseQuery` throws
`Unknown column`, and the client persists its params BEFORE the request, so the filter comes back on the
next load and the next: the rep is locked out of their own list with no control to clear it.

THE ONE PATH, and it is the same path for any field that ever goes away — a Customize Form deletion, a
renamed column, an app uninstalled. Nothing here is about derived fields:

    1. The request names a field this list cannot serve.
    2. It is dropped from the request, so the page renders.
    3. It is dropped from the rep's SAVED VIEW, so it does not come back tomorrow.
    4. The answer carries `removed_fields`, so the client says so once instead of failing in silence.

Each rep repairs their own view on their own next load, so the cleanup cascades without a sweep, without
a patch and without a migration. There is no registry of retired names and there must never be one: a
name is unserveable because the LIST does not have it, which is knowable from `frappe.get_meta` alone.

WHY DROPPING A FILTER IS THE HONEST ANSWER. It widens what the rep sees, which this app otherwise never
does silently — but the alternative is not a narrower list, it is NO list. The request is already
unanswerable, and it is answered today with a traceback. `removed_fields` is what keeps it from being
silent, and it is the client's obligation to show it.

INERT BY CONSTRUCTION. A request naming nothing unknown is returned as the very object it came in as, so
a healthy list is byte for byte what it was before this module existed.

Plan: docs/plans/tasks-ui/2026-07-31-derived-field-head.md
"""

import frappe
from frappe.model import default_fields, optional_fields, std_fields

from tatva_connect.list_engine import derived

VIEW_DOCTYPE = "CRM View Settings"

# THE ONE DECLARATION of where a fieldname can appear in a payload; a sixth key is added HERE, once.
DICT_KEYS = ("filters", "default_filters")
LIST_KEYS = ("rows", "kanban_fields")

# Keys holding ONE bare fieldname. Each arrives top-level, and the first two also inside the saved view.
_SINGLE = ("column_field", "group_by_field", "title_field")

# The saved-view columns that hold a fieldname, by the request key that carries the same thing.
_VIEW_COLUMNS = ("filters", "order_by", "columns", "rows", "kanban_fields")


def _known(doctype):
	"""Every fieldname this list can really serve: its own columns, the framework's, and the live
	declarations. A derived field is `frappe.get_meta`'s blind spot and the only reason this is not one
	call — everything else here is the framework's own answer to what a column is."""
	meta = frappe.get_meta(doctype)
	return (
		{df.fieldname for df in meta.fields}
		| set(default_fields)
		| set(optional_fields)
		| {f["fieldname"] for f in std_fields}
		| set(derived.names(doctype))
	)


def _bare(term):
	"""One `order_by` term as the fieldname it sorts on, or None when it is not a bare name.

	A qualified or quoted term (`` `tabCRM Task`.modified ``) is left entirely alone: it is not something a
	rep's field picker produced, and this module refuses to guess at anything it did not recognise."""
	name = str(term).strip().split(" ")[0]
	return name if name.isidentifier() else None


def named_in(kwargs, view):
	"""EVERY fieldname a listing payload references, wherever it references one. The ONE enumeration.

	The engine asks this to find the derived fields a request names and this module asks it to find the
	names the list cannot serve — two different questions off one answer. Written twice they drift, and the
	drift is silent both ways: a key the engine reads and the janitor does not leaves a broken view
	un-repaired, and the reverse drops a field the engine was about to serve.

	Only BARE names are returned. A qualified or expression-shaped key is not something a rep's field
	picker produced, and guessing wrong costs a filter dropped from a list that was working."""
	said = set()
	for key in DICT_KEYS:
		said.update(frappe.parse_json(kwargs.get(key) or "{}") or {})
	for key in LIST_KEYS:
		said.update(frappe.parse_json(kwargs.get(key) or "[]") or [])
	said.update(
		c.get("key") or c.get("fieldname")
		for c in (frappe.parse_json(kwargs.get("columns") or "[]") or [])
		if isinstance(c, dict)
	)
	said.update(_bare(t) for t in str(kwargs.get("order_by") or "").split(",") if t.strip())
	for key in _SINGLE:
		said.add(kwargs.get(key))
		said.add((view or {}).get(key))
	return {name for name in said if isinstance(name, str) and name.isidentifier()}


def unserveable(doctype, kwargs, view):
	"""The fieldnames this request names that the list cannot serve, in a stable order."""
	known = _known(doctype)
	return sorted(name for name in named_in(kwargs, view) if name not in known)


def _like(original, value):
	"""One key written back in the SHAPE it arrived in.

	The wire sends JSON strings and a python caller sends the objects; native reads whichever it was
	handed and `crm/api/doc.py:273` builds a `_dict` straight off `filters`. Handing back a string where an
	object came in turned a repaired filter into a per-character update sequence."""
	return frappe.as_json(value) if isinstance(original, str) else value


def without(kwargs, gone):
	"""The payload with every unserveable name taken out of every key that could carry it."""
	kwargs = dict(kwargs)
	for key in DICT_KEYS:
		given = frappe.parse_json(kwargs.get(key) or "{}") or {}
		if set(given) & gone:
			kwargs[key] = _like(kwargs.get(key), {k: v for k, v in given.items() if k not in gone})
	for key in LIST_KEYS:
		listed = frappe.parse_json(kwargs.get(key) or "[]") or []
		if set(listed) & gone:
			kwargs[key] = _like(kwargs.get(key), [name for name in listed if name not in gone])
	columns = frappe.parse_json(kwargs.get("columns") or "[]") or []
	if any(isinstance(c, dict) and (c.get("key") or c.get("fieldname")) in gone for c in columns):
		kwargs["columns"] = _like(
			kwargs.get("columns"),
			[
				c
				for c in columns
				if not (isinstance(c, dict) and (c.get("key") or c.get("fieldname")) in gone)
			],
		)
	terms = [t.strip() for t in str(kwargs.get("order_by") or "").split(",") if t.strip()]
	if any(_bare(t) in gone for t in terms):
		# Empty, never None: native declares `order_by: str`, and the framework then applies its own default.
		kwargs["order_by"] = ", ".join(t for t in terms if _bare(t) not in gone)
	for key in _SINGLE:
		if kwargs.get(key) in gone:
			kwargs[key] = None
	view = frappe.parse_json(kwargs.get("view") or "{}") or {}
	if any(view.get(key) in gone for key in _SINGLE):
		# A board's columns ARE the removed field's values, so they go with it rather than becoming phantoms.
		kwargs["kanban_columns"] = _like(kwargs.get("kanban_columns"), [])
		kwargs["view"] = _like(
			kwargs.get("view"), {k: (None if k in _SINGLE and v in gone else v) for k, v in view.items()}
		)
	return kwargs


def saved_view(doctype, view):
	"""The row this request was drawn from — the rep's named view, else their own standard one for this
	view type, which is how `create_or_update_standard_view` itself addresses it."""
	named = view.get("custom_view_name")
	if named and frappe.db.exists(VIEW_DOCTYPE, named):
		return named
	return frappe.db.exists(
		VIEW_DOCTYPE,
		{
			"dt": doctype,
			"type": view.get("view_type") or "list",
			"is_standard": 1,
			"user": frappe.session.user,
		},
	)


def repair_view(name, gone=None):
	"""Take every unserveable name out of ONE stored view, through the doctype, and say whether it moved.

	THE ONLY WRITE IN THIS MODULE, and every gate calls this one — a rep's configuration is not edited on a
	whim, it is edited when a field it names has been deleted or disabled and is blocking the surface it is
	on. `doc.save()` rather than a direct column write, so `modified` moves and `CRM View Settings`
	(`track_changes: 1`) records a Version row: the edit is on the record, attributable and reversible.

	Best effort: a rep looking at someone else's public view may not be allowed to write it, and the page
	still has to render. Whoever CAN write it repairs it for everyone on their next load."""
	if not name:
		return False
	try:
		doc = frappe.get_doc(VIEW_DOCTYPE, name)
		# No bypass: a rep repairs their own view under the permission engine's own answer.
		if not doc.has_permission("write"):
			return False
		gone = set(gone) if gone is not None else set(unserveable(doc.dt, _stored(doc), {}))
		if not gone:
			return False
		stored = _stored(doc)
		repaired = without(stored, gone)
		if repaired == stored:
			return False
		for key in (*_VIEW_COLUMNS, *_SINGLE):
			doc.set(key, repaired.get(key))
		if any(stored.get(key) in gone for key in _SINGLE):
			# A board's columns ARE the removed field's values, so they go with it rather than becoming phantoms.
			doc.kanban_columns = "[]"
		doc.save()
		return True
	except Exception as unrepairable:
		# The surface renders either way; a view that refuses to save is a thing to log, not a list to take down.
		frappe.log_error(title="Saved view not repaired", message=f"{VIEW_DOCTYPE} {name}: {unrepairable}")
		return False


def _stored(doc):
	"""One saved view as the payload shape `without` reads, so the row and the request are repaired by
	the SAME function and cannot drift into two answers about what naming a field means."""
	return {
		"filters": doc.get("filters") or "{}",
		"order_by": doc.get("order_by") or "",
		"columns": doc.get("columns") or "[]",
		"rows": doc.get("rows") or "[]",
		"kanban_fields": doc.get("kanban_fields") or "[]",
		**{key: doc.get(key) for key in _SINGLE},
	}


def scrub(kwargs):
	"""The payload this list can answer, and the names taken out of it to get there.

	Returns the caller's own object untouched when there is nothing to repair, which is every request on a
	healthy site."""
	doctype = kwargs.get("doctype")
	if not doctype:
		return kwargs, []
	view = frappe.parse_json(kwargs.get("view") or "{}") or {}
	gone = unserveable(doctype, kwargs, view)
	if not gone:
		return kwargs, []
	frappe.log_error(
		title="Listing field no longer exists",
		message=f"{doctype}: {gone} removed from the request and from the saved view",
	)
	repair_view(saved_view(doctype, view), gone)
	return without(kwargs, set(gone)), gone


def views_using(dt, fieldname):
	"""How many saved views name this field, and how many people they belong to.

	The config form asks this before an operator retires a declaration, so "this is in use" is a number they
	can act on rather than a warning they learn to click past. A view counts as using the field if
	REPAIRING it would change it — the same function the janitor runs, so the warning and the cleanup can
	never disagree about what naming a field means."""
	rows = frappe.get_all(
		VIEW_DOCTYPE,
		fields=["name", "user", *_VIEW_COLUMNS, *_SINGLE],
		filters={"dt": dt},
		limit=0,
	)
	using = [row for row in rows if without(_stored(row), {fieldname}) != _stored(row)]
	return {"views": len(using), "users": len({row.user for row in using if row.user})}
