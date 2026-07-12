# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document


class CRMTelephonyAgentMap(Document):
	"""A provider's agent identifier -> the CRM user it stands for.

	Needed only when the two differ. An agent whose provider email is also their CRM login resolves on
	its own and never needs a row here. The map exists because that is not always true: of 24 agents in
	a live capture, one used a corporate address, twenty a partner company's domain, and three personal
	Gmail accounts, and no rule can infer a CRM user from the last of those.

	Named `{account}::{email}` so one person may be a different user on two accounts, and so an email
	can never be mapped twice on the same account.
	"""

	def autoname(self):
		self.agent_email = (self.agent_email or "").strip().casefold()
		self.name = f"{self.telephony_account}::{self.agent_email}"

	def validate(self):
		self.agent_email = (self.agent_email or "").strip().casefold()
		if not frappe.db.get_value("User", {"name": self.user, "enabled": 1}):
			frappe.throw(
				_("{0} is not an enabled CRM user.").format(frappe.bold(self.user or "")),
				title=_("Invalid user"),
			)
