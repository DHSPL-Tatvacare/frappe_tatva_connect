# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class TatvaMCPReadableTable(Document):
	pass  # one doctype the MCP server may read rows of, listed on its parent (Tatva Platform Settings)
