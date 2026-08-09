# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""One subject's journey through a frozen workflow version. Durable: it parks, resumes and retries, and it binds to a VERSION so an edit to the workflow can never re-route a journey already under way."""
from frappe.model.document import Document


class CRMWorkflowJourney(Document):
	@staticmethod
	def default_list_data():
		"""What a runs list shows before anyone opens the column picker, declared once where `crm.api.doc.get_data` already looks for it - so the columns, their widths and the fields fetched cannot disagree, and every one of them stays replaceable by the reader's own saved view."""
		columns = [
			{"label": "Run ID", "type": "Data", "key": "name", "width": "10rem"},
			# A Dynamic Link, so the cell reads the lead's name off `_link_titles` while the row keeps the id.
			{
				"label": "Lead",
				"type": "Dynamic Link",
				"key": "subject_name",
				"options": "subject_doctype",
				"width": "14rem",
			},
			{"label": "Status", "type": "Select", "key": "status", "width": "8rem"},
			{"label": "Step", "type": "Data", "key": "current_node", "width": "10rem"},
			{"label": "Started", "type": "Datetime", "key": "creation", "width": "10rem"},
		]

		# `subject_doctype` is fetched but never drawn: it is the field a Dynamic Link's target is read from.
		rows = [
			"name",
			"subject_doctype",
			"subject_name",
			"status",
			"current_node",
			"creation",
			"modified",
		]
		return {"columns": columns, "rows": rows}
