# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""Native CRM Task columns the automation engine may read/watch/set — the Task resource's own-column
brain (its per-task-type declared fields live on CRM Task Type Field). Grain derives from the task type,
never stored here. `can_set` is read by `activity.api.task_columns`, `can_watch` by the dispatcher's diff."""
import frappe
from frappe.model.document import Document


class CRMTaskField(Document):
	def validate(self):
		if not frappe.get_meta("CRM Task").get_field(self.fieldname):
			frappe.throw(
				frappe._("{0} is not a native column on CRM Task — declared per-task-type fields live on CRM Task Type Field.").format(self.fieldname),
				title=frappe._("Unknown CRM Task column"),
			)
