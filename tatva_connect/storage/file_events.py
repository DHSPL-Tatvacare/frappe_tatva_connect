# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt

"""File doc_events: offload bytes to Azure after insert, remove them on delete.

Offload runs SYNCHRONOUSLY on `after_insert` (not before_insert — core's validate() runs
after before_insert and calls validate_file_on_disk(), so the local file must still exist
then). Doing it synchronously here means `file_url` becomes the Azure proxy URL inside the
SAME transaction as the insert, so every consumer that reads the File afterwards (attach
fields, the Attachment comment, WhatsApp `attach`, email, intake) captures the proxy URL and
never a local `/files`|`/private/files` URL that would 404 once the local copy is removed.

No data loss, ever: the local copy is dropped only AFTER the transaction commits AND only
after the blob is re-confirmed in Azure (see `_drop_local_copy`). A rollback or an Azure
hiccup leaves the bytes safely local. `offload()` is shared with the backfill command.
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

	# Repoint the row + the Attachment-comment snapshot to the proxy IN THE CALLER'S transaction
	# (no commit of our own — a larger transaction that creates this File stays atomic). On commit,
	# file_url is the proxy for everyone; on rollback it reverts and the blob is a harmless orphan.
	doc.db_set({"file_url": url, "custom_uploaded_to_azure": 1}, update_modified=False)
	_repoint_attachment_comment(doc, old_url, url)

	# Drop the local copy only AFTER commit (enqueue_after_commit) and only once the blob is
	# re-confirmed (see _drop_local_copy) — bytes are never removed on an uncommitted/failed offload.
	if store.settings.remove_local_after_upload and local_path:
		frappe.enqueue(
			_drop_local_copy,
			queue="short",
			enqueue_after_commit=True,
			file_name=doc.name,
			local_path=local_path,
			blob_key=key,
		)
	return True


def _drop_local_copy(file_name, local_path, blob_key):
	"""Post-commit cleanup. Remove the local copy ONLY if the File is still marked offloaded
	AND the blob really exists in Azure. Any doubt → keep the local bytes (no data loss)."""
	if not frappe.db.get_value("File", file_name, "custom_uploaded_to_azure"):
		return
	if not BlobStore().exists(blob_key):
		return
	if local_path and os.path.exists(local_path):
		os.remove(local_path)


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


def _is_compose_draft(doc) -> bool:
	"""Files staged by the composer before the mail is sent — defer their offload until
	send (add_attachments then makes a Home/Attachments / Communication-scoped File we DO
	offload). Two cases: device uploads we stage UNATTACHED in "Home/Email Drafts", and any
	legacy composer upload that landed attached in folder "Home" (frappe-ui's default)."""
	if doc.folder == "Home/Email Drafts":
		return True
	return bool(doc.attached_to_doctype) and doc.folder == "Home"


def after_insert(doc, method=None):
	# Offload SYNCHRONOUSLY on the in-memory doc so file_url flips to the proxy before the insert
	# returns — every consumer then captures the proxy URL, never a local one (root-cause fix). The
	# row + the core Attachment comment already exist here (File.after_insert runs before this hook).
	if (
		blob_store.is_enabled()
		and not doc.is_folder
		and blob_store.is_local_url(doc.file_url)
		and not _is_compose_draft(doc)
	):
		try:
			offload(doc)
		except Exception:
			# Azure unreachable / transient: leave the file LOCAL and fully working — never break
			# the upload, never drop bytes. migrate_local_files() backfills it later.
			frappe.log_error(
				title="Azure offload failed (file left local)",
				message=f"file={doc.name}\n{frappe.get_traceback()}",
			)


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
	# Email/comment attachments copy a file's URL onto a Communication/Comment-scoped File,
	# so several rows share ONE blob. Drop the blob only on the LAST reference — else deleting
	# a sent-mail copy would orphan the original's bytes.
	if frappe.db.count("File", {"file_url": doc.file_url, "name": ["!=", doc.name]}):
		return
	try:
		BlobStore().delete(key)
	except Exception:
		frappe.log_error(
			title="Azure blob delete on File trash failed (orphan left in container)",
			message=f"file={doc.name} key={key}\n{frappe.get_traceback()}",
		)
