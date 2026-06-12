# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt

"""Whitelisted endpoints: permission-gated download proxy, connection test, backfill."""

import frappe
from frappe import _

from tatva_connect.storage import blob_store
from tatva_connect.storage.blob_store import BlobStore
from tatva_connect.storage.file_events import offload


@frappe.whitelist(allow_guest=True)
def download_file(file_name: str):
	"""Proxy for an offloaded File: enforce Frappe's own permission, then redirect to a
	short-lived SAS link. `allow_guest` so public files work; private files are gated by
	`File.is_downloadable()` exactly as core Frappe gates `/private/files`."""
	from frappe.utils.response import download_private_file

	name = frappe.db.exists("File", {"file_url": blob_store.download_url(file_name)})
	if not name:
		raise frappe.DoesNotExistError

	doc = frappe.get_doc("File", name)
	if blob_store.is_local_url(doc.file_url):
		return download_private_file(doc.file_url)
	if doc.is_private and not doc.is_downloadable():
		raise frappe.PermissionError

	frappe.local.response["type"] = "redirect"
	frappe.local.response["location"] = BlobStore().sas_url(file_name)


@frappe.whitelist()
def test_connection() -> str:
	"""Settings button: prove the connection string reaches the account."""
	BlobStore().service.get_service_properties()
	return _("Connection to Azure Blob Storage succeeded.")


@frappe.whitelist()
def migrate_local_files(limit: int = 50) -> str:
	"""Backfill existing local files (oldest first). Idempotent — already-offloaded rows
	are filtered out. Re-run until it reports 0."""
	if not blob_store.is_enabled():
		frappe.throw(_("Enable Azure Storage first."))

	names = frappe.get_all(
		"File",
		filters={
			"custom_uploaded_to_azure": 0,
			"is_folder": 0,
			"file_url": ["like", "/%files/%"],   # both /files/ and /private/files/
		},
		pluck="name",
		order_by="creation asc",
		limit=int(limit),
	)
	done = sum(offload(frappe.get_doc("File", name)) for name in names)
	frappe.db.commit()
	return _("Offloaded {0} file(s). Re-run until it reports 0.").format(done)
