"""Retire the CRM Task checklist: three doctypes, their tables, and the child field on CRM Task.

IT COULD NEVER BE SATISFIED. `seed_checklist` wrote rows at `done = 0` and `enforce_checklist` refused
Done while a required one stayed unticked — and nothing in the product ever set `done` to 1. There is no
checklist in the CRM app's frontend and no writer anywhere but the seeder, so a single template row would
have made every task of that type permanently unclosable. It stayed harmless only because the switch that
gated both was off and no template was ever authored: 0 templates, 0 template items, 0 checklist rows.

`Task::CRM Task::guards` retires in the same change for its own reason, recorded in the registry: the
task TYPE declares what a completion demands and `activity.api.compute_activity` already enforces it.

Three end states declared here: no `CRM Task` carries a checklist field, the three doctypes and their
tables are gone, and the Field Operations sidebar no longer offers Checklist Templates. The doctype
folders are archived rather than deleted (.archive/archive/doctype/), the shape `retire_task_type_scope`
set. Deleted through native `delete_doc`, so a link we have NOT accounted for still fails loud.
Idempotent; assumes nothing about what ran before.
"""

import frappe

from tatva_connect.patches import _desk, _schema

FIELD = "custom_checklist"

# Child before parent: a template item is a row OF a template, and the item doctype is what the field on CRM Task points at.
_DOCTYPES = ("CRM Task Checklist Item", "CRM Task Checklist Template Item", "CRM Task Checklist Template")


def execute():
	_drop_task_field()
	for doctype in _DOCTYPES:
		_drop_doctype(doctype)
	# The sidebar loses a Link, and a bumped `modified` alone ships nothing on a desk that was ever opened.
	_desk.reimport("workspace_sidebar", "field_operations.json")


def _drop_task_field():
	"""The Custom Field row AND the column. Dropping it from fixtures/custom_field.json only stops it
	shipping — frappe never deletes a Custom Field a site already holds, and never drops the column when
	a field leaves a doctype, so both survive as a live-looking Table field pointing at a doctype that is
	gone. A Table field owns no column of its own, so there is nothing to drop beyond the row itself."""
	name = frappe.db.get_value("Custom Field", {"dt": "CRM Task", "fieldname": FIELD})
	if name:
		frappe.delete_doc("Custom Field", name, ignore_permissions=True)  # authz-ok: tier-a — patch, runs at migrate


def _drop_doctype(doctype):
	if frappe.db.exists("DocType", doctype):
		frappe.delete_doc("DocType", doctype, force=True)  # authz-ok: tier-a — patch, runs at migrate
	# delete_doc removes the DocType row, never the table — the drop is ours, through the one door.
	if frappe.db.table_exists(doctype):
		_schema.ddl(f"DROP TABLE IF EXISTS `tab{doctype}`", f"tab{doctype}")
