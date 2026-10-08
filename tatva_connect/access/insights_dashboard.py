# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The one override of `Insights Dashboard v3`: its preview stored like every other file, and its filter values reachable by a guest when it is public."""
import inspect

import frappe
from insights.insights.doctype.insights_dashboard_v3.insights_dashboard_v3 import (
	InsightsDashboardv3,
	generate_preview_key,
	get_page_preview,
)
from insights.insights.doctype.insights_query_v3.insights_query_v3 import InsightsQueryv3
from insights.permissions import check_referenced_query_access

from tatva_connect.storage.insights_preview import write_preview


class TatvaInsightsDashboardv3(InsightsDashboardv3):
	def generate_dashboard_preview(self):
		# Upstream reserves an EMPTY File with ignore_validate and fills it later, so after_insert has no bytes to offload and the preview never leaves local disk.
		with generate_preview_key(self.name) as key:
			preview = get_page_preview(
				frappe.utils.get_url(f"/insights/shared/dashboard/{self.name}", allow_header_override=False),
				headers={"X-Insights-Preview-Key": key},
			)
		# No cache-buster: `BlobStore.new_key` mints a fresh hash per upload, so the URL already differs every run.
		self.db_set("preview_image", write_preview(preview, self.name))
		return self.preview_image

	@frappe.whitelist()
	def get_distinct_column_values(
		self, query: str, column_name: str, search_term: str | None = None, adhoc_filters: dict | None = None
	):
		# Upstream's query lookup checks the CALLER for an Insights role, which a guest never holds, so a public filter 403s into login; mirrors insights #1422, drop on upgrade.
		if frappe.session.user != "Guest":
			return super().get_distinct_column_values(query, column_name, search_term, adhoc_filters)
		if not self.is_public or not self.is_filter_column(query, column_name):
			raise frappe.PermissionError
		check_referenced_query_access(query)
		lookup = inspect.unwrap(InsightsQueryv3.get_distinct_column_values)  # authz-ok: tier-b — gated above: a public dashboard, a saved filter column, and the publisher's read on the query
		return lookup(
			frappe.get_cached_doc("Insights Query v3", query),
			column_name,
			search_term=search_term,
			adhoc_filters=adhoc_filters,
		)
