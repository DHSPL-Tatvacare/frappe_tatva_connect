# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt

"""Insights' dashboard preview, written through the one file path every other app uses: bytes at insert. The dashboard class that calls this is `access.insights_dashboard`."""

import frappe

DOCTYPE = "Insights Dashboard v3"


def write_preview(content: bytes, dashboard: str) -> str:
	"""One File, bytes and all, in a single insert — the same call crm, lms, helpdesk and wiki make."""
	drop_preview(dashboard)
	return (
		frappe.get_doc(
			{
				"doctype": "File",
				"file_name": f"{dashboard}-preview.jpeg",
				"content": content,
				"is_private": 1,
				"attached_to_doctype": DOCTYPE,
				"attached_to_name": dashboard,
			}
		)
		# authz-ok: tier-a — background worker (enqueue_doc from before_save); the File is pinned to the dashboard the save already gated
		.insert(ignore_permissions=True)
		.file_url
	)


def drop_preview(dashboard: str):
	"""The previous preview, deleted before the new one is written — `File.on_trash` reclaims its blob."""
	for name in frappe.get_all(
		"File",
		filters={"attached_to_doctype": DOCTYPE, "attached_to_name": dashboard},
		pluck="name",
	):
		# authz-ok: tier-a — same worker; only Files already attached to this dashboard are in scope
		frappe.delete_doc("File", name, force=True, ignore_permissions=True)
