# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document

from tatva_connect.taxonomy.normalize import normalize_field


class CRMState(Document):
	def validate(self):
		# M-2: normalize the display value so variants never fork.
		normalize_field(self, "state_name")
