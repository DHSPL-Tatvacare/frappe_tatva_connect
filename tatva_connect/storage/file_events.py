# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt

"""File doc_events: offload bytes to Azure after insert, remove them on delete.

Runs on `after_insert` (the row + local file already exist, so core insert + thumbnail
logic is untouched), gated by the master switch. Folders and already-remote files are
skipped. `offload()` is shared with the backfill command.
"""

from tatva_connect.storage import blob_store
from tatva_connect.storage.blob_store import BlobStore


def offload(doc) -> bool:
	"""Upload a local PRIVATE File's bytes to Azure and repoint the row at the proxy URL.
	Returns True if it offloaded, False otherwise. PUBLIC files are left on local disk
	untouched — so form logos/banners are served exactly as before and cannot break."""
	if doc.is_folder or not doc.is_private or not blob_store.is_local_url(doc.file_url):
		return False

	store = BlobStore()
	key = store.new_key(doc.file_name, doc.attached_to_doctype)
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
