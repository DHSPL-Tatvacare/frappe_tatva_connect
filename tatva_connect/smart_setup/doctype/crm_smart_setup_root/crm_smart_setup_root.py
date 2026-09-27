# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class CRMSmartSetupRoot(Document):
	pass  # its type is stamped by the parent (CRMSmartSetup.validate): a parent save never runs a child's validate()
