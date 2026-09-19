# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class CRMLeadImportColumn(Document):
	pass  # checked by the parent (CRMLeadImport._validate_columns): a parent save never runs a child's validate()
