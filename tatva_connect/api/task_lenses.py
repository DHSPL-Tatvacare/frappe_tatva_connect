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

The quick-filter bar is not a lens at all and is the other pair here: its contents are a STORED choice
(one `CRM Global Settings` row), so it is READ at the position that row gives and WRITTEN without the
Property Setter a derived name has no DocField to carry.

Plan: docs/plans/task-form-layer/2026-07-25-task-slots-to-sections-and-form-layer.md §6 and §9 Phase 6.
"""

import frappe
from frappe.model.document import get_controller

from tatva_connect.list_engine import derived, engine

TASK = "CRM Task"


def declared_fields(doctype):
	"""The rep-facing set a doctype declares for its list, or None when this layer does not narrow it.

	`default_list_data()["rows"]` is the declaration: `columns` is only the subset shown by default,
	`rows` is everything the list may offer. Read off the controller, never restated here."""
	if doctype != TASK:
		return None
	return set(get_controller(TASK).default_list_data().get("rows") or [])


def _narrow(fields, doctype):
	"""The native answer, keeping only what the declaration names, plus the doctype's derived fields.

	A derived field is not in `frappe.get_meta`, so no native lens can find it; it is offered here in the
	same dict shape a real field arrives in, which is what lets every picker treat it as ordinary."""
	declared = declared_fields(doctype)
	if declared is None:
		return [*fields, *engine.lens_fields(doctype)]
	kept = [f for f in fields if f.get("fieldname") in declared]
	return [*kept, *engine.lens_fields(doctype)]


@frappe.whitelist()
def get_filterable_fields(doctype: str):
	from crm.api.doc import get_filterable_fields as _native

	return _narrow(_native(doctype), doctype)


@frappe.whitelist()
def get_group_by_fields(doctype: str):
	from crm.api.doc import get_group_by_fields as _native

	return _narrow(_native(doctype), doctype)


@frappe.whitelist()
def sort_options(doctype: str):
	"""Every derived field is sortable, because a declaration is an ORDERED list of buckets and `_by_bucket`
	composes the page in that order. A declared `order_by` is only the tiebreaker WITHIN a bucket, so a
	field without one still sorts — its rows just fall back to the framework's own ordering inside each."""
	from crm.api.doc import sort_options as _native

	return _narrow(_native(doctype), doctype)


def _declared_quick_filters(doctype):
	"""The doctype's derived fields by fieldname, in the shape this ONE endpoint hands the client."""
	return {f["fieldname"]: f for f in engine.quick_filter_fields(doctype)}


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
		return native

	by_name = {f.get("fieldname"): f for f in native}
	return [
		declared[name] if name in declared else by_name[name]
		for name in chosen
		if name in declared or name in by_name
	]


@frappe.whitelist()
def update_quick_filters(quick_filters: str, old_filters: str, doctype: str):
	"""Recording the rep's choice is native's; stamping a Property Setter for a derived name is refused.

	`update_in_standard_filter` writes `<doctype>-<field>-in_standard_filter` for a field that has no DocField
	to carry it, and it fires unasked — the client seeds its list from this endpoint's answer, so a derived
	name is in `new_filters` on the first ever save even if the rep never touched it. A derived name is
	withheld from the pair native diffs, so every REAL fieldname keeps native's behaviour exactly — the
	removal write included; the choice is then recorded WITH it, since that row is what `get_quick_filters`
	resolves against."""
	from crm.api.doc import create_update_global_settings
	from crm.api.doc import update_quick_filters as _native

	declared = _declared_quick_filters(doctype)
	if not declared:
		return _native(quick_filters, old_filters, doctype)

	chosen = frappe.parse_json(quick_filters) or []
	real = lambda listed: frappe.as_json([n for n in listed if n not in declared])  # noqa: E731
	_native(real(chosen), real(frappe.parse_json(old_filters) or []), doctype)
	create_update_global_settings(doctype, chosen)


@frappe.whitelist()
def get_column_fields(doctype: str):
	"""The column lens. Its picker has no native endpoint, so this answers the declaration's set in the
	shape `ColumnSettings.vue`'s `fieldSource` prop already takes — the same narrowed list the filter
	lens returns, so all four menus have one source. An empty list for a doctype this layer does not
	narrow is that prop's own contract for "keep the stock doctype-meta source"."""
	if declared_fields(doctype) is None and not engine.lens_fields(doctype):
		return []
	return get_filterable_fields(doctype)
