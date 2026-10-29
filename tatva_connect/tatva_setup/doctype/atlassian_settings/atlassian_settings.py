# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""The Atlassian Cloud account every integration calls as; each integration reads its own section."""
import frappe
from frappe import _
from frappe.model.document import Document


class AtlassianSettings(Document):
	def validate(self):
		if self.site:
			self.site = self.site.strip().rstrip("/")
			if not self.site.startswith("https://"):
				frappe.throw(_("The site must start with https://."), title=_("Not an Atlassian address"))

	@frappe.whitelist()
	def check_connection(self):
		"""Who Atlassian says the saved account is; Atlassian's own refusal otherwise."""
		from tatva_connect.record_actions import jira

		return _("Connected as {0}.").format(jira.whoami())

	def on_update(self):
		from tatva_connect.record_actions import jira

		jira.refresh()  # another account sees other projects, people and labels
