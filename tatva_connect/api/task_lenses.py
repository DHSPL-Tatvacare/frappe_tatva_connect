# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The four `CRM Task` list lenses — filter, group-by, sort, columns — resolved through ONE declaration.

The native pickers walk raw `frappe.get_meta("CRM Task").fields`, so a rep is offered every column the
doctype carries — including the operational plumbing (the workflow token, the LSQ ids, the notified-for
stamps, the GPS capture, the ASM), none of which is a question a rep asks of a list.

There is no suppression list here, and there must never be one. `CRM Task.default_list_data()` is the
ONE declaration of the rep-facing set (plan §6); each lens is the native answer INTERSECTED with it.
The native call still decides fieldtype eligibility, labels, translation and standard fields — this
layer only drops what the declaration does not name. A field reaches the four menus by being declared
and by nothing else, so adding one is an edit to the declaration alone.

Only `CRM Task` is narrowed. Every other doctype is returned by the native function untouched, so the
CRM Lead, CRM Deal, FCRM Note and CRM Call Log pickers stay byte for byte what upstream ships.

Wired at `hooks.py` `override_whitelisted_methods`, the same seam as `api/list_link_titles.get_data`;
the fork stays dispatch-only. The column picker is the exception — it has no native endpoint, it reads
doctype meta in the browser (`stores/meta.js` `getFields`) — so `get_column_fields` IS that lens, fed
to `ColumnSettings.vue`'s existing `fieldSource` prop from `ViewControls.vue`.

The two menus that carry a VALUE control — Filter and the quick-filter bar — also say which scoped link
query each Link field's control must use, because a filter on a composite master is a cross-grain question
and the framework's own search answers it once per grain. That decision is `taxonomy.labels.link_query`'s
and is only relayed here.

The quick-filter bar is not a lens at all and is the other pair here: its contents are a STORED choice
(one `CRM Global Settings` row), so it is READ at the position that row gives and WRITTEN without the
Property Setter a derived name has no DocField to carry.

WHICH MENUS a derived field reaches is the declaration's own answer, not this module's: each of the five
asks `derived` for the fields that name ITS surface. A code declaration names none, which reads as all, so
this file behaves exactly as it did before a field could be authored in Desk.

