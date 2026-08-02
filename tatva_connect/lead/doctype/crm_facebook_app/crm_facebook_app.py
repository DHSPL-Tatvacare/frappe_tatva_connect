# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""One Meta app. A Facebook token is issued by exactly one app, so this is what a token's holder names.

An app is NOT the owner of a Page: a Business owns Pages and Apps alike, and an App is merely granted
access to a Page. Several apps under one Business is the normal shape, which is why this is a table and
not a Single, and why nothing here links to a Page.
"""
import frappe
from frappe.model.document import Document

_BASE = "https://graph.facebook.com"


class CRMFacebookApp(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		app_id: DF.Data
		app_name: DF.Data
		app_secret: DF.Password
		graph_api_version: DF.Literal["v23.0", "v24.0", "v25.0"]
		lead_page_size: DF.Int
	# end: auto-generated types

	def validate(self):
		if not self.lead_page_size or self.lead_page_size < 1:
			frappe.throw(frappe._("Leads Fetched Per Request must be at least 1."))

	def api_url(self, endpoint: str) -> str:
		"""A Graph URL at THIS app's chosen version. Version is per app so one can be moved forward alone."""
		return f"{_BASE}/{self.graph_api_version}/{endpoint.lstrip('/')}"

	def secret(self) -> str:
		return self.get_password("app_secret", raise_exception=False) or ""

	def inspector(self) -> str:
		"""The app access token Graph accepts as the asker of a debug_token question, "" when unusable.
		It is the only credential that can describe a token which has already lapsed."""
		secret = self.secret()
		return f"{self.app_id}|{secret}" if (self.app_id and secret) else ""
