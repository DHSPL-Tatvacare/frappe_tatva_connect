# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document

from tatva_connect.access import invitations

SETTINGS = "Tatva Platform Settings"

# CRM Cohort Pace / Contact Cap / MCP Settings were folded into the Rate Limits tab; archived in .archive/2026-10-19-limits-into-platform-settings/.
# Refused rather than clamped; 0 means "use the default", so a site that never set a value still saves.
BANDS = {"pace_chunk_size": (1, 500), "pace_interval_seconds": (10, 3600), "presence_idle_minutes": (1, 120), "auto_away_minutes": (1, 120)}


class TatvaPlatformSettings(Document):
	def validate(self):
		self.validate_invitation_domains()
		self.validate_app_documentation()
		self.validate_readable_tables()
		self.validate_limits()
		self.warn_inert_presence()

	def warn_inert_presence(self):
		"""Say, without refusing the save, when a presence field is set but nothing reads it yet; Settings are filled before switches are flipped."""
		from tatva_connect.lead import checkin
		from tatva_connect.notifications import push

		if self.presence_idle_minutes and not push._live():
			frappe.msgprint(_("Push alerts are off, so Idle After has no effect yet."), indicator="orange", title=_("Not in use yet"))
		if self.auto_away_minutes and not checkin._member_rows():
			frappe.msgprint(_("No pool requires check-in, so Auto-Away After affects no rep yet."), indicator="orange", title=_("Not in use yet"))

	def validate_invitation_domains(self):
		"""Each row stored the way the rule reads it, once, so what the operator sees is exactly what is enforced."""
		seen = set()
		for row in self.get(invitations.FIELD):
			row.domain = invitations.clean_domain(row.domain)
			if row.domain in seen:
				frappe.throw(_("Row {0}: {1} is already listed.").format(row.idx, row.domain))
			seen.add(row.domain)
		allowed, blocked = invitations.split(self.get(invitations.FIELD))
		if allowed or blocked:
			frappe.msgprint(
				invitation_rule(allowed, blocked), title=_("Invitations from now on"), indicator="blue"
			)

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

	def validate_readable_tables(self):
		"""Each row names a table once, and a parent: a child table is read through the parent that holds it."""
		seen = set()
		for row in self.mcp_readable_tables:
			if row.document_type in seen:
				frappe.throw(_("Row {0}: {1} is already listed.").format(row.idx, row.document_type))
			if frappe.get_meta(row.document_type).istable:
				frappe.throw(_("Row {0}: {1} is a child table. List the table that holds it.").format(row.idx, row.document_type))
			seen.add(row.document_type)

	def validate_limits(self):
		"""Each banded value stays inside its range; an armed contact limit has a count and a period."""
		from tatva_connect.automation import contact_cap

		for fieldname, (low, high) in BANDS.items():
			value = self.get(fieldname)
			if value and not low <= value <= high:
				frappe.throw(_("{0} must be between {1} and {2}. {3} is outside what it can be set to.").format(
					self.meta.get_label(fieldname), low, high, value), title=_("Outside the range"))
		self.validate_auto_away()
		if not self.contact_cap_enabled:
			return
		if not contact_cap.MIN_CONTACTS <= (self.contact_cap_max_contacts or 0) <= contact_cap.MAX_CONTACTS:
			frappe.throw(_("Enter between {0} and {1} messages. {2} is outside what this cap can be set to.").format(
				contact_cap.MIN_CONTACTS, contact_cap.MAX_CONTACTS, self.contact_cap_max_contacts), title=_("Outside the range"))
		if (self.contact_cap_window_count or 0) < 1:
			frappe.throw(_("The period must be at least 1 {0}.").format(self.contact_cap_window_unit or _("day")),
			             title=_("Period too short"))

	def validate_auto_away(self):
		"""Auto-Away After is never shorter than Idle After: a tab in use counts as here for Idle After, so a shorter wait could not take effect."""
		from tatva_connect import presence

		idle = self.presence_idle_minutes or presence.IDLE_SECONDS // 60
		if self.auto_away_minutes and self.auto_away_minutes < idle:
			frappe.throw(_("{0} must be at least {1} minutes, the {2}. A tab in use counts as here for that long, so a shorter wait could not take effect.").format(
				self.meta.get_label("auto_away_minutes"), idle, self.meta.get_label("presence_idle_minutes")), title=_("Too short"))


def site_pace() -> tuple[int, int]:
	"""`(patients per batch, seconds between batches)` — the ONE reader the workflow drain asks; a blank field reads its default."""
	from tatva_connect.workflow_engine import thresholds

	row = frappe.get_cached_doc(SETTINGS)
	return (
		row.pace_chunk_size or thresholds.DRAIN_CHUNK,
		row.pace_interval_seconds or thresholds.DRAIN_INTERVAL_SECONDS,
	)


def presence_idle_seconds() -> int:
	"""Seconds without input before a person reads as idle — the ONE reader presence asks; a blank field reads the code default."""
	from tatva_connect import presence

	minutes = frappe.get_cached_doc(SETTINGS).presence_idle_minutes
	return minutes * 60 if minutes else presence.IDLE_SECONDS


def auto_away_seconds() -> int:
	"""Seconds without input before a checked-in rep gets no lead (`checkin.in_app`); 0 is auto-away off (A.6: a blank field reads as off)."""
	return (frappe.get_cached_doc(SETTINGS).auto_away_minutes or 0) * 60


def invitation_rule(allowed, blocked):
	"""The saved grid in words, as `invitations.assert_allowed` will apply it; a domain is on one rule only."""
	if not allowed:
		return _("Invitations go to any address except those on: {0}.").format(", ".join(blocked))
	return _("Invitations go only to addresses on: {0}.").format(", ".join(allowed))
