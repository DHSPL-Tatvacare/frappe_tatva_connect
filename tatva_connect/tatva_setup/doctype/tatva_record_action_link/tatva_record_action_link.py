# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""What a record action made for a record; written and read only through `api/record_actions.py`."""
import frappe
from frappe.model.document import Document


class TatvaRecordActionLink(Document):
	pass


def on_doctype_update():
	# Every read asks for one record's links.
	frappe.db.add_index("Tatva Record Action Link", ["reference_doctype", "reference_name"])
