# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt

"""One row per (user, record) that user may read. Written by access/record_access.py, never by hand.

No controller logic on purpose: this table is a DERIVED index of a rule that lives in one place. A
validate() here would be a second opinion about who may see what, and two opinions is the defect the
whole module exists to remove.
"""
from frappe.model.document import Document


class CRMRecordAccess(Document):
	pass