Plan: docs/plans/task-form-layer/2026-07-25-task-slots-to-sections-and-form-layer.md §6 and §9 Phase 6.
"""

import frappe
from frappe.model.document import get_controller

from tatva_connect.lead import filters as lead_filters
from tatva_connect.list_engine import derived, engine
from tatva_connect.taxonomy import labels

TASK = "CRM Task"


def _link_queries(fields):
	"""Every Link at a composite master, told WHICH scoped query its filter control must use.

	A grain master keys one human value once per grain, so the framework's own link search offers `Not
	Interested` four times and each one matches a single programme. `taxonomy.labels.label_query` offers it
	once. Which masters those are is decided on THIS side — `labels.link_query` is the one decision — so the
	client is handed a query name and never a list of doctypes to reason about.

	A new dict per field rather than a stamp: native caches its quick-filter answer, and mutating those rows
	would write this into the cache."""
	return [
		{**f, "link_query": query}
		if f.get("fieldtype") == "Link" and (query := labels.link_query(f.get("options")))
		else f
		for f in fields
	]


def _scoped(fields, doctype):
	"""Both stamps a filter control needs: the query a composite master is searched by, and the values a
	grain axis may offer. One call, so a menu can never carry the first and miss the second."""
	return lead_filters.stamp_grain_options(_link_queries(fields), doctype)


def declared_fields(doctype):
	"""The rep-facing set a doctype declares for its list, or None when this layer does not narrow it.

	`default_list_data()["rows"]` is the declaration: `columns` is only the subset shown by default,
	`rows` is everything the list may offer. Read off the controller, never restated here."""
	if doctype != TASK:
		return None
	return set(get_controller(TASK).default_list_data().get("rows") or [])


def _offered(doctype, surface):
	"""The doctype's derived fields for ONE menu, shaped exactly as `engine.lens_fields` shapes them.

	`surfaces` is the declaration's own answer to which of the five menus offer it, so a field authored for
	Columns alone never turns up in Filter. A code declaration names no surface, which reads as ALL — so
	nothing that shipped before an operator could author a field moves by one byte."""
	on_surface = {f.fieldname for f in derived.for_doctype(doctype) if surface in f.surfaces}
	return [f for f in engine.lens_fields(doctype) if f["fieldname"] in on_surface]


def _narrow(fields, doctype, surface):
	"""The native answer, keeping only what the declaration names, plus the derived fields THIS menu offers.

	A derived field is not in `frappe.get_meta`, so no native lens can find it; it is offered here in the
	same dict shape a real field arrives in, which is what lets every picker treat it as ordinary."""
	declared = declared_fields(doctype)
	if declared is None:
		return [*fields, *_offered(doctype, surface)]
	kept = [f for f in fields if f.get("fieldname") in declared]
	return [*kept, *_offered(doctype, surface)]


@frappe.whitelist()
def get_filterable_fields(doctype: str):
	from crm.api.doc import get_filterable_fields as _native

	return _scoped(_narrow(_native(doctype), doctype, derived.FILTER), doctype)


@frappe.whitelist()
def get_group_by_fields(doctype: str):
	from crm.api.doc import get_group_by_fields as _native

	return _narrow(_native(doctype), doctype, derived.GROUP_BY)


@frappe.whitelist()
def sort_options(doctype: str):
	"""Every derived field is sortable, because a declaration is an ORDERED list of buckets and `_by_bucket`
	composes the page in that order. A declared `order_by` is only the tiebreaker WITHIN a bucket, so a
	field without one still sorts — its rows just fall back to the framework's own ordering inside each."""
	from crm.api.doc import sort_options as _native

	return _narrow(_native(doctype), doctype, derived.SORT)


def _declared_quick_filters(doctype):
	"""The derived fields that offer themselves to the BAR, by fieldname, in the shape this ONE endpoint
	hands the client. A declaration that withholds this surface is simply not resolvable here."""
	on_surface = {f.fieldname for f in derived.for_doctype(doctype) if derived.QUICK_FILTER in f.surfaces}
	return {f["fieldname"]: f for f in engine.quick_filter_fields(doctype) if f["fieldname"] in on_surface}


def _stored_choice(doctype):
	"""The chosen quick-filter fieldnames, read off the SAME `CRM Global Settings` row native reads.

	`None` means there is no row, which is native's `in_standard_filter` fallback and a different question
	entirely — not an empty choice."""
	row = frappe.db.exists("CRM Global Settings", {"dt": doctype, "type": "Quick Filters"})
	if not row:
		return None
	return frappe.parse_json(frappe.db.get_value("CRM Global Settings", row, "json")) or []


@frappe.whitelist()
def get_quick_filters(doctype: str, cached: bool = True):
	"""The fifth menu, and the only one that is a rep's STORED choice rather than a lens.

	Which fields get a control is one `CRM Global Settings` row (`dt`, type `Quick Filters`, a json list of
	fieldnames), and native resolves each chosen name through `frappe.get_meta` — which has never heard of a
	derived field, so the rep's chosen POSITION is unreachable by it. It is resolved here AT that position,
	off that same row; every real name is native's own entry, in native's own order, untouched. Appending
	instead would also put back a field the rep had just removed, because a removal is a shorter list.

	Absent that row native falls back to `in_standard_filter`, a DocField flag no derived field can carry.
	A derived field is then OFFERED — the picker lists it — and NOT APPLIED: it never reaches the bar
	unasked, which is this app's standing rule that nothing arrives switched on."""
	from crm.api.doc import get_quick_filters as _native

	native = _native(doctype, cached)
	declared = _declared_quick_filters(doctype)
	chosen = _stored_choice(doctype)
	if not declared or chosen is None:
		return _scoped(native, doctype)

	by_name = {f.get("fieldname"): f for f in native}
	return _scoped([
		declared[name] if name in declared else by_name[name]
		for name in chosen
		if name in declared or name in by_name
	], doctype)


@frappe.whitelist()
def update_quick_filters(quick_filters: str, old_filters: str, doctype: str):
	"""Recording the rep's choice is native's; stamping a Property Setter for a derived name is refused.

	`update_in_standard_filter` writes `<doctype>-<field>-in_standard_filter` for a field that has no DocField
	to carry it, and it fires unasked — the client seeds its list from this endpoint's answer, so a derived
	name is in `new_filters` on the first ever save even if the rep never touched it. A derived name is
	withheld from the pair native diffs, so every REAL fieldname keeps native's behaviour exactly — the
	removal write included; the choice is then recorded WITH it, since that row is what `get_quick_filters`
	resolves against.

	The withheld set is EVERY derived name on the doctype, not the ones this surface offers: a Property
	Setter describing a column that does not exist is wrong whichever menu the name reached the payload by."""
	from crm.api.doc import create_update_global_settings
	from crm.api.doc import update_quick_filters as _native

	declared = set(derived.names(doctype))
	if not declared:
		return _native(quick_filters, old_filters, doctype)

	chosen = frappe.parse_json(quick_filters) or []
	real = lambda listed: frappe.as_json([n for n in listed if n not in declared])  # noqa: E731
	_native(real(chosen), real(frappe.parse_json(old_filters) or []), doctype)
	create_update_global_settings(doctype, chosen)


@frappe.whitelist()
def get_column_fields(doctype: str):
	"""The column lens. Its picker has no native endpoint, so this answers the declaration's set in the
	shape `ColumnSettings.vue`'s `fieldSource` prop already takes — native's own filterable list, narrowed
	the same way the other three are, so all four menus have one source. An empty list for a doctype this
	layer does not narrow is that prop's own contract for "keep the stock doctype-meta source".

	It also feeds `KanbanSettings.vue`'s `fieldSource`, so the board's column picker is this surface too."""
	from crm.api.doc import get_filterable_fields as _native

	if declared_fields(doctype) is None and not _offered(doctype, derived.COLUMN):
		return []
	return _narrow(_native(doctype), doctype, derived.COLUMN)


@frappe.whitelist()
def declaration_version():
	"""The one string a client keys the five menus by. Tiny on purpose — it is a version, not a payload.

	Those menus are cached in IndexedDB with NO expiry (frappe-ui `resources.js` has no TTL anywhere, by
	design), so a rep who loaded the page before an operator authored a field would never be offered it.
	This is the mechanism that makes "live on Save" true on the client as well as the server."""
	return derived.declaration_version()
