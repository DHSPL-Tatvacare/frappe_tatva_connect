# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document

_FROZEN = ("workflow", "version_no", "definition_hash", "payload_json", "node_count")


class CRMWorkflowVersion(Document):
	"""An immutable snapshot of one workflow's graph. Rows are minted by `workflow_engine.versions` and
	their DEFINITION is never edited afterwards - a running Journey reads it from here, so a mutation
	would reintroduce exactly the drift versioning exists to remove.

	`is_current` is the one mutable column: it records which definition a NEW Journey binds to, and moves
	when the Definition is edited (or reverted). That is a fact about the workflow's present, not about
	this frozen graph, so it can move without the definition ever changing."""

	def before_save(self):
		if self.is_new():
			return
		changed = [f for f in _FROZEN if self.has_value_changed(f)]
		if changed:
			frappe.throw(
				_("A workflow version's definition is immutable ({0} changed). Edit the Definition; a new "
				  "version is minted on save.").format(", ".join(changed)),
				title=_("Immutable"),
			)
