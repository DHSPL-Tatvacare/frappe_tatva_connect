# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document

_FROZEN = ("rule", "version_no", "definition_hash", "payload_json", "action_count")


class CRMAutomationRuleVersion(Document):
	"""An immutable snapshot of one rule's program. Rows are minted by `automation.versions` and their
	DEFINITION is never edited afterwards — a running execution reads it from here, so a mutation would
	reintroduce exactly the drift versioning exists to remove.

	`is_current` is the one mutable column: it records which definition a NEW fire binds to, and moves
	when the rule is edited (or reverted). That is a fact about the rule's present, not about this
	frozen program, so it can move without the definition ever changing."""

	def before_save(self):
		if self.is_new():
			return
		changed = [f for f in _FROZEN if self.has_value_changed(f)]
		if changed:
			frappe.throw(
				_("An automation rule version's definition is immutable ({0} changed). Edit the rule; a new "
				  "version is minted on save.").format(", ".join(changed)),
				title=_("Immutable"),
			)
