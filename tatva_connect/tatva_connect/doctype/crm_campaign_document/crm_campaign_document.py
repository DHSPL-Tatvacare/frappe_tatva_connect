# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The owner a generated marketing document hangs off, so publishing one cannot publish a patient's files.

WHY THIS ROW EXISTS. Privacy has ONE checkpoint — `storage.file_events.may_be_public()` — and it
classifies a file by the record that OWNS it: the `(attached_to_doctype, attached_to_name)` pair must
both be real, the `Storage::File::privacy` switch must be on, and the OWNING DOCTYPE must appear in the
operator's allowlist (`CRM Azure Storage Settings -> Public Attachment Doctypes`). `FileOverride` derives
`is_private` from that answer before core writes a byte and again on every save, so nothing else decides
it. That allowlist is per-DOCTYPE, never per-file — which is exactly what makes this row necessary.

Filing the PDF against `CRM Lead` would mean putting `CRM Lead` on the allowlist, and one entry would then
publish EVERY lead attachment, a patient's clinical documents included. A generated marketing document is a
different kind of thing, so it gets a different owner and only THAT owner is listed. `may_be_public` is not
touched, no kwarg is added and no per-file tick is introduced — the mechanism already says all of this, and
a second decider is precisely what the file rules forbid.

Listing the doctype is a DEPLOY STEP, not a migration, the same shape the existing allowlist entries have.
Until an operator sets it these documents are private and no external fetcher can read them, which is the
correct dormant state.
"""
import frappe
from frappe.model.document import Document

DT = "CRM Campaign Document"


class CRMCampaignDocument(Document):
	pass


def drop_for_lead(doc, method=None):
	"""`CRM Lead.on_trash` — a lead's campaign documents die with the lead, and their bytes with them.

	Deleted one row at a time through `frappe.delete_doc`, never `frappe.db.delete`, because here the delete
	IS the reclaim: only the document path reaches `file_manager.remove_all` -> `File.on_trash` -> the blob
	drop, which is M1 — a blob's life is exactly its row's life. A bulk row delete would leave a PUBLIC PDF
	about a deleted patient sitting in the container for ever, and nothing would ever name it again.

	Placed on `on_trash` and not on a reaper because frappe runs `on_trash` BEFORE its own link check, so the
	rows are gone by the time `check_if_doc_is_linked` reads the `lead` Link — the cascade is what lets the
	lead delete succeed at all, rather than something that tidies up after it.

	Deliberately NOT `@fail_safe`. A campaign document holds a real artifact, so a failure here must take the
	lead delete down with it rather than half-delete a person; refusing the delete is the framework's own
	answer to a dependant it cannot remove, and this is that answer.
	"""
	for name in frappe.get_all(DT, filters={"lead": doc.name}, pluck="name"):
		frappe.delete_doc(DT, name, ignore_permissions=True)  # authz-ok: tier-b — the gate is frappe's own delete permission on the lead, already enforced to reach its on_trash; this reaches only documents that lead owns
