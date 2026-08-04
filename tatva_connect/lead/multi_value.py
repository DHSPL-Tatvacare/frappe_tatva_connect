# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The ONE reader and the ONE writer of a lead field that takes more than one value.

A `Table MultiSelect` on a doctype whose `istable` is 1 is a SECOND level of child rows, and Frappe
persists one: `frappe/model/base_document.py:62` assigns `TABLE_DOCTYPES_FOR_CHILD_TABLES` — an empty
map — to every child row through `_init_child`, so a child row is told it has no table fields of its
own. It neither loads nor saves. Every consumer that tried was reading a column that answers nothing.

So the selections come one level UP, onto the lead itself, as `CRM Lead Multi Value` rows addressed by
`(field_key, row_key)`:

  * `field_key` is a Link into `CRM Lead API Field`, so WHICH field is the catalog's answer, not a
    string this module parses. The section, the label and the column name are read off that row.
  * `row_key` is the value of the section's own `row_key_field` for the row the selection belongs to —
    a cycle date, a report date. BLANK IS A REAL ADDRESS: a section keeping one row per lead declares
    no row key at all, and a multi-row section is free to hold a row that names none.
  * `value` stays a real Link to `CRM Picklist Value`, so the vocabulary, the grain scoping and
    `labels.title_of` are the same ones every other picklist field on the lead already rides.

`is_multi_value` on the catalog row is the WHOLE declaration. Nothing here names a field, a section or
a doctype, so the next multi-value field is a catalog row and never a schema change.

Both calls take the lead DOCUMENT, not its name. Every consumer already holds it — the Data tab reads
it, the partner API builds it, the loader saves it — and its child tables came with it, so `read_all`
costs no query at all and `replace` stages onto the same doc the caller is about to save. That also
means the write rides the caller's own `doc.save()`: one permission check, one validate, one set of
doc_events, and a rollback takes the selections with it.
"""
import frappe
from frappe.utils import cstr

# The Table field on CRM Lead holding every multi-value selection, whichever field declared it.
TABLE = "custom_multi_values"
DOCTYPE = "CRM Lead Multi Value"


def declared():
	"""{(section, fieldname): field_key} — every field the catalog declares multi-value.

	THE one reading of `is_multi_value`. Read live, because the declaration is operator data: a field
	becomes multi-value the moment the box is ticked, with nothing to regenerate. Every consumer asks
	here — the partner catalog builds its cached projection off this, the loader keys it by child table
	— so no module decides for itself what a multi-value field is."""
	rows = frappe.get_all(
		"CRM Lead API Field", filters={"is_multi_value": 1},
		fields=["field_key", "section", "fieldname"],
	)
	return {(r.section, r.fieldname): r.field_key for r in rows}


def declared_in(section):
	"""{fieldname: field_key} for ONE section — the same declaration, narrowed."""
	return {fn: fk for (sec, fn), fk in declared().items() if sec == section}


def value_field():
	"""The docfield one selection is stored in — what a reader types and options a picker from.

	A multi-value field has no column on its section's doctype, so there is no docfield there to read a
	fieldtype and a Link target off. This row IS that declaration, and it is read rather than restated."""
	return frappe.get_meta(DOCTYPE).get_field("value")


def read_all(lead):
	"""{(field_key, row_key): [value, ...]} — every multi-value selection this lead holds.

	One pass over the child table the lead already carries, so a consumer asks once and answers every
	field on every row from it. Order within an address is the stored `idx`, which is the order the rows
	were picked in."""
	out = {}
	for row in lead.get(TABLE) or []:
		out.setdefault((cstr(row.field_key), cstr(row.row_key)), []).append(row.value)
	return out


def read(lead, field_key, row_key):
	"""The selections at ONE address, or []. The single-address spelling of `read_all`."""
	return read_all(lead).get((cstr(field_key), cstr(row_key)), [])


def replace(lead, field_key, row_key, values):
	"""Stage the selections at ONE address, dropping whatever was there. The caller saves the lead.

	REPLACE, not merge: a picker hands back the whole set it is showing, so a value the reader removed
	must go. Every other address is untouched, which is what keeps two cycles of one field, and two
	fields on one cycle, from overwriting each other.

	Blanks are dropped and duplicates collapse, so a picker that sends the same value twice stores it
	once — the address plus the value is the identity, and a second row of it means nothing."""
	address = (cstr(field_key), cstr(row_key))
	kept = [r for r in (lead.get(TABLE) or [])
	        if (cstr(r.field_key), cstr(r.row_key)) != address]
	lead.set(TABLE, kept)
	for value in dict.fromkeys(v for v in (values or []) if v):
		lead.append(TABLE, {"field_key": field_key, "row_key": cstr(row_key), "value": value})
