# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt

from frappe.model.document import Document

from tatva_connect.access import invitations


class TatvaPlatformSettings(Document):
	def validate(self):
		# Stored the way the rule reads it, so what the operator sees is exactly what is enforced.
		self.invitation_domains = "\n".join(invitations.domains_of(self.invitation_domains))
