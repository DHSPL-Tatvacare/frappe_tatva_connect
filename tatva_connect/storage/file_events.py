# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt

"""File doc_events: offload bytes to Azure after insert, remove them on delete.

Runs on `after_insert` (the row + local file already exist, so core insert + thumbnail
logic is untouched), gated by the master switch. Folders and already-remote files are
skipped. `offload()` is shared with the backfill command.
"""

from tatva_connect.storage import blob_store
from tatva_connect.storage.blob_store import BlobStore

_DOMAIN_PRIVATE = {"CRM Lead", "WhatsApp Message", "CRM Enrolment Submission"}


def _is_settings_doctype(dt):
	return bool(dt) and dt.endswith("Settings")


def apply_privacy_policy(doc, method=None):
	"""Force file privacy by what it's attached to (runs on File.validate):
	  *Settings doctype  -> public  (logos/banners must render with no auth)
	  domain doctype     -> private (patient data is always gated)
	  anything else      -> leave the uploader's choice untouched.
	Storage is identical either way (one private Azure container); is_private only
	decides whether download_file gates the request (see storage/api.py)."""
	dt = doc.attached_to_doctype
	if _is_settings_doctype(dt):
		doc.is_private = 0
	elif dt in _DOMAIN_PRIVATE:
		doc.is_private = 1


def offload(doc) -> bool:
	"""Upload a local File's bytes to Azure and repoint the row at the proxy URL.
	ALL files offload (public + private) — one private container; is_private only
	gates serving (see storage/api.download_file). Folders and already-remote files
	are skipped."""
	if doc.is_folder or not blob_store.is_local_url(doc.file_url):
		return False

	store = BlobStore()
	key = store.new_key(doc.file_name, doc.attached_to_doctype, doc.attached_to_name)
	url = store.upload(key, doc.get_content(), doc.file_name)

	# Remove the local copy while file_url still points at it, then repoint the row.
	if store.settings.remove_local_after_upload:
		doc.delete_file_data_content()
	doc.db_set({"file_url": url, "custom_uploaded_to_azure": 1}, update_modified=False)
	return True


def after_insert(doc, method=None):
	if blob_store.is_enabled():
		offload(doc)


def on_trash(doc, method=None):
	if not doc.custom_uploaded_to_azure or not blob_store.is_enabled():
		return
	key = blob_store.blob_key_from_url(doc.file_url)
	if key:
		BlobStore().delete(key)
