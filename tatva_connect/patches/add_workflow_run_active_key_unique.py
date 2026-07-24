"""Make `CRM Workflow Run.active_key` unique, and back-fill it for live runs.

The double-start guard was declared but never real: the column carried no unique constraint and nothing
wrote it then, so `UniqueValidationError` could not be raised and every matching save started another
journey on the same lead — each one raising its own tasks and sending its own messages. The engine writes
it now (`workflow_engine/triggers.py`) and relies on this index as the double-start guard.

Declared end state: every live (Running/Parked) run carries `workflow::subject_name`, terminal runs carry
NULL, and the column is unique. Duplicates that already exist are collapsed — the OLDEST live run per
(workflow, subject) keeps running and the rest are marked Failed, because they are duplicates that should
never have started, and leaving them live would keep them acting on the lead.

Idempotent; a no-op on a fresh site. The unique index is created from the doctype JSON during model sync;
this patch runs after that, in [post_model_sync], and only makes the data satisfy it. That ordering is safe
because `active_key` is all-NULL when the index is built and MariaDB does not constrain NULLs — if the field
ever gains a default, or a writer that runs before the index exists, this collapse must move to
[pre_model_sync] and pre-create the index through `patches/_schema.py`.
"""
import frappe

RUN_DT = "CRM Workflow Run"
LIVE = ("Running", "Parked")


def execute():
	if not frappe.db.table_exists(RUN_DT):
		return
	_collapse_duplicates()
	_backfill_keys()
	frappe.db.set_value(RUN_DT, {"status": ["not in", LIVE]}, "active_key", None, update_modified=False)  # authz-ok: tier-c — patch, no user input


def _collapse_duplicates():
	"""Keep the oldest live run per (workflow, subject); fail the rest so they stop acting."""
	seen = set()
	for run in frappe.get_all(
		RUN_DT, filters={"status": ["in", LIVE]},
		fields=["name", "workflow", "subject_name"], order_by="creation asc",
	):
		key = (run.workflow, run.subject_name)
		if key in seen:
			frappe.db.set_value(RUN_DT, run.name, {"status": "Failed", "active_key": None}, update_modified=False)  # authz-ok: tier-c — patch, no user input
			continue
		seen.add(key)


def _backfill_keys():
	for run in frappe.get_all(
		RUN_DT, filters={"status": ["in", LIVE]}, fields=["name", "workflow", "subject_name"]
	):
		frappe.db.set_value(  # authz-ok: tier-c — patch, no user input
			RUN_DT, run.name, "active_key", f"{run.workflow}::{run.subject_name}", update_modified=False
		)
