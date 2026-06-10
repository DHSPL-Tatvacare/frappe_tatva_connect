# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt

from frappe.model.document import Document

from tatva_connect.taxonomy.normalize import normalize_field


class CRMDoctor(Document):
	def validate(self):
		# M-2: normalize the display value so "Dr. A Rao " / "dr. a rao" converge.
		# name is opaque (hash) now, so two real same-named doctors can coexist —
		# dedup is by normalization + the merge surface, not a unique key.
		normalize_field(self, "doctor_name")
		normalize_field(self, "city")
