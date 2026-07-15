# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class CRMBulkJobResult(Document):
	"""One record's outcome in a CRM Bulk Job — created / merged / failed, addressed by its input row
	index. Standalone (not a child table) so a 50k-row job never loads its results with the parent."""
