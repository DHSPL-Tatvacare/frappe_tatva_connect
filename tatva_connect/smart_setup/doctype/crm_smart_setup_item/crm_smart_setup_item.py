# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class CRMSmartSetupItem(Document):
	pass  # written only by the Check and Apply worker (smart_setup.api.run), never by hand
