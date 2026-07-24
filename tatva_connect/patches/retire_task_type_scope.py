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

# EVERY Link that points at CRM Task Type. delete_doc's link check refuses a referenced row, so all five are cleared or the patch aborts the migrate — CRM Visit Audit alone is written on every geofence check, so the rows exist.
_REFERRERS = (
	("CRM Task", "custom_task_type"),
	("CRM Visit Audit", "task_type"),
	("CRM Smart View", "activity_type"),
	("CRM Task Checklist Template", "task_type"),
	("CRM Program", "custom_first_activity_type"),
)


def execute():
	_drop_grainless_types()
	_drop_scope_doctype()


def _drop_grainless_types():
	"""A grainless (name-keyed) type is dormant — _grain_matches calls its all-blank grain never-raisable,
	so the row only ever advertised an activity that does not exist. A USED site can still carry records
	tagged with it: a legacy task (a pre-re-key 'Call Lead'), a Visit Audit row written by a geofence
	check, a Smart View, a checklist template, a program's first activity. Untag them all first — a blank
	Link is a valid state on every one of them, exactly what a native call-log task is
	(crm.integrations.api.add_task_to_call_log sets none) — so the meaningless tag is cleared, nothing is
	orphaned, and the type deletes. delete_doc stays force-less: a link we have NOT declared here is a
	reference nobody accounted for, and it must still fail loud rather than orphan silently."""
	for name in frappe.get_all("CRM Task Type", filters={"name": ["not like", "%::%"]}, pluck="name"):
		for doctype, fieldname in _REFERRERS:
			_untag(doctype, fieldname, name)
		frappe.delete_doc("CRM Task Type", name)  # force-less: an UNDECLARED link still fails loud


def _untag(doctype, fieldname, task_type):
	"""Clear one referrer's Link. Guarded on live reality — a doctype or column that never landed on this site is not an error, it is simply nothing to clear."""
	if not (frappe.db.exists("DocType", doctype) and frappe.db.has_column(doctype, fieldname)):
		return
	tagged = frappe.get_all(doctype, filters={fieldname: task_type}, pluck="name")
	for row in tagged:
		frappe.db.set_value(doctype, row, fieldname, None, update_modified=False)  # untag, never orphan
	if tagged:
		print(f"  retire_task_type_scope: untagged {len(tagged)} {doctype} row(s) of '{task_type}' before delete")


def _drop_scope_doctype():
	if frappe.db.exists("DocType", SCOPE_DT):
		frappe.delete_doc("DocType", SCOPE_DT, force=True)
	# delete_doc removes the DocType row, never the table — the drop is ours, through the one door.
	if frappe.db.table_exists(SCOPE_DT):
		_schema.ddl(f"DROP TABLE IF EXISTS `{SCOPE_TABLE}`", SCOPE_TABLE)
