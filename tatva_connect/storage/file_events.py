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

from tatva_connect import automation
from tatva_connect.storage import blob_store
from tatva_connect.storage.blob_store import BlobStore


def _public_attachment_doctypes() -> set:
	"""Operator-listed doctypes whose attachments may be public (config, empty by default)."""
	raw = frappe.db.get_single_value("CRM Azure Storage Settings", "public_attachment_doctypes") or ""
	return {line.strip() for line in raw.replace(",", "\n").splitlines() if line.strip()}


def may_be_public(attached_to_doctype) -> bool:
	"""A file may be public ONLY if its record's doctype is on the operator's allowlist and the toggle is
	on. An unattached file belongs to no doctype, so it can never qualify — the floor, not a guess."""
	return bool(attached_to_doctype) and automation.is_enabled("Storage::File::privacy") \
		and attached_to_doctype in _public_attachment_doctypes()


def _is_external_link(file_url) -> bool:
	"""A file we do not hold: the "Web Link" tab points at somebody else's URL — no bytes ever reach us."""
	return bool(file_url) and not blob_store.is_local_url(file_url) \
		and not blob_store.blob_key_from_url(file_url)


def apply_privacy_policy(doc, method=None):
	"""THE privacy checkpoint (File.validate): private unless the doctype is on the operator's allowlist.

	The caller never decides — a rep cannot make a patient document public by ticking a box, and no other
	code may write is_private. Unattached = no doctype = private (the floor); the allowlist is re-applied
	by link_attach_fields the moment the bond is made and the doctype is finally known. The floor governs
	the files we STORE: an external link is not ours to lock. Toggle OFF removes the public exceptions
	only — it can make a file MORE private, never leak one (invariant 15, fail-closed)."""
	if _is_external_link(doc.file_url):
		doc.is_private = 0  # not ours to lock — say so plainly rather than draw a padlock over a public URL
		return
	doc.is_private = 0 if may_be_public(doc.attached_to_doctype) else 1


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


def after_insert(doc, method=None):
	# Offload SYNCHRONOUSLY on the in-memory doc so file_url flips to the proxy before the insert
	# returns — every consumer then captures the proxy URL, never a local one (root-cause fix). The
	# row + the core Attachment comment already exist here (File.after_insert runs before this hook).
	# UNCONDITIONAL: every real file offloads — no folder/draft special-case (that was a per-uploader
	# lottery on frappe's default "Home" folder). A discarded draft or any delete is reclaimed by
	# on_trash (ref-counted, drops the blob on the last reference); reads resolve through
	# FileOverride.get_content, so offloaded bytes serve transparently — including the email SMTP
	# attach path, which reads attachment content via File.get_content.
	if (
		blob_store.is_enabled()
		and not doc.is_folder
		and blob_store.is_local_url(doc.file_url)
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


def link_attach_fields(doc, method=None):
	"""M1: bond an offloaded file to the record whose Attach field names it — core's linker (file/utils.py:325)
	skips remote URLs by design, so the bond is ours. Same lookups and idempotency as core's, one guard changed."""
	if doc.doctype == "File":
		return
	for df in doc.meta.get("fields", {"fieldtype": ["in", ["Attach", "Attach Image"]]}):
		value = doc.get(df.fieldname)
		if not blob_store.blob_key_from_url(value):
			continue  # local /files value (core links it) or empty
		if frappe.db.exists("File", {
			"file_url": value,
			"attached_to_name": doc.name,
			"attached_to_doctype": doc.doctype,
			"attached_to_field": df.fieldname,
		}):
			continue  # already bonded — idempotent, this hook rides every save
		unattached = frappe.db.exists("File", {
			"file_url": value,
			"attached_to_name": None,
			"attached_to_doctype": None,
			"attached_to_field": None,
		})
		if unattached:  # bond ONLY a free row: an email/comment alias of the same blob is already spoken for
			frappe.db.set_value("File", unattached, {
				"attached_to_name": doc.name,
				"attached_to_doctype": doc.doctype,
				"attached_to_field": df.fieldname,
				# The bond is the first moment the doctype is known, so it is where the allowlist can finally
				# be applied: an avatar/logo comes back out public, everything else stays on the floor.
				"is_private": 0 if may_be_public(doc.doctype) else 1,
			})


def on_trash(doc, method=None):
	"""Reclaim the Azure blob when the LAST File referencing it is deleted. Keyed on the
	file_url being an Azure proxy (blob_key_from_url yields a key) — NOT the
	custom_uploaded_to_azure flag and NOT the feature toggle. Any row pointing at a blob is a
	reference, including frappe core's add_attachments copies (sent-mail/comment attachments)
	that never carry our flag; gating on the flag would strand their bytes as orphans. A local
	(non-proxy) url yields no key -> no-op, so disabling offload never touches anything. The
	delete is idempotent (already-gone = success) and any Azure error is logged, never raised —
	orphan-and-log beats wedging the File (and any parent-cascade) delete."""
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
