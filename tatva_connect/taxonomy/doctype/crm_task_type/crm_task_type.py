# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt

from frappe.model.document import Document

from tatva_connect.taxonomy.normalize import normalize_field


class CRMTaskType(Document):
	def validate(self):
		# M-2: normalize the display value so "Apollo " / "apollo" never fork.
		normalize_field(self, "type_name")
