# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt

"""Insights' dashboard preview, written through the one file path every other app uses: bytes at insert."""

import frappe
from insights.insights.doctype.insights_dashboard_v3.insights_dashboard_v3 import (
	InsightsDashboardv3,
	generate_preview_key,
	get_page_preview,
)

from tatva_connect.access.insights_publish import assert_may_publish

DOCTYPE = "Insights Dashboard v3"


class TatvaInsightsDashboardv3(InsightsDashboardv3):
	@frappe.whitelist()
	def update_access(self, data: dict | str):
		# `is_public` is written by db_set, so no permlevel reaches it — the rule lives in access/insights_publish.
		assert_may_publish(self, frappe.parse_json(data))
		return super().update_access(data)

	def generate_dashboard_preview(self):
		# Upstream reserves an EMPTY File with ignore_validate and fills it later, so after_insert has no bytes to offload and the preview never leaves local disk.
		with generate_preview_key() as key:
			preview = get_page_preview(
				frappe.utils.get_url(f"/insights/shared/dashboard/{self.name}"),
				headers={"X-Insights-Preview-Key": key},
			)
		# No cache-buster: `BlobStore.new_key` mints a fresh hash per upload, so the URL already differs every run.
		self.db_set("preview_image", write_preview(preview, self.name))
		return self.preview_image


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
