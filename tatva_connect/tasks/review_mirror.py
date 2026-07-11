"""Mirror a Document Review task's verdict back onto its File — the ONE back-reference writer.

A Document Review task carries the rep's Approve/Reject in `custom_outcome` (the promoted column
`compute_activity` writes when the rep logs the activity). When that verdict lands, copy it onto the
File the review was raised on (found by the File's `custom_review_task` back-reference), so the
Attachments-tab badge reflects the decision without any fan-out.

One writer, unified get_doc/save path (never db.set_value — that skips validate/mirroring). Gated by
its own kill switch (ships dormant) and idempotent: it fires only when the outcome actually CHANGED
to a terminal verdict on this save, and no-ops when the File already shows it.
"""
import frappe

from tatva_connect import automation

_VERDICTS = ("Approved", "Rejected")


def mirror_review_outcome(doc, method=None):
	"""CRM Task.on_update — copy a Document Review task's Approved/Rejected verdict onto its File."""
	if not automation.is_enabled("Task::Review::mirror"):
		return
	outcome = doc.custom_outcome
	if outcome not in _VERDICTS:
		return
	before = doc.get_doc_before_save()
	if before and before.custom_outcome == outcome:
		return  # outcome unchanged on this save — nothing to mirror
	if frappe.db.get_value("CRM Task Type", doc.custom_task_type, "type_name") != "Document Review":
		return
	# Fan out to EVERY File that links back to this task (get_all, never get_value — the back-reference
	# is one-to-many-capable, so a single-row read could badge one file and orphan the rest). With the
	# per-document review tasks this is normally one File; the total lookup guarantees no linked file is
	# ever left Pending after its verdict lands.
	for file_name in frappe.get_all("File", filters={"custom_review_task": doc.name}, pluck="name"):
		file_doc = frappe.get_doc("File", file_name)
		if file_doc.custom_review_status == outcome:
			continue  # already mirrored — no-op
		file_doc.custom_review_status = outcome
		file_doc.save(ignore_permissions=True)
