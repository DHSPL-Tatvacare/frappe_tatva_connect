# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt

"""Whitelisted endpoints: permission-gated download proxy, connection test, backfill."""

import frappe
from frappe import _

from tatva_connect.storage import blob_store, file_access, file_manager
from tatva_connect.storage.blob_store import BlobStore
from tatva_connect.storage.file_events import offload


@frappe.whitelist(allow_guest=True)  # guest-ok: public file fetch (short-lived SAS link); PRIVATE files permission-gated inside (A.15)
def download_file(file_name: str, download: str | int | None = None):
	"""Proxy for an offloaded File: enforce permission, then redirect to a short-lived SAS link.
	`allow_guest` so public files work; a private file is gated by `file_access.may_read_blob`, which
	judges the BLOB across every row that references it — core's own `/private/files` rule, because a
	blob's rows do not share a parent and one row's verdict was never the blob's.

	`download=1` asks for a link the browser SAVES. It is a flavour of the same permission-gated route
	and not a second one — the check above still runs, and the File's own name becomes the saved name.
	It exists because the redirect leaves this origin: an `<a download>` is honoured only same-origin,
	so without this a Download control opens the audio in a tab instead of saving it."""
	from frappe.utils.response import download_private_file

	names = file_manager.rows_for_blob(file_name)
	if not names:
		raise frappe.DoesNotExistError

	doc = frappe.get_doc("File", names[0])
	if blob_store.is_local_url(doc.file_url):
		return download_private_file(doc.file_url)
	# EVERY row, not this one: a blob's rows do not share a parent, so one row's verdict is not the blob's.
	if not file_access.may_read_blob(file_name, names=names):
		raise frappe.PermissionError

	frappe.local.response["type"] = "redirect"
	frappe.local.response["location"] = BlobStore().sas_url(
		file_name, attachment_name=doc.file_name if frappe.utils.cint(download) else None
	)


@frappe.whitelist()
def test_connection() -> str:
	"""Settings button: prove the connection string reaches the account."""
	frappe.has_permission("CRM Azure Storage Settings", "read", throw=True)
	BlobStore().service.get_service_properties()
	return _("Connection to Azure Blob Storage succeeded.")


@frappe.whitelist()
def migrate_local_files(limit: int = 50) -> str:
	"""Backfill existing local files (oldest first). Idempotent — already-offloaded rows
	are filtered out. Re-run until it reports 0."""
	frappe.has_permission("CRM Azure Storage Settings", "write", throw=True)
	if not blob_store.is_enabled():
		frappe.throw(_("Enable Azure Storage first."))

	names = frappe.get_all(  # authz-ok: caller gated above (Storage Settings write); bulk maintenance over all files by design
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
