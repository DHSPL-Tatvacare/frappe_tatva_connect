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
from frappe import _

from tatva_connect.access import visibility

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

	Every grain field gets a key, even where no visible lead carries a value for it: the frontend reads
	"the endpoint answered for this field" as "this field is scoped", so an omitted key would silently
	hand that axis back to the unscoped master picker.

	Read-only, and it grants nothing: every value returned is already on a row the caller can open. A
	System Manager is not special-cased — their `get_list` is unscoped, so they get every value present
	on any lead, which is the same rule applied to a wider set of rows rather than a second rule.
	"""
	# Only the records that CARRY a grain may be asked; anything else is a caller naming a table we never scope.
	if doctype not in visibility.PARENT_DOCTYPES:
		frappe.throw(_("Unsupported record type {0}").format(doctype))
	out = {}
	for fieldname in grain_filter_fields(doctype):
		rows = frappe.get_list(
			doctype, fields=[fieldname], distinct=True, limit_page_length=0, ignore_ifnull=True,
		)
		out[fieldname] = sorted({row.get(fieldname) for row in rows if row.get(fieldname)})
	return out
