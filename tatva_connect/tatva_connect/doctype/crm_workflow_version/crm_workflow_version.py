# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt

from frappe import _
from frappe.model.document import Document

from tatva_connect.authoring import versions

_FROZEN = ("workflow", "version_no", "definition_hash", "payload_json", "node_count")


class CRMWorkflowVersion(Document):
	"""An immutable snapshot of one workflow's graph. Rows are minted by `workflow_engine.versions` and
	their DEFINITION is never edited afterwards - a running Journey reads it from here, so a mutation
	would reintroduce exactly the drift versioning exists to remove.

	`is_current` is the one mutable column: it records which definition a NEW Journey binds to, and moves
	when the Definition is edited (or reverted). That is a fact about the workflow's present, not about
	this frozen graph, so it can move without the definition ever changing."""

	@staticmethod
	def default_list_data():
		"""What the Versions tab shows before anyone opens the column picker, read by `crm.api.doc.get_data` as the runs list's is."""
		columns = [
			{"label": "Version", "type": "Int", "key": "version_no", "width": "7rem"},
			{"label": "Current", "type": "Check", "key": "is_current", "width": "6rem"},
			{"label": "Nodes", "type": "Int", "key": "node_count", "width": "6rem"},
			{"label": "Created By", "type": "Link", "key": "owner", "options": "User", "width": "12rem"},
			{"label": "Created On", "type": "Datetime", "key": "creation", "width": "10rem"},
			{"label": "Version ID", "type": "Data", "key": "name", "width": "12rem"},
		]
		rows = ["name", "version_no", "is_current", "node_count", "owner", "creation"]
		return {"columns": columns, "rows": rows}

	def before_save(self):
		versions.refuse_edit(self, _FROZEN, _("A workflow version's definition is immutable ({0} changed). Edit the Definition; a new "
		                                       "version is minted on save."))
