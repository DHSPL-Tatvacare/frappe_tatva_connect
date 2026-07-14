# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""THE resolver for a composite-PK Link value -> the label a human reads. One rule, one place.

Six doctypes are named by a `format:` autoname built from the grain, so their PRIMARY KEY is data:
`CRM Task Type` is `{vertical}::{group}::{program}::{type_name}`. The clean name a human wants is the
doctype's own `title_field` — `type_name` there, `display_label` on CRM Lead Stage. That is the whole
rule, and reading it from META (never a hardcoded doctype/field pair) is what keeps it ONE rule: a new
composite doctype needs no edit here, and a renamed title_field cannot leave a stale copy behind.

THE CONTRACT every payload follows. It was already written down at `activity.api.lead_task_board` and
is now the law everywhere:

    <key>        the composite PK. The client filters, saves and looks up config on it. It STAYS.
    <key>_label  the clean title_field. Display, search, sort.

NEVER conflate the two. Replacing the PK with its label breaks the client silently (it filters and
saves on the key); shipping the PK without a label is what put `GoodFlip Care::Anaya::::Identify PSP
Category` in front of a rep. Both halves are load-bearing. An option list uses frappe's own
`{name, label}` shape for the same pair.

Why this module exists at all: the rule was implemented FOUR times — twice hardcoding
`get_value("CRM Task Type", ..., "type_name")`, once off meta in `lead.detail._display_label`, once in
`save_activity`'s title — and forgotten in eight payloads. Not because anyone chose to skip it, but
because the only resolver lived in a lead-detail module and demanded a DOCFIELD, which a hand-rolled
`frappe.get_all` payload does not have. So the brain now takes what the call sites actually hold: a
value and a doctype. `tests/api/test_composite_pk_labels.py` fails the build on any read endpoint that
leaks a naked PK, deriving the doctype set from `autoname` at runtime.

Permissions: `frappe.get_all` does not check them — the same posture as the `frappe.db.get_value`
calls this replaces. A label is not a read grant, and every caller has already gated the record the
value was read off.
"""
import frappe


def label(value, doctype):
	"""The clean label for ONE composite-PK value. Falls back to the raw value when the doctype has no
	title_field or the row is gone — a missing label must never blank a field."""
	if not value:
		return ""
	return labels([value], doctype).get(value, value)


def labels(values, doctype):
	"""{pk -> label} for many values in ONE query. Use this when projecting a list of rows; calling
	`label()` per row is a query per row, and that N+1 is half of why this module exists."""
	wanted = {v for v in (values or []) if v}
	if not wanted:
		return {}
	title_field = _title_field(doctype)
	if not title_field:
		return {v: v for v in wanted}
	resolved = {v: v for v in wanted}
	for row in frappe.get_all(doctype, filters={"name": ["in", list(wanted)]},
	                          fields=["name", title_field]):
		resolved[row["name"]] = row.get(title_field) or row["name"]
	return resolved


def _title_field(doctype):
	"""The doctype's own title_field, or None when it has none (then the PK is all there is to show)."""
	try:
		meta = frappe.get_meta(doctype)
	except Exception:
		return None
	return meta.title_field if meta.title_field and meta.title_field != "name" else None
