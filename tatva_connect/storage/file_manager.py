# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt

"""The single file-manager front door.

Every file the app creates, finds, re-homes, or links to goes through here, so naming, attachment,
the proxy URL, lookup, and re-keying are decided in ONE place — call sites pass only the params they
have and get a consistent result. This removes the per-site hand-rolled `frappe.get_doc({"File": ...})`
boilerplate + duplicated lookups that let behaviour drift across sites (e.g. the absolute-vs-relative
proxy-URL break).

Cross-cutting persistence — fail-closed privacy + Azure offload — is NOT re-implemented here: privacy is
decided by FileOverride.before_insert and the offload by the File doc_events (storage.file_events), and
both run for ANY File insert no matter who creates it. This facade sits on top of that one bond; it never
bypasses it.
"""

from urllib.parse import parse_qs, urlparse

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
	meta=None,
):
	"""Create + persist a File from bytes. FileOverride.before_insert applies the fail-closed privacy
	policy and the doc_events offload to Azure — this only assembles the row. `meta` sets extra File
	fields (e.g. {"custom_wa_message_id": id}). Returns the File doc.

	Privacy is NOT a parameter. apply_privacy_policy (FileOverride.before_insert) overwrites is_private
	from the ONE checkpoint, so a `private=` argument here only looked like a decision — a caller could ask
	for public on a patient document and be silently overruled. Ask the checkpoint, not the caller."""
	return frappe.get_doc({
		"doctype": "File",
		"file_name": filename,
		"content": content,
		"attached_to_doctype": attached_to_doctype,
		"attached_to_name": attached_to_name,
		"attached_to_field": attached_to_field,
		**(meta or {}),
	}).insert(ignore_permissions=True)  # authz-ok: tier-b — the file front door; privacy floor is enforced by File doc_events


def link(file_url, *, attached_to_doctype, attached_to_name, meta=None):
	"""Surface an already-stored file on a record — a File row pointing at an existing blob URL (no
	re-upload), so the file also shows in that record's attachments. Privacy is decided by the checkpoint
	in FileOverride.before_insert, never by the caller (see save()).

	`file_size` / `content_hash` are carried across from the row that already owns this URL. Core derives
	them from bytes it writes, and a reference writes none, so it left them 0/None — a patient's
	prescription then read "0.00 B" on the lead while the blob was whole. They describe the BLOB, not the
	row, so copying them is the same fact restated for a second owner, never a measurement invented here.
	"""
	owned = frappe.db.get_value("File", {"file_url": file_url}, ["file_size", "content_hash"], as_dict=True)
	return frappe.get_doc({
		"doctype": "File",
		"file_url": file_url,
		"attached_to_doctype": attached_to_doctype,
		"attached_to_name": attached_to_name,
		**({"file_size": owned.file_size, "content_hash": owned.content_hash} if owned else {}),
		**(meta or {}),
	}).insert(ignore_permissions=True)  # authz-ok: tier-b — the file front door; privacy floor is enforced by File doc_events


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


def fetch_url(file_doc):
	"""The URL an OFF-SITE caller downloads from — fully qualified, and signed when it can be.

	`proxy_url` above is right for a browser already on this host and wrong for anyone else: it is
	root-relative, and it needs our session or API token to even start, so it cannot be opened, handed to
	a worker, or range-requested. An offloaded file therefore answers with the short-lived signed blob
	link the proxy would have redirected to anyway — same bytes, same permission already checked by the
	caller's own read, no credentials to pass on. A file still on local disk has no blob to sign, so it
	keeps the proxy route with the host filled in. Signing failure degrades the same way, never a 500."""
	key = None if blob_store.is_local_url(file_doc.file_url) else blob_key_from_url(file_doc.file_url)
	if not key:
		return frappe.utils.get_url(file_doc.file_url)
	return blob_store.BlobStore().sas_url(key)


def fetch_expires_at(file_doc):
	"""When the URL `fetch_url` just handed out stops working, or None for a link that never expires.

	Read off the token's own `se=` rather than computed as now+ttl: `sas_url` CACHES its token, so a
	second caller inside the window gets the first one's remaining life, and now+ttl would over-promise
	by however much of it had already elapsed. Calling `fetch_url` again is what makes the two agree —
	the cache answers it, so nothing is minted twice."""
	return parse_qs(urlparse(fetch_url(file_doc)).query).get("se", [None])[0]


def by_blob_key(blob_key):
	"""Resolve a File from a blob key by its proxy URL — matched by KEY so a host change or an
	absolute↔relative URL difference never orphans a download. The bond-breakage fix, in ONE place."""
	exact = frappe.db.exists("File", {"file_url": blob_store.download_url(blob_key)})
	if exact:
		return exact
	# The query comes from the ONE builder; escaping matters because an encoded key contains `%`, a LIKE wildcard.
	esc = blob_store.download_query(blob_key).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
	return frappe.db.exists("File", {"file_url": ["like", f"%?{esc}"]})


def rehome(file, attached_to_doctype, attached_to_name, *, meta=None):
	"""Re-attach an existing File to a record and re-key its blob into that record's folder (one folder
	per record in the container). Idempotent — a file already in the target folder only re-points its
	row. Returns the (possibly new) proxy URL. Centralises WhatsApp's outbound 'adopt' so the re-key
	rule lives once.

	Privacy is NOT an argument here. apply_privacy_policy runs on INSERT, so a re-home is outside it — a
	`private=` flag would have been a second authority deciding what is public. It calls the ONE checkpoint
	(file_events.may_be_public) instead, exactly as the insert path does.

	db_set, never save(): a re-homed file is already offloaded, so its url is an Azure PROXY url — one of
	core's own URL_PREFIXES (file.py:44) — and core's byte-mover handle_is_private_changed early-returns on
	is_remote_file (file.py:313). There are no local bytes to move; a save would only add a validate() pass
	that re-raises the enforce_public_file_restrictions 403, a modified bump and a Version row. db_set is
	core's own writer and updates the in-memory doc the caller still holds, in the same call."""
	from tatva_connect.storage.file_events import may_be_public

	new_url = file.file_url
	if getattr(file, "custom_uploaded_to_azure", 0):
		store = BlobStore()
		old_key = blob_key_from_url(file.file_url)
		new_key = store.new_key(file.file_name, attached_to_doctype, attached_to_name)
		if (old_key or "").split("/")[0:3] != new_key.split("/")[0:3]:
			new_url = store.upload(new_key, store.download(old_key), file.file_name)
			store.delete(old_key)
	file.db_set(dict({
		"attached_to_doctype": attached_to_doctype,
		"attached_to_name": attached_to_name,
		"attached_to_field": None,
		"file_url": new_url,
		"is_private": 0 if may_be_public(attached_to_doctype, attached_to_name) else 1,
	}, **(meta or {})), update_modified=False)
	return new_url
