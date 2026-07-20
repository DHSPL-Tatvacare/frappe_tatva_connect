"""Make `CRM Workflow Run.active_key` unique, and back-fill it for live runs.

The double-start guard was declared but never real: the column carried no unique constraint and no code
ever wrote it, so `UniqueValidationError` could not be raised and every matching save started another
journey on the same lead — each one raising its own tasks and sending its own messages.

Declared end state: every live (Running/Parked) run carries `workflow::subject_name`, terminal runs carry
NULL, and the column is unique. Duplicates that already exist are collapsed — the OLDEST live run per
(workflow, subject) keeps running and the rest are marked Failed, because they are duplicates that should
never have started, and leaving them live would keep them acting on the lead.

Idempotent; a no-op on a fresh site. The unique index itself is created by `bench migrate` from the
doctype JSON — this patch only makes the data satisfy it, and MUST run before that index is applied.
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
