# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt

from frappe.model.document import Document

from tatva_connect.taxonomy.normalize import normalize_field


class CRMHospital(Document):
	def validate(self):
		# M-2: normalize the display value so variants converge. name is opaque (hash);
		# two real same-named hospitals can coexist (dedup = normalize + merge, no key).
		normalize_field(self, "hospital_name")
		normalize_field(self, "city")
