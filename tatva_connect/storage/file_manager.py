# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt

"""The single file-manager front door.

Every file the app creates, finds, re-homes, or links to goes through here, so naming, attachment,
the proxy URL, lookup, and re-keying are decided in ONE place — call sites pass only the params they
have and get a consistent result. This removes the per-site hand-rolled `frappe.get_doc({"File": ...})`
boilerplate + duplicated lookups that let behaviour drift across sites (e.g. the absolute-vs-relative
proxy-URL break).

Cross-cutting persistence — fail-closed privacy + Azure offload — is NOT re-implemented here: it is
enforced by the File doc_events (storage.file_events), which run for ANY File insert/save no matter who
creates it. This facade sits on top of that one bond; it never bypasses it.
"""

import frappe

from tatva_connect.storage import blob_store
from tatva_connect.storage.blob_store import BlobStore, blob_key_from_url


def save(
	content,
	*,
	filename,
	attached_to_doctype=None,
	attached_to_name=None,
	attached_to_field=None,
	private=True,
	meta=None,
):
	"""Create + persist a File from bytes. The doc_events apply the fail-closed privacy policy and
	offload to Azure — this only assembles the row. `meta` sets extra File fields (e.g.
	{"custom_wa_message_id": id}). Returns the File doc."""
	return frappe.get_doc({
		"doctype": "File",
		"file_name": filename,
		"content": content,
		"attached_to_doctype": attached_to_doctype,
		"attached_to_name": attached_to_name,
		"attached_to_field": attached_to_field,
		"is_private": 1 if private else 0,
		**(meta or {}),
	}).insert(ignore_permissions=True)


def link(file_url, *, attached_to_doctype, attached_to_name, private=True, meta=None):
	"""Surface an already-stored file on a record — a File row pointing at an existing blob URL (no
	re-upload), so the file also shows in that record's attachments. The privacy policy still runs."""
	return frappe.get_doc({
		"doctype": "File",
		"file_url": file_url,
		"attached_to_doctype": attached_to_doctype,
		"attached_to_name": attached_to_name,
		"is_private": 1 if private else 0,
		**(meta or {}),
	}).insert(ignore_permissions=True)


def find(**filters):
	"""The File matching `filters` (idempotency keys, attachment, etc.), or None."""
	name = frappe.db.exists("File", filters)
	return frappe.get_doc("File", name) if name else None


def proxy_url(file_or_key):
	"""The root-relative proxy URL for a blob key or a File doc. One place builds it (blob_store)."""
	key = file_or_key
	if hasattr(file_or_key, "file_url"):
		key = blob_key_from_url(file_or_key.file_url)
	return blob_store.download_url(key)


def by_blob_key(blob_key):
	"""Resolve a File from a blob key by its proxy URL — matched by KEY so a host change or an
	absolute↔relative URL difference never orphans a download. The bond-breakage fix, in ONE place."""
	exact = frappe.db.exists("File", {"file_url": blob_store.download_url(blob_key)})
	if exact:
		return exact
	esc = blob_key.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
	return frappe.db.exists("File", {"file_url": ["like", f"%?file_name={esc}"]})


def rehome(file, attached_to_doctype, attached_to_name, *, private=True, meta=None):
	"""Re-attach an existing File to a record and re-key its blob into that record's folder (one folder
	per record in the container). Idempotent — a file already in the target folder only re-points its
	row. Returns the (possibly new) proxy URL. Centralises WhatsApp's outbound 'adopt' so the re-key
	rule lives once."""
	new_url = file.file_url
	if getattr(file, "custom_uploaded_to_azure", 0):
		store = BlobStore()
		old_key = blob_key_from_url(file.file_url)
		new_key = store.new_key(file.file_name, attached_to_doctype, attached_to_name)
		if (old_key or "").split("/")[0:2] != new_key.split("/")[0:2]:
			new_url = store.upload(new_key, store.download(old_key), file.file_name)
			store.delete(old_key)
	frappe.db.set_value(
		"File",
		file.name,
		{
			"attached_to_doctype": attached_to_doctype,
			"attached_to_name": attached_to_name,
			"attached_to_field": None,
			"is_private": 1 if private else 0,
			"file_url": new_url,
			**(meta or {}),
		},
		update_modified=False,
	)
	return new_url
