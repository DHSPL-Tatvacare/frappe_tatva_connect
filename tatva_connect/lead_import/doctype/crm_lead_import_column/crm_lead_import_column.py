# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class CRMLeadImportColumn(Document):
	# The section + field check lives on the PARENT (CRMLeadImport._validate_columns), which asks the same
	# mapping seam that fed the picker: a child controller's validate() is not invoked by a parent save.
	pass
