# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt

from frappe import _
from frappe.model.document import Document

from tatva_connect.authoring import versions

_FROZEN = ("task_type", "version_no", "definition_hash", "payload_json", "question_count")


class CRMTaskTypeVersion(Document):
	"""An immutable snapshot of one task form, minted by `taxonomy.form_versions` on Publish; a task is read with the version
	it was answered on, so its definition never changes. `is_current` is the one mutable column: which version reps are offered."""

	@staticmethod
	def default_list_data():
		"""What the Versions list shows before anyone opens the column picker, read by `crm.api.doc.get_data`."""
		columns = [
			{"label": "Version", "type": "Int", "key": "version_no", "width": "7rem"},
			{"label": "Current", "type": "Check", "key": "is_current", "width": "6rem"},
			{"label": "Questions", "type": "Int", "key": "question_count", "width": "6rem"},
			{"label": "Created By", "type": "Link", "key": "owner", "options": "User", "width": "12rem"},
			{"label": "Created On", "type": "Datetime", "key": "creation", "width": "10rem"},
			{"label": "Version ID", "type": "Data", "key": "name", "width": "12rem"},
		]
		rows = ["name", "version_no", "is_current", "question_count", "owner", "creation"]
		return {"columns": columns, "rows": rows}

	def before_save(self):
		versions.refuse_edit(self, _FROZEN, _("A task form version's definition is immutable ({0} changed). Edit the form; a new "
		                                       "version is minted on publish."))
