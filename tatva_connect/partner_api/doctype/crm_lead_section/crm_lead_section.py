# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""One row per lead section — the ONE home of its table, its target, its row key and its title.

`sql_source` is DERIVED here and stored nowhere — it was a column on all 607 catalog rows and 29 of them
were born blank, because a value copied onto every row is a value that has to be remembered on every row.
Which row a multi-row child shows is read STRUCTURALLY off the section's `row_key_field`, not encoded into
a pick string, so there is one contract and nothing left to rot.
"""
import frappe
import frappe.model
from frappe.model.document import Document
from frappe.utils import now_datetime, today

LEAD_DOCTYPE = "CRM Lead"
# Where a section's columns live: on the lead, on its child table, or as one row per question.
PARENT, CHILD, ANSWER = "parent", "child", "answer"

# Every field on this doctype that NAMES a column of the target. One list, so a new one is validated,
# and described, by having been added here rather than by anyone remembering to write a check for it.
COLUMN_FIELDS = ("row_key_field", "value_field", "label_field", "question_field")

# What a key-value section must name before it can hold anything: where a row's identity, its answer,
# its human label and the raw key that identity was derived from each live.
_KEY_VALUE_REQUIRED = ("row_key_field", "value_field", "label_field", "question_field")


def docfield(doctype, fieldname):
	"""The DocField for `fieldname` on `doctype`, STANDARD FIELDS INCLUDED, or None where it is no column.

	`Meta.get_field` answers only for fields the DocType declares, so it returns None for `name`, `owner`,
	`creation` and `modified` — frappe keeps those in `frappe.model.std_fields`, which carries their real
	fieldtype AND label. Two places asked meta alone and both went wrong the same way: a catalog row on a
	standard field could not be SAVED (its own validate called it "not a field"), and Smart Views typed it
	`Data`, so a Datetime rendered as a raw `2026-09-09 00:25:09.490786`. One answer, asked once."""
	if not (doctype and fieldname):
		return None
	df = frappe.get_meta(doctype).get_field(fieldname)
	if df:
		return df
	for std in frappe.model.std_fields:
		if std["fieldname"] == fieldname:
			# frappe declares `name` a Link with no target: the doctype it names is its target.
			return frappe._dict(std, options=std.get("options") or (doctype if std["fieldtype"] == "Link" else None))
	return None


def sql_source(section):
	"""Where this section's columns physically live. Derived — a stored copy is a second brain."""
	if section.get("is_key_value"):
		return ANSWER
	return CHILD if section.get("child_table_field") else PARENT


def stamp_row_key(section, values):
	"""A NEW multi-row row whose writer named no key gets the moment it arrived, typed to the column.

	A surface that KNOWS when the row happened sends it (Facebook sends Meta's `created_time`, a partner
	names its own, an automation Field Map may set it). One that does not — an intake form, a rep, an
	automation node — gets now. The alternative was a blank key, and a blank key on a multi-row section is
	no address at all: `_multi_row_needs_a_row_key` says so, the partner API could never target such a row,
	and every later write appended beside it (or merged into it) for ever.

	It lives here because the section already declares the row key AND the doctype it is a column of, so it
	needs neither the parent doc nor the child-table fieldname, and every appending lane — the partner API,
	both automation child-row verbs — asks the ONE question instead of restating it. Values that already
	name a key come back untouched: this gives a row an address, it never re-dates one."""
	key_field = section.get("row_key_field")
	if not section.get("is_multi_row") or not key_field or values.get(key_field) not in (None, ""):
		return values
	df = docfield(section.get("target_doctype"), key_field)
	return {**values, key_field: today() if (df and df.fieldtype == "Date") else now_datetime()}


_CHILD_SECTIONS_CACHE = "tatva_connect:child_sections"


