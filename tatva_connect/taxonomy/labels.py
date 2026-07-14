# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Resolve a Link value to the label a human reads: the target doctype's `title_field`.

Six doctypes are named by a `format:` autoname built from the grain, so their primary key is data:
`CRM Task Type` is `{vertical}::{group}::{program}::{type_name}`. Printing that key gives a rep
`GoodFlip Care::Anaya::::Identify PSP Category`. The clean name is the doctype's own `title_field`,
read from meta, never a hardcoded doctype/field pair.

Where a payload carries the key AND the label, the contract is:

    <key>        the primary key. The client filters, saves and looks up config on it. It stays.
    <key>_label  the title_field. Display, search, sort.

Never replace the key with the label in a payload the client filters or groups by: two stages that
share a name across programs are distinct keys but one label, so the filter matches nothing and the
group-by merges them. The SPA list does not use this module at all; it uses the framework's own
`_link_titles` map (see api/list_link_titles.py), which ships the label alongside the untouched key.
This module is for the hand-built payloads that have no such map.

`api.list_link_titles._resolve_title` calls `title_of` for the lookup, so there is one implementation
of "read the title_field"; it adds the framework's `show_title_field_in_link` and read-permission
gates on top, which are the list map's semantics, not this module's.

Permissions: `frappe.get_cached_value` does not check them, matching the `frappe.db.get_value` calls
this replaced. Callers have already gated the record the value was read off.
"""
import frappe

# The grain masters whose primary key is a composite. Named here so no call site spells them itself.
TASK_TYPE = "CRM Task Type"
LEAD_STAGE = "CRM Lead Stage"


def title_of(doctype, value):
	"""The target's title_field for one value, or None when there is no title to read. Never raises:
	a bad doctype or a title_field naming a missing column degrades to no-label, never a 500."""
	if not (doctype and value):
		return None
	try:
		meta = frappe.get_meta(doctype)
		field = meta.title_field
		if not field or field == "name":
			return None
		return frappe.get_cached_value(doctype, value, field) or None
	except Exception:
		frappe.log_error(f"labels: cannot read title_field of {doctype}", frappe.get_traceback())
		return None


def label(value, doctype):
	"""The label for one value, falling back to the raw value so a field never blanks."""
	if not value:
		return ""
	return title_of(doctype, value) or value


def labels(values, doctype):
	"""{pk: label} for a row projection. Deduped, so a 100-row page of 5 distinct stages costs 5 cached
	reads, not 100. Build this once above the loop rather than calling `label()` inside it."""
	wanted = {v for v in (values or []) if v}
	return {v: label(v, doctype) for v in wanted}


def shown(doctype, fieldname, value):
	"""A field's value as a human should read it: a Link to a grain master holds a composite key, so
	show the target's title. Any other fieldtype passes through untouched. For the callers that hold a
	doctype and a fieldname rather than a Link target: notifications, WhatsApp params, rule previews."""
	if not (doctype and fieldname and isinstance(value, str) and value):
		return value
	try:
		df = frappe.get_meta(doctype).get_field(fieldname)
	except Exception:
		return value
	if not df or df.fieldtype != "Link" or not df.options:
		return value
	return label(value, df.options)
