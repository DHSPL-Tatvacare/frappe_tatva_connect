# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt

"""File doc_events: offload bytes to Azure after insert, remove them on delete.

Runs on `after_insert` (the row + local file already exist, so core insert + thumbnail
logic is untouched), gated by the master switch. Folders and already-remote files are
skipped. `offload()` is shared with the backfill command.
"""

import os
from urllib.parse import quote

import frappe

from tatva_connect.storage import blob_store
from tatva_connect.storage.blob_store import BlobStore

def _is_settings_doctype(dt):
	return bool(dt) and dt.endswith("Settings")


def _public_attachment_doctypes() -> set:
	"""Operator-listed doctypes whose attachments may be public (config, empty by default)."""
	raw = frappe.db.get_single_value("CRM Azure Storage Settings", "public_attachment_doctypes") or ""
	return {line.strip() for line in raw.replace(",", "\n").splitlines() if line.strip()}


def apply_privacy_policy(doc, method=None):
	"""Fail-closed file privacy (runs on File.validate): an attachment is PRIVATE unless its
	doctype is a *Settings doctype (logos/banners) or operator-listed public in CRM Azure
	Storage Settings. Unattached files keep the uploader's choice. is_private only gates
	serving (see storage/api.download_file); storage is one private container either way."""
	dt = doc.attached_to_doctype
	if not dt:
		return
	doc.is_private = 0 if (_is_settings_doctype(dt) or dt in _public_attachment_doctypes()) else 1


def offload(doc) -> bool:
	"""Upload a local File's bytes to Azure and repoint the row at the proxy URL.
	ALL files offload (public + private) — one private container; is_private only
	gates serving (see storage/api.download_file). Folders and already-remote files
	are skipped."""
	if doc.is_folder or not blob_store.is_local_url(doc.file_url):
		return False

	old_url = doc.file_url
	store = BlobStore()
	key = store.new_key(doc.file_name, doc.attached_to_doctype, doc.attached_to_name)
	local_path = doc.get_full_path()  # capture before repoint so we can drop the local copy after
	url = store.upload(key, doc.get_content(), doc.file_name)

	# Repoint the row to the Azure proxy and COMMIT before removing the local copy, so a crash
	# never leaves a row pointing at a deleted file (always readable: proxy if committed, else local).
	doc.db_set({"file_url": url, "custom_uploaded_to_azure": 1}, update_modified=False)
	frappe.db.commit()
	_repoint_attachment_comment(doc, old_url, url)
	if store.settings.remove_local_after_upload and local_path and os.path.exists(local_path):
		os.remove(local_path)
	return True


def _repoint_attachment_comment(doc, old_url, new_url):
	"""Frappe snapshots the file URL into an 'Attachment' Comment on the parent doc at
	attach time (the Activity-tab link reads it). After offload removes the local copy,
	that snapshot still points at /files|/private/files and 404s — so swap its href to
	our proxy URL, exactly once, matching the precise string Frappe wrote."""
	if not (doc.attached_to_doctype and doc.attached_to_name):
		return
	old_href = quote(old_url, safe="/:")  # mirrors file.create_attachment_record()
	for c in frappe.get_all(
		"Comment",
		filters={
			"comment_type": "Attachment",
			"reference_doctype": doc.attached_to_doctype,
			"reference_name": doc.attached_to_name,
		},
		fields=["name", "content"],
	):
		if c.content and old_href in c.content:
			frappe.db.set_value(
				"Comment", c.name, "content", c.content.replace(old_href, new_url), update_modified=False
			)


def after_insert(doc, method=None):
	# Offload in the background after commit: the File lands locally instantly (no UX delay,
	# survives an Azure outage), then a worker moves the bytes off and drops the local copy.
	if blob_store.is_enabled() and not doc.is_folder and blob_store.is_local_url(doc.file_url):
		frappe.enqueue(offload_file, queue="short", enqueue_after_commit=True, file_name=doc.name)


def offload_file(file_name):
	"""Background offload entry — never raises into the worker; on failure the File stays local."""
	try:
		offload(frappe.get_doc("File", file_name))
	except Exception:
		frappe.log_error(title="Azure offload failed (file left local)",
		                 message=f"file={file_name}\n{frappe.get_traceback()}")


def on_trash(doc, method=None):
	"""Delete the Azure blob when its File row is deleted. Cleanup keys off whether THIS
	file was offloaded (custom_uploaded_to_azure) — NOT the feature toggle: disabling the
	integration must stop new offloads, never strand already-offloaded blobs. The blob
	delete is idempotent (already-gone = success) and any remaining Azure error is logged,
	never raised — orphan-and-log beats wedging the File (and any parent-cascade) delete."""
	if not doc.custom_uploaded_to_azure:
		return
	key = blob_store.blob_key_from_url(doc.file_url)
	if not key:
		return
	try:
		BlobStore().delete(key)
	except Exception:
		frappe.log_error(
			title="Azure blob delete on File trash failed (orphan left in container)",
			message=f"file={doc.name} key={key}\n{frappe.get_traceback()}",
		)