def child_sections():
	"""Every section that lives in a child table, as section docs.

	A caller wanting the ONE row a section resolves to passes each of these to
	`lead.multirow.row_for_section` — the same rule the Data tab and a Smart View read through. Listed
	here beside `sql_source` so nobody re-filters `CRM Lead Section` by hand.

	Request-cached, and not as a nicety: `section_for_child` walks this list and both the publish gate and the
	authoring answer ask it ONCE PER NODE, so a 25-node graph paid 25 identical `get_all`s for a set that
	cannot change inside a request. The docs were already `get_cached_doc`; only the listing query was not.
	"""
	from tatva_connect.access import request_cache

	def build():
		return [
			frappe.get_cached_doc("CRM Lead Section", r.name)
			for r in frappe.get_all("CRM Lead Section", filters={"child_table_field": ["!=", ""]}, fields=["name"])
		]

	return request_cache(_CHILD_SECTIONS_CACHE, "all", build)


def section_for_child(value):
	"""The child section a value names — by section name, by child-table fieldname, or by child doctype.

	A child-row workflow node stores the SECTION (a Link, so it rides the app's own link picker and
	`api.list_link_titles`), the publish gate asks about the record its fields live on, and the runtime
	needs the table fieldname to reach the rows. One resolver answers all three, and accepting the
	fieldname is also why a config authored before the node was retyped still resolves."""
	for section in child_sections():
		if value and value in (section.name, section.child_table_field, section.target_doctype):
			return section
	return None


class CRMLeadSection(Document):
	def validate(self):
		self._multi_row_needs_a_row_key()
		self._key_value_needs_an_address_and_a_value()
		self._key_value_is_not_multi_row()
		if not self.target_doctype:
			return  # reqd catches it, and every check below reads the target's meta
		self._every_named_column_is_real()
		self._child_table_reaches_the_target()
		self._a_section_with_no_child_table_is_the_lead()

	def _key_value_needs_an_address_and_a_value(self):
		missing = [f for f in _KEY_VALUE_REQUIRED if not self.get(f)]
		if self.is_key_value and missing:
			frappe.throw(
				frappe._("A key-value section must name every column it uses; missing: {0}. Its rows ARE its fields, so nothing can read one until it knows where the identity, the answer, the label and the raw key each live.").format(", ".join(missing)),
				title=frappe._("Key-value section is incomplete"),
			)

	def _key_value_is_not_multi_row(self):
		if self.is_key_value and self.is_multi_row:
			frappe.throw(
				frappe._("A section is keyed by a field or dated by a row key, never both: a key-value section already holds exactly one row per field."),
				title=frappe._("Key Value and Multi Row are exclusive"),
			)

	def _every_named_column_is_real(self):
		"""Each COLUMN_FIELDS entry names a column of the target, or it names nothing at all.

		One check for all of them: a section that points at a column which does not exist is a section
		every consumer reads a None out of, and there is no reason for that to be caught for the row key
		and missed for the label."""
		meta = frappe.get_meta(self.target_doctype)
		for field in COLUMN_FIELDS:
			named = self.get(field)
			if named and not meta.get_field(named):
				frappe.throw(
					frappe._("{0} names {1}, which is not a field of {2}.").format(
						self.meta.get_label(field), named, self.target_doctype
					),
					title=frappe._("Unknown column"),
				)

	def _multi_row_needs_a_row_key(self):
		if self.is_multi_row and not self.row_key_field:
			frappe.throw(
				frappe._("A multi-row section needs a Row Key Field: without one no row has an address, so every write lands on the same row."),
				title=frappe._("Row Key Field required"),
			)

	def _child_table_reaches_the_target(self):
		if not self.child_table_field:
			return
		field = frappe.get_meta(LEAD_DOCTYPE).get_field(self.child_table_field)
		if not field or field.fieldtype != "Table" or field.options != self.target_doctype:
			frappe.throw(
				frappe._("{0} is not a Table field on {1} holding {2} rows.").format(self.child_table_field, LEAD_DOCTYPE, self.target_doctype),
				title=frappe._("Child Table Field does not reach the target"),
			)

	def _a_section_with_no_child_table_is_the_lead(self):
		if not self.child_table_field and self.target_doctype != LEAD_DOCTYPE:
			frappe.throw(
				frappe._("A section with no Child Table Field is the lead row itself, so its target must be {0}.").format(LEAD_DOCTYPE),
				title=frappe._("Child Table Field required"),
			)
