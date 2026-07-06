# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""CRM Task class override — mirror of lead.crm_lead_override: add `parse_list_data` so the SPA
task list renders `custom_task_type` (a Link to the grain-composite-PK CRM Task Type) as the clean
`type_name`, never the raw `vertical::group::program::type_name` PK. Same ONE display-label brain
(lead.detail._display_label = target doctype's title_field). Purely additive subclass; A.1 order."""
import frappe
from crm.fcrm.doctype.crm_task.crm_task import CRMTask

from tatva_connect.lead.detail import _display_label

# CRM Task Link fields whose value is a composite `::` PK — show the title_field (type_name).
_LABEL_FIELDS = ("custom_task_type",)


class TatvaCRMTask(CRMTask):
	@staticmethod
	def parse_list_data(tasks):
		if not tasks:
			return tasks
		meta = frappe.get_meta("CRM Task")
		dfs = {fn: meta.get_field(fn) for fn in _LABEL_FIELDS}
		for row in tasks:
			for fn, df in dfs.items():
				val = row.get(fn)
				if val:
					label = _display_label(df, val)
					if label:
						row[fn] = label
		return tasks
