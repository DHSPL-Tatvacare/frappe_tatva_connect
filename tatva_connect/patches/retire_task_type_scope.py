"""Retire the `CRM Task Type Scope` rival grain source, and the grainless types it propped up.

The grain lives in the parent composite key (`vertical::group::program::type_name`) and has since the
re-key. `Scope` stayed behind as a deprecated child table — and did real damage: the automation lane
gated its grain check on a Scope row EXISTING, so a type carrying only the parent grain (every real
type) had `scoped` falsy and the check never ran. A rival grain source whose only live effect was to
switch enforcement off.

Two end states declared here: the doctype and its table are gone, and no CRM Task Type is name-keyed.
A name-keyed type carries no grain, `_grain_matches` calls an all-blank grain dormant, and it can
therefore never be raised on any lead — the row only ever advertised an activity that does not exist.
Deleted through native `delete_doc`, so frappe's own link check refuses any that is genuinely
referenced rather than orphaning it. Idempotent; assumes nothing about what ran before.
"""
import frappe

from tatva_connect.patches import _schema

SCOPE_DT = "CRM Task Type Scope"
SCOPE_TABLE = "tabCRM Task Type Scope"


def execute():
	_drop_grainless_types()
	_drop_scope_doctype()


def _drop_grainless_types():
	"""A grainless (name-keyed) type is dormant — _grain_matches calls its all-blank grain never-raisable,
	so the row only ever advertised an activity that does not exist. A USED site can still carry a legacy
	task tagged with it (a pre-re-key 'Call Lead' from before the assignment resolver guard). Untype those
	tasks first — a CRM Task with no custom_task_type is a valid state, exactly what a native call-log task
	is (crm.integrations.api.add_task_to_call_log sets none) — so the meaningless tag is cleared, nothing is
	orphaned, and the type deletes. delete_doc stays force-less: a NON-task link (a rule/view) is a real
	reference the operator must resolve, and it still fails loud."""
	for name in frappe.get_all("CRM Task Type", filters={"name": ["not like", "%::%"]}, pluck="name"):
		tagged = frappe.get_all("CRM Task", filters={"custom_task_type": name}, pluck="name")
		for task in tagged:
			frappe.db.set_value("CRM Task", task, "custom_task_type", None, update_modified=False)  # untype, never orphan
		if tagged:
			print(f"  retire_task_type_scope: untyped {len(tagged)} legacy task(s) of '{name}' before delete")
		frappe.delete_doc("CRM Task Type", name)  # force-less: a non-task link still fails loud


def _drop_scope_doctype():
	if frappe.db.exists("DocType", SCOPE_DT):
		frappe.delete_doc("DocType", SCOPE_DT, force=True)
	# delete_doc removes the DocType row, never the table — the drop is ours, through the one door.
	if frappe.db.table_exists(SCOPE_DT):
		_schema.ddl(f"DROP TABLE IF EXISTS `{SCOPE_TABLE}`", SCOPE_TABLE)
