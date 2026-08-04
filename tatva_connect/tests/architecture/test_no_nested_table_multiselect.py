# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A `Table MultiSelect` on a child doctype is a second level of child rows, and Frappe persists one.

`frappe/model/base_document.py:62` sets `TABLE_DOCTYPES_FOR_CHILD_TABLES = MappingProxyType({})` and
`_init_child` assigns it to every child row, so a child row is told it has no table fields of its own.
The rows neither load nor save: a parent save writes nothing, a read answers nothing, and every reader
downstream — the Data tab, the partner projection, Smart Views — shows an empty cell over stored data.
It fails in SILENCE, which is why it stood for months on a field 96 of 250 migrated leads had answered.

A field that takes more than one value hangs its selections off the LEAD instead, one level down, as
`CRM Lead Multi Value` rows declared by `is_multi_value` on the catalog — `tatva_connect.lead.multi_value`.

Estate-wide: the other Table MultiSelects (frappe, LMS, Helpdesk, Insights, and our own on CRM Lead) all
sit on real doctypes and work. This gate is about the HOST being a child table, nothing else.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \
        --module tatva_connect.tests.architecture.test_no_nested_table_multiselect
"""
import frappe
from frappe.tests.utils import FrappeTestCase

_FIELDTYPE = "Table MultiSelect"


def _declared_fields():
	"""Every Table MultiSelect on the site, standard and custom alike, as (host doctype, fieldname).

	Both tables are read because a fieldtype can arrive either way and the hazard is identical — the one
	that shipped this defect was a DocField on an app-owned child doctype, and the same mistake made
	through a Custom Field would be invisible to a check that only read one of them."""
	standard = frappe.get_all(
		"DocField", filters={"fieldtype": _FIELDTYPE}, fields=["parent", "fieldname"],
		parent_doctype="DocType",
	)
	custom = frappe.get_all(
		"Custom Field", filters={"fieldtype": _FIELDTYPE}, fields=["dt as parent", "fieldname"],
	)
	return [(r["parent"], r["fieldname"]) for r in [*standard, *custom]]


class TestNoNestedTableMultiSelect(FrappeTestCase):
	def test_no_child_doctype_declares_a_table_multiselect(self):
		declared = _declared_fields()
		self.assertTrue(declared, "no Table MultiSelect exists at all — this gate is asserting nothing")
		hosts = {host for host, _fn in declared}
		child_hosts = set(frappe.get_all(
			"DocType", filters={"name": ["in", sorted(hosts)], "istable": 1}, pluck="name",
		))
		nested = sorted(f"{host}.{fieldname}" for host, fieldname in declared if host in child_hosts)
		self.assertFalse(
			nested,
			"a Table MultiSelect on a child doctype is a second level of child rows, which Frappe never "
			"loads and never saves — the value is stored by nobody and read by nobody, in silence. "
			"Declare the field multi-value on its CRM Lead API Field row and let "
			f"tatva_connect.lead.multi_value hold the selections instead: {nested}",
		)
