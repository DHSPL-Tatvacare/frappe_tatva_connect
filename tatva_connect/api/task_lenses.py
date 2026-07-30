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

Plan: docs/plans/task-form-layer/2026-07-25-task-slots-to-sections-and-form-layer.md §6 and §9 Phase 6.
"""

import frappe
from frappe.model.document import get_controller

from tatva_connect.list_engine import engine

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
	from crm.api.doc import sort_options as _native

	return _narrow(_native(doctype), doctype)


@frappe.whitelist()
def get_quick_filters(doctype: str, cached: bool = True):
	"""The fifth menu. It is not one of the four lenses — it reads `meta.fields` and `in_standard_filter`
	directly — so a derived field is appended in that endpoint's own shape and nothing native is narrowed."""
	from crm.api.doc import get_quick_filters as _native

	return [*_native(doctype, cached), *engine.quick_filter_fields(doctype)]


@frappe.whitelist()
def get_column_fields(doctype: str):
	"""The column lens. Its picker has no native endpoint, so this answers the declaration's set in the
	shape `ColumnSettings.vue`'s `fieldSource` prop already takes — the same narrowed list the filter
	lens returns, so all four menus have one source. An empty list for a doctype this layer does not
	narrow is that prop's own contract for "keep the stock doctype-meta source"."""
	if declared_fields(doctype) is None and not engine.lens_fields(doctype):
		return []
	return get_filterable_fields(doctype)
