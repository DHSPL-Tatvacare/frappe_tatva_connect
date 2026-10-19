# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document

from tatva_connect.access import invitations

SETTINGS = "Tatva Platform Settings"

# CRM Cohort Pace / Contact Cap / MCP Settings were folded into the Rate Limits tab; archived in .archive/2026-10-19-limits-into-platform-settings/.
# Refused rather than clamped; 0 means "use the default", so a site that never set the pace still saves.
PACE_BANDS = {"pace_chunk_size": (1, 500), "pace_interval_seconds": (10, 3600)}


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
		self.validate_limits()

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

	def validate_limits(self):
		"""The pace stays inside what a cohort can be paced at; an armed contact limit has a count and a period."""
		from tatva_connect.automation import contact_cap

		for fieldname, (low, high) in PACE_BANDS.items():
			value = self.get(fieldname)
			if value and not low <= value <= high:
				frappe.throw(_("{0} must be between {1} and {2}. {3} is outside what the pace can be set to.").format(
					self.meta.get_label(fieldname), low, high, value), title=_("Outside the range"))
		if not self.contact_cap_enabled:
			return
		if not contact_cap.MIN_CONTACTS <= (self.contact_cap_max_contacts or 0) <= contact_cap.MAX_CONTACTS:
			frappe.throw(_("Enter between {0} and {1} messages. {2} is outside what this cap can be set to.").format(
				contact_cap.MIN_CONTACTS, contact_cap.MAX_CONTACTS, self.contact_cap_max_contacts), title=_("Outside the range"))
		if (self.contact_cap_window_count or 0) < 1:
			frappe.throw(_("The period must be at least 1 {0}.").format(self.contact_cap_window_unit or _("day")),
			             title=_("Period too short"))


def site_pace() -> tuple[int, int]:
	"""`(patients per batch, seconds between batches)` — the ONE reader the workflow drain asks; a blank field reads its default."""
	from tatva_connect.workflow_engine import thresholds

	row = frappe.get_cached_doc(SETTINGS)
	return (
		row.pace_chunk_size or thresholds.DRAIN_CHUNK,
		row.pace_interval_seconds or thresholds.DRAIN_INTERVAL_SECONDS,
	)


def invitation_rule(allowed, blocked):
	"""The saved lists in words, as `invitations.assert_allowed` will apply them: Blocked always wins over Allowed."""
	if not allowed:
		return _("Invitations go to any address except those on: {0}.").format(", ".join(blocked))
	reachable = [d for d in allowed if d not in blocked]
	if not reachable:
		return _("No invitation can be sent: every allowed domain is also blocked, and Blocked always wins.")
	rule = _("Invitations go only to addresses on: {0}.").format(", ".join(reachable))
	return rule + " " + _("Blocked always wins over Allowed.") if blocked else rule
