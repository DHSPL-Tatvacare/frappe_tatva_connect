# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The four executors `bulk_actions.py` dispatches to. Each calls frappe's own, unmodified, innermost
mutation function per row — never frappe's outer dispatcher (`add_multiple`, `submit_cancel_or_update_docs`,
`delete_items`), which each carry their own ad-hoc threshold this seam intentionally never reaches.
`run_bulk_delete` also deliberately avoids a fourth, CRM-specific dispatcher, `crm.api.doc.delete_bulk_docs`
(see its own docstring below for why).

ASSIGN AND CLEAR ASSIGNMENT LOOP HERE, ON PURPOSE. `assign_to.add_multiple`/`remove_multiple` are bare
loops around `add()`/`remove()` with no try/except at all — one failing row aborts every row after it.
The loop below is the ONLY new logic in this file; the actual assignment logic inside `add()`/`remove()`
is called exactly as frappe wrote it.

BULK EDIT DOES NOT LOOP HERE. `bulk_update._bulk_action` already has correct per-row try/except and
per-row commit — re-implementing that loop here would be a second, divergent copy of logic frappe
already got right. BULK DELETE now has its OWN per-row cascade loop (see `run_bulk_delete`'s docstring),
but the terminal delete itself is still one un-looped call to `reportview.delete_bulk`, which has its
own per-row try/except, per-row commit, and self-healing retry pass.
"""
import time

import frappe
from frappe import _

_DEADLOCK_RETRIES = 3


def _with_deadlock_retry(fn):
	"""Retry fn() on a transient deadlock/lock-wait (same classification as workflow_engine.interpreter and api.partner_bulk_worker); any other exception propagates immediately, unretried."""
	for attempt in range(_DEADLOCK_RETRIES):
		try:
			return fn()
		except (frappe.QueryDeadlockError, frappe.QueryTimeoutError):
			frappe.db.rollback()
			if attempt == _DEADLOCK_RETRIES - 1:
				raise
			time.sleep(0.15 * (attempt + 1))


def run_assign(doctype, docnames, params):
	from tatva_connect.lead.assignment import silent_add_assignee

	assignees = params["assign_to"]
	if isinstance(assignees, str):
		assignees = [assignees]
	succeeded, failed = [], []
	for name in docnames:
		def _assign(name=name):
			for assignee in assignees:
				silent_add_assignee(doctype, name, assignee)
		try:
			_with_deadlock_retry(_assign)
			frappe.db.commit()  # one open transaction across up to 500 rows is real lock-hold exposure
			succeeded.append(name)
		except Exception:
			frappe.db.rollback()  # discard any partial per-assignee writes before the next docname
			frappe.log_error(f"bulk assign failed: {doctype} {name}")
			failed.append(name)
	if succeeded:
		_notify_batch_assigned(assignees, len(succeeded))
	return _summary(docnames, succeeded, failed)


def run_clear_assignment(doctype, docnames, params):
	from frappe.desk.form import assign_to

	from tatva_connect.lead.assignment import silent_unassign

	succeeded, failed = [], []
	for name in docnames:
		def _clear(name=name):
			for assignment in assign_to.get({"doctype": doctype, "name": name}):
				silent_unassign(doctype, name, assignment.get("owner"))
		try:
			_with_deadlock_retry(_clear)
			frappe.db.commit()  # one open transaction across up to 500 rows is real lock-hold exposure
			succeeded.append(name)
		except Exception:
			frappe.db.rollback()  # discard any partial per-assignee writes before the next docname
			frappe.log_error(f"bulk clear assignment failed: {doctype} {name}")
			failed.append(name)
	return _summary(docnames, succeeded, failed)


def _notify_batch_assigned(assignees, count):
	"""One notification per recipient for the whole batch — frappe's own notify_assignment fires per ToDo with no off-switch, so this reuses its underlying delivery primitive directly instead, once."""
	from frappe.desk.doctype.notification_log.notification_log import enqueue_create_notification

	for assignee in assignees:
		if assignee == frappe.session.user:  # matches frappe's own notify_assignment: never tell yourself
			continue
		enqueue_create_notification(assignee, {
			"type": "Alert", "subject": _("{0} leads assigned to you").format(count),
			"from_user": frappe.session.user,
		})


def edit_values(params):
	# Bulk Edit's params as the {fieldname: value} a document update takes. Read at the door for the `disable_bulk_complete` refusal and here for the write, so the two can never judge different values.
	return {params["field"]: params["value"]}


def run_bulk_edit(doctype, docnames, params):
	from frappe.desk.doctype.bulk_update.bulk_update import _bulk_action

	failed = _bulk_action(doctype, docnames, "update", edit_values(params)) or []
	succeeded = [d for d in docnames if d not in failed]
	return _summary(docnames, succeeded, failed)


def run_bulk_delete(doctype, docnames, params):
	"""THE CASCADE IS NOT OPTIONAL. The CRM app's own list-view Delete button never calls frappe's bare
	`delete_bulk` directly — it goes through `crm.api.doc.delete_bulk_docs`, which first walks every
	selected row's linked documents and unlinks (or, if the operator chose it, deletes) each one via
	`remove_linked_doc_reference`. Skipping that cascade here would silently drop a real feature the
	moment a selection crosses this seam's threshold.

	NOT CALLED WHOLESALE, ON PURPOSE. `delete_bulk_docs` carries its OWN >10-row threshold and its own
	bare `frappe.enqueue("frappe.desk.reportview.delete_bulk", ...)` — untracked, no job row, no
	completion signal. Any selection that reaches THIS executor is already ≥20 (this seam's own
	threshold), which is always >10, so calling `delete_bulk_docs` as one unit would make it take its
	OWN enqueue branch every time: it would return `{"queued": items, "deleted": [], "failed": []}`
	immediately, before anything is actually deleted, and the real delete would run later on a SEPARATE,
	untracked job this seam knows nothing about — reporting "Completed" while nothing had happened yet.

	So the two REAL, ALREADY-WHITELISTED functions `delete_bulk_docs` itself calls are reused directly,
	in the same loop shape, and frappe's own `delete_bulk` is called as the terminal step exactly as
	`delete_bulk_docs`'s inline (≤10) branch already does — this executor simply never lets that inner
	threshold decision fire, because it owns the ONE threshold for this whole plan instead.

	ONE BAD ROW'S CASCADE NEVER STOPS THE BATCH. `delete_bulk_docs` wraps each docname's own cascade in
	a try/except and logs+continues, exactly like `run_assign`/`run_clear_assignment` above do for their
	own per-row work — this executor matches that: a docname whose cascade raises is logged and simply
	never added to `ready`, so it's never passed to `delete_bulk` and naturally falls out as `failed` via
	the existing read-back below, with no separate tracking structure needed.
	"""
	from crm.api.doc import get_linked_docs_of_document, remove_linked_doc_reference
	from frappe.desk.reportview import delete_bulk

	delete_linked = bool(params.get("delete_linked"))
	ready = []
	for name in docnames:
		if not frappe.db.exists(doctype, name):
			continue

		def _cascade(name=name):
			for linked in get_linked_docs_of_document(doctype, name):
				if not linked.get("reference_doctype") or not linked.get("reference_docname"):
					continue
				remove_linked_doc_reference(
					[{"doctype": linked["reference_doctype"], "docname": linked["reference_docname"]}],
					remove_contact=doctype == "Contact",
					delete=delete_linked,
				)
		try:
			_with_deadlock_retry(_cascade)
			frappe.db.commit()  # one open transaction across up to 500 rows is real lock-hold exposure
		except Exception as e:
			frappe.db.rollback()  # discard any partial per-linked-doc writes before the next docname
			frappe.log_error(f"Error processing linked docs for {doctype} {name}: {e!s}", "Bulk Delete Error")
			continue
		ready.append(name)

	delete_bulk(doctype, ready)
	remaining = set(frappe.get_all(doctype, filters={"name": ["in", docnames]}, pluck="name"))
	failed = [d for d in docnames if d in remaining]
	succeeded = [d for d in docnames if d not in remaining]
	return _summary(docnames, succeeded, failed)


def _summary(docnames, succeeded, failed):
	return {"total": len(docnames), "succeeded": len(succeeded), "failed": len(failed), "failed_names": failed}
