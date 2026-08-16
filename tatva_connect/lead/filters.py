"""Grain filter options — the values a user may FILTER the lead list by.

The lead LIST is already scoped correctly: native User Permission narrows `get_list`, so an Anaya rep
reads 1952 of 2424 leads. The value DROPDOWNS beside it were not: they are fed by frappe's Link search,
which is called with the target doctype only — no `reference_doctype` — so our narrow, CRM-Lead-scoped
User Permission never fires and the picker offers the whole master (5 verticals, 6 groups, 10 programmes).
The rep could not open those leads, but they could read the names of every other business line.

The fix needs no new permission and no `reference_doctype` gymnastics: ask the LEAD TABLE, through the
same `get_list` that already scopes the list. What comes back is, by construction, exactly the grain values
present on rows the caller can see — self-scoping, and correct for a wildcard entitlement (which holds no
programme permission at all, so a permission-based filter would have offered them nothing).

WHICH fields this covers is derived from the DATA MODEL, never from a list of names: a field is a grain
axis iff it is a Link whose TARGET is one of the three grain masters. An earlier build named three
fieldnames explicitly, and the two history fields (`custom_previous_program`, `custom_origin_vertical`)
leaked the whole programme master for exactly that reason — they were Links to a grain master that the
list happened not to mention. A rule keyed on the target cannot have that gap, and a grain Link field
added tomorrow is scoped with no code change here.

This is the FILTER side and it reads WHAT EXISTS. The create picker is the WRITE side and reads WHAT IS
ALLOWED — the CRM Grain registry, including programmes with no leads yet, so the first lead in a new
programme can be created. The two sources are deliberately never merged: pointing a filter at the registry
would offer values that match nothing, and pointing a picker at the lead table would make a new programme
uncreatable. (Plan decision 7.)
"""
import frappe

# The three masters that MAKE a field a grain axis. This is the one place the grain doctypes are named;
# WHICH fields point at them is read off the meta below, so the two can never drift apart.
GRAIN_MASTERS = ("CRM Vertical", "CRM Group", "CRM Program")

_DOCTYPE = "CRM Lead"


def grain_filter_fields(doctype: str = _DOCTYPE):
	"""Every field on `doctype` that is a Link to a grain master, off the live meta.

	The rule, expressed once: the TARGET decides. Nothing here enumerates fieldnames, so
	`custom_vertical`, `custom_group`, `custom_current_program` and the two history Links are covered by
	the same sentence — and so is any grain Link added later, on either record that carries a grain.
	"""
	return tuple(
		field.fieldname
		for field in frappe.get_meta(doctype).fields
		if field.fieldtype == "Link" and field.options in GRAIN_MASTERS
	)


@frappe.whitelist()
def grain_filter_options(doctype: str = _DOCTYPE):
	"""`{fieldname: [values]}` — the distinct values of every grain axis on the rows the CALLER can see.

	Every grain field gets a key, even where no visible lead carries a value for it, so `stamp_grain_options`
	can tell "this axis has nothing to offer" from "this field is not an axis" — an omitted key would hand
	that axis back to the unscoped master picker.

	Total: a record type with no grain Link answers `{}`. The derivation IS the allowlist, so no caller can
	name a table this refuses — a gate here refused `CRM Task` on every list page and activity tab that
	mounts the shared filter, which is a question asked in the wrong room and not a caller to correct.

	Read-only, and it grants nothing: every value returned is already on a row the caller can open. A
	System Manager is not special-cased — their `get_list` is unscoped, so they get every value present
	on any lead, which is the same rule applied to a wider set of rows rather than a second rule.
	"""
	out = {}
	for fieldname in grain_filter_fields(doctype):
		rows = frappe.get_list(
			doctype, fields=[fieldname], distinct=True, limit_page_length=0, ignore_ifnull=True,
		)
		out[fieldname] = sorted({row.get(fieldname) for row in rows if row.get(fieldname)})
	return out


def stamp_grain_options(fields, doctype: str = _DOCTYPE):
	"""Every grain axis in a FIELD CATALOG, carrying the values its filter control may offer.

	The catalog is where a field already says what control it needs — `link_query` is this same sentence for
	a composite master — so the values ride ON the field. A consumer therefore asks the field, never a second
	endpoint keyed on a fieldname it hopes matches: a Smart View names this same column `lead:program`, and a
	fieldname match silently missed it and handed a rep the whole programme master.

	Stamped by `fieldname`, which every catalog carries whatever it keys its rows by. A field this doctype
	has no answer for is returned untouched, so a catalog of some other record type is unchanged.

	A new dict per field rather than a stamp: native caches its catalog answer, and mutating those rows would
	write one caller's visible values into a cache every caller reads."""
	values = grain_filter_options(doctype)
	return [
		{**f, "grain_options": values[f.get("fieldname")]} if f.get("fieldname") in values else f
		for f in fields
	]
