"""Note modal support — read a note's file attachments for the unified note modal (lead/deal Notes
tab + the Notes main page). Permission-checked on the note; reuses the CRM's own attachment reader.

Linking + Azure lifecycle are NOT handled here: files are attached to the FCRM Note via the native
upload path, and the File doc_events (tatva_connect.storage.file_events) apply privacy (private unless
the doctype is operator-listed public — FCRM Note is not) and offload to Azure, with `file_url` already
the Azure proxy. This endpoint only lists what is attached so the modal can show + remove it.
"""
import frappe


@frappe.whitelist()
def note_attachments(note):
	frappe.has_permission("FCRM Note", "read", doc=note, throw=True)
	from crm.api.activities import get_attachments

	return get_attachments("FCRM Note", note)
