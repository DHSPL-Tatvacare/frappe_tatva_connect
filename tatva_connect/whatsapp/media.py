"""WhatsApp media <-> File-on-Lead: the single brain shared by inbound webhook,
outbound send, and Refresh. Every WhatsApp media file ends in the SAME state:
attached to the CRM Lead, private, in Azure, stamped with the WATI message id,
and the WhatsApp Message's `attach` points at the File's proxy URL."""
import os

import frappe

_MEDIA_TYPES = {"image", "document", "video", "audio"}


def media_filename(media_type: str, text: str | None, data: str) -> str:
	"""Human filename for the File row. For documents WATI puts the ORIGINAL
	filename in `text`; for images `text` is a caption, so fall back to the uuid
	in the `data` path. Always keep a real extension."""
	if media_type == "document" and text:
		return text.strip()
	base = os.path.basename(data.split("?")[0]) or "file"  # '<uuid>.<ext>'
	return base


def find_lead_media(lead: str, wati_id: str):
	"""Existing File for this WATI message on this lead, or None (idempotency key)."""
	if not wati_id:
		return None
	name = frappe.db.exists(
		"File",
		{"attached_to_doctype": "CRM Lead", "attached_to_name": lead, "custom_wa_message_id": wati_id},
	)
	return frappe.get_doc("File", name) if name else None


def ensure_lead_media(lead: str, wati_id: str, filename: str, content: bytes):
	"""Idempotently create (or return) the File for this WATI media on the lead.
	Saved PRIVATE + attached to the LEAD -> privacy policy keeps it private and the
	File.after_insert hook offloads it to Azure. Returns the File doc."""
	existing = find_lead_media(lead, wati_id)
	if existing:
		return existing
	doc = frappe.get_doc({
		"doctype": "File",
		"file_name": filename,
		"attached_to_doctype": "CRM Lead",
		"attached_to_name": lead,
		"is_private": 1,
		"custom_wa_message_id": wati_id,
		"content": content,
	}).insert(ignore_permissions=True)
	return doc


def adopt_outbound_media(file_url: str, lead: str, wati_id: str):
	"""Outbound: the agent uploaded a File via the CRM tab (unattached -> offloaded
	under a hash key). After the send succeeds, re-home it onto the lead, force
	private, stamp the WATI id, and RE-KEY the blob into the lead folder so the
	container layout matches inbound (crm_lead/<lead>/...). Returns the File's
	(possibly new) proxy URL. Idempotent: re-keying a file already in the lead
	folder is a no-op."""
	name = frappe.db.exists("File", {"file_url": file_url})
	if not name:
		return file_url
	fd = frappe.get_doc("File", name)
	new_url = fd.file_url
	if getattr(fd, "custom_uploaded_to_azure", 0):
		from tatva_connect.storage.blob_store import BlobStore, blob_key_from_url

		store = BlobStore()
		old_key = blob_key_from_url(fd.file_url)
		new_key = store.new_key(fd.file_name, "CRM Lead", lead)
		if new_key.split("/")[0:2] != (old_key.split("/")[0:2] if old_key else []):
			new_url = store.upload(new_key, store.download(old_key), fd.file_name)
			store.delete(old_key)
	frappe.db.set_value("File", name, {
		"attached_to_doctype": "CRM Lead",
		"attached_to_name": lead,
		"attached_to_field": None,
		"is_private": 1,
		"custom_wa_message_id": wati_id,
		"file_url": new_url,
	}, update_modified=False)
	return new_url
