# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The executors `bulk_actions.py` dispatches to. Each calls frappe's own, unmodified, innermost
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
and its terminal delete goes through `utils.delete_in_pace`, the one paced, per-row-commit delete every
data-sized loop uses (frappe's `delete_bulk` queues one cleanup job per row with no regard for the queue cap).
"""

import frappe
from frappe import _

from tatva_connect.utils import delete_in_pace, retry_on_deadlock


def _assign_row(doctype, name, assignees):
	"""Put `assignees` on ONE row. The body of the old `run_assign` loop, unchanged."""
	# Frappe's own line, restored where it belongs. `assign_to._add` opens with exactly this
	# (assign_to.py:72) and it is READ, deliberately: crm's row gate grants read wherever a live
	# ToDo names you, so without it naming a docname is enough to assign it to yourself and be
	# allowed to see it. Our notify-suppressing copy of `add` dropped the line; the door cannot
	# hold it, because `docnames` arrives in the request body and is tied to no list.
	from tatva_connect.lead import assignment

	frappe.get_doc(doctype, name).check_permission()
	for assignee in assignees:
		assignment.assign(doctype, name, assignee, notify=False)


def _clear_row(doctype, name):
	"""Take every assignee off ONE row. The body of the old `run_clear_assignment` loop, unchanged."""
	# `assign_to.set_status` gates identically (assign_to.py:215) — stripping assignments off
	# records you cannot see revokes other people's access.
	from tatva_connect.lead import assignment

	frappe.get_doc(doctype, name).check_permission()
	for user in assignment.current_assignees(doctype, name):
		assignment.unassign(doctype, name, user, notify=False)


def _per_row(doctype, docnames, work, what):
	"""The loop all three assignment executors share: retry a transient deadlock, commit the row, and
	let one bad row fail alone. `work(name)` is the whole of one row's mutation, so whatever it does is
	ONE transaction — which is what makes Reassign safe (see `run_reassign`)."""
	succeeded, failed = [], []
	for name in docnames:
		try:
			retry_on_deadlock(lambda name=name: work(name))
			frappe.db.commit()  # one open transaction across up to 500 rows is real lock-hold exposure
			succeeded.append(name)
		except Exception:
			frappe.db.rollback()  # discard any partial per-assignee writes before the next docname
			frappe.log_error(f"bulk {what} failed: {doctype} {name}")
			failed.append(name)
	return succeeded, failed


def _assignees(params):
	"""The picked users, as a list — the payload carries one name or many."""
	assignees = params["assign_to"]
	return [assignees] if isinstance(assignees, str) else assignees


def run_assign(doctype, docnames, params):
	assignees = _assignees(params)
	succeeded, failed = _per_row(
		doctype, docnames, lambda name: _assign_row(doctype, name, assignees), "assign"
	)
	if succeeded:
		_notify_batch_assigned(assignees, len(succeeded))
	return _summary(docnames, succeeded, failed)


def run_clear_assignment(doctype, docnames, params):
	succeeded, failed = _per_row(
		doctype, docnames, lambda name: _clear_row(doctype, name), "clear assignment"
	)
	return _summary(docnames, succeeded, failed)


def run_reassign(doctype, docnames, params):
	"""Clear, then assign — ONE row at a time, inside ONE transaction per row.

	The order matters and so does the grain. Clearing every row first and assigning afterwards would,
	on any failure in between, leave a batch of leads owned by NOBODY — strictly worse than where they
	started. Because `_per_row` commits once per row, a row that fails here rolls back to the assignee
	it already had, so a half-reassigned lead never exists. Both halves are the same functions Assign
	and Clear Assignment call, so this adds an ORDER, not a second way to assign."""
	assignees = _assignees(params)

	def _reassign(name):
		_clear_row(doctype, name)
		_assign_row(doctype, name, assignees)

	succeeded, failed = _per_row(doctype, docnames, _reassign, "reassign")
	if succeeded:
		_notify_batch_assigned(assignees, len(succeeded))
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
			# No record to derive `app` from, so it is named: the CRM bell reads the CRM's apps only.
			"app": "tatva_connect",
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
	in the same loop shape, and the terminal delete goes through `utils.delete_in_pace` — this executor simply never lets that inner
	threshold decision fire, because it owns the ONE threshold for this whole plan instead.

	ONE BAD ROW'S CASCADE NEVER STOPS THE BATCH. `delete_bulk_docs` wraps each docname's own cascade in
	a try/except and logs+continues, exactly like `run_assign`/`run_clear_assignment` above do for their
	own per-row work — this executor matches that: a docname whose cascade raises is logged and simply
	never added to `ready`, so it's never deleted and naturally falls out as `failed` via
	the existing read-back below, with no separate tracking structure needed.

	THE RECORD'S OWN DELETE RIGHT DECIDES FOR WHAT HANGS OFF IT, as it does in stock crm. Judging each linked
	row by its own permission broke the moment rows were scoped through their parent: crm unlinks a row before
	deleting it, and an unlinked row no longer hangs off anything the caller can see. So the caller's delete on
	the record is asked once, and every linked row is then deleted, or unlinked, while it still hangs off it.
	"""
	from crm.api.doc import get_linked_docs_of_document, remove_contact_link, remove_doc_link

	delete_linked = bool(params.get("delete_linked"))
	ready = []
	for name in docnames:
		if not frappe.db.exists(doctype, name):
			continue
		if not frappe.has_permission(doctype, "delete", name):
			continue  # never reaches `ready`, so the read-back reports it failed

		def _cascade(name=name):
			linked = [
				(d["reference_doctype"], d["reference_docname"])
				for d in get_linked_docs_of_document(doctype, name)
				if d.get("reference_doctype") and d.get("reference_docname")
			]
			for ldt, ldn in [pair for pair in linked if not frappe.db.exists(*pair)]:
				remove_doc_link(ldt, ldn)  # an orphan has no row to judge; crm's own clear removes the dangling child row
			if delete_linked:
				_delete_linked(linked)
			elif doctype == "Contact":
				for ldt, ldn in linked:
					remove_contact_link(ldt, ldn)
			else:
				for ldt, ldn in linked:
					_unlink(ldt, ldn, doctype, name)
		try:
			retry_on_deadlock(_cascade)
			frappe.db.commit()  # one open transaction across up to 500 rows is real lock-hold exposure
		except Exception as e:
			frappe.db.rollback()  # discard any partial per-linked-doc writes before the next docname
			frappe.log_error(f"Error processing linked docs for {doctype} {name}: {e!s}", "Bulk Delete Error")
			continue
		ready.append(name)

	delete_in_pace(doctype, ready, lambda name: frappe.delete_doc(doctype, name))  # the read-back below reports what is left
	remaining = set(frappe.get_all(doctype, filters={"name": ["in", docnames]}, pluck="name"))
	failed = [d for d in docnames if d in remaining]
	succeeded = [d for d in docnames if d not in remaining]
	return _summary(docnames, succeeded, failed)


def _delete_linked(linked):
	"""Delete every linked row, retrying the ones another linked row still points at until no pass makes progress."""
	pending = [(ldt, ldn) for ldt, ldn in linked if frappe.db.exists(ldt, ldn)]
	while pending:
		blocked = []
		for ldt, ldn in pending:
			try:
				frappe.delete_doc(ldt, ldn, ignore_permissions=True)  # authz-ok: tier-b — the caller's delete on the record this row hangs off is asked in run_bulk_delete
			except frappe.LinkExistsError:
				blocked.append((ldt, ldn))
		if len(blocked) == len(pending):
			frappe.throw(_("{0} {1} is still linked to another record.").format(_(blocked[0][0]), blocked[0][1]), frappe.LinkExistsError)
		pending = blocked


def _unlink(ldt, ldn, doctype, name):
	"""Clear every Link / Dynamic Link on the row that points at doctype/name — read off its meta, so `reference_name` and a plain Link clear too.

	Written, not saved: a save re-runs validate, and crm's WhatsApp validate re-derives the lead from the phone number, putting the link straight back."""
	row = frappe.db.get_value(ldt, ldn, "*", as_dict=True)
	if not row:
		return
	cleared = {}
	for df in frappe.get_meta(ldt).get("fields", {"fieldtype": ("in", ("Link", "Dynamic Link"))}):
		target = row.get(df.options) if df.fieldtype == "Dynamic Link" else df.options
		if target == doctype and row.get(df.fieldname) == name:
			cleared[df.fieldname] = None
			if df.fieldtype == "Dynamic Link":
				cleared[df.options] = None
	if cleared:
		frappe.db.set_value(ldt, ldn, cleared)


def _summary(docnames, succeeded, failed):
	return {"total": len(docnames), "succeeded": len(succeeded), "failed": len(failed), "failed_names": failed}
