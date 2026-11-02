# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class TatvaSmartSetupRoot(Document):
	pass  # its type is stamped by the parent (TatvaSmartSetup.validate): a parent save never runs a child's validate()
