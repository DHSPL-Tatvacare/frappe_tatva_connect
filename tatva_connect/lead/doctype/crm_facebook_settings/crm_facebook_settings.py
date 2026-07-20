# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
import frappe
from frappe.model.document import Document


class CRMFacebookSettings(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		app_id: DF.Data | None
		app_secret: DF.Password | None
		graph_api_version: DF.Literal["v23.0", "v24.0", "v25.0"]
		lead_page_size: DF.Int
	# end: auto-generated types

	def validate(self):
		if not self.lead_page_size or self.lead_page_size < 1:
			frappe.throw(frappe._("Leads Fetched Per Request must be at least 1."))
