# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""The record actions' settings: one tab per action, read by its module in `record_actions/`."""
import frappe
from frappe import _
from frappe.model.document import Document


class TatvaRecordActions(Document):
	def validate(self):
		if self.jira_site:
			self.jira_site = self.jira_site.strip().rstrip("/")
			if not self.jira_site.startswith("https://"):
				frappe.throw(_("The Jira site must start with https://."), title=_("Not a Jira address"))
