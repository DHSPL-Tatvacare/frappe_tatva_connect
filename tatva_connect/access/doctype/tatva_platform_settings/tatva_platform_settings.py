# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document

from tatva_connect.access import invitations


class TatvaPlatformSettings(Document):
	def validate(self):
		# Stored the way the rule reads it, so what the operator sees is exactly what is enforced.
		self.invitation_domains = "\n".join(invitations.domains_of(self.invitation_domains))
		self.blocked_invitation_domains = "\n".join(invitations.domains_of(self.blocked_invitation_domains))
		allowed, blocked = self.invitation_domains.split(), self.blocked_invitation_domains.split()
		if allowed or blocked:
			frappe.msgprint(
				invitation_rule(allowed, blocked), title=_("Invitations from now on"), indicator="blue"
			)
		self.validate_app_documentation()

	def validate_app_documentation(self):
		"""Each row names an installed app once, and a space the wiki publishes."""
		installed, seen = set(frappe.get_installed_apps()), set()
		for row in self.app_documentation:
			row.app = (row.app or "").strip().lower()
			if row.app not in installed:
				frappe.throw(_("Row {0}: {1} is not an installed app. Installed: {2}.").format(row.idx, row.app, ", ".join(sorted(installed))))
			if row.app in seen:
				frappe.throw(_("Row {0}: {1} already has a documentation space.").format(row.idx, row.app))
			seen.add(row.app)
			if not frappe.db.get_value("Wiki Space", row.wiki_space, "is_published"):
				frappe.throw(_("Row {0}: {1} is not published, so its Docs could not open it.").format(row.idx, row.wiki_space))


def invitation_rule(allowed, blocked):
	"""The saved lists in words, as `invitations.assert_allowed` will apply them: Blocked always wins over Allowed."""
	if not allowed:
		return _("Invitations go to any address except those on: {0}.").format(", ".join(blocked))
	reachable = [d for d in allowed if d not in blocked]
	if not reachable:
		return _("No invitation can be sent: every allowed domain is also blocked, and Blocked always wins.")
	rule = _("Invitations go only to addresses on: {0}.").format(", ".join(reachable))
	return rule + " " + _("Blocked always wins over Allowed.") if blocked else rule
