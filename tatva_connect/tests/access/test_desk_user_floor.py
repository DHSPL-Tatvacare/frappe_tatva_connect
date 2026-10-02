# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Drift-lock on the Desk User grants, the floor every System User inherits at runtime.
An upgrade that widens the floor fails here; change it deliberately by editing the reviewed sets below."""

import frappe
from frappe.tests import IntegrationTestCase

# Desk User may read or select these; `User` is absent so the staff directory is never enumerable.
REVIEWED_READABLE = {
	"Calendar View", "Dashboard", "Dashboard Chart", "Dashboard Settings", "Desktop Icon",
	"Desktop Layout", "DocType Layout", "Document Follow", "Email Template", "Event", "Form Tour",
	"Google Calendar", "Google Contacts", "Kanban Board", "Letter Head", "List Filter",
	"Module Onboarding", "Network Printer Settings", "Note", "Number Card", "Onboarding Step",
	"Print Format", "Print Heading", "Reminder", "Report", "Submission Queue", "UTM Campaign",
	"UTM Medium", "UTM Source", "User Group", "Workflow Action", "Workflow State", "Workspace",
	"Workspace Sidebar",
}

# Desk User may write these; each is own-scoped or hook-scoped, so none writes a row another user reads.
REVIEWED_WRITABLE = {
	"Dashboard Settings", "Desktop Icon", "Desktop Layout", "Document Follow", "Event",
	"Google Calendar", "Google Contacts", "Kanban Board", "List Filter", "Note", "Reminder",
	"Workflow Action", "Workspace", "Workspace Sidebar",
}


def _live_sets():
	"""The effective Desk User floor now: Custom DocPerm REPLACES stock when it exists for a doctype."""
	readable, writable = set(), set()
	for src in ("Custom DocPerm", "DocPerm"):
		for p in frappe.get_all(
			src, filters={"role": "Desk User"},
			fields=["parent", "`read`", "`select`", "`write`", "`create`", "`delete`"],
		):
			auth = "Custom DocPerm" if frappe.db.exists("Custom DocPerm", {"parent": p.parent}) else "DocPerm"
			if src != auth:
				continue
			if p.read or p.select:
				readable.add(p.parent)
			if p.write or p.create or p.delete:
				writable.add(p.parent)
	return readable, writable


class TestDeskUserFloor(IntegrationTestCase):
	def test_readable_floor_has_not_drifted(self):
		live, _ = _live_sets()
		added = sorted(live - REVIEWED_READABLE)
		removed = sorted(REVIEWED_READABLE - live)
		self.assertEqual(
			live, REVIEWED_READABLE,
			f"Desk User read/select floor drifted. ADDED (review + gate or add to REVIEWED_READABLE): "
			f"{added}. REMOVED (a control tightened — update the set): {removed}.",
		)

	def test_writable_floor_has_not_drifted(self):
		_, live = _live_sets()
		added = sorted(live - REVIEWED_WRITABLE)
		removed = sorted(REVIEWED_WRITABLE - live)
		self.assertEqual(
			live, REVIEWED_WRITABLE,
			f"Desk User write/create/delete floor drifted. ADDED (review — is it own/hook scoped?): "
			f"{added}. REMOVED: {removed}.",
		)

	def test_user_directory_not_enumerable_by_desk_user(self):
		"""B1 sentinel — the staff directory must never be listable by the auto role again."""
		readable, _ = _live_sets()
		self.assertNotIn(
			"User", readable,
			"Desk User can enumerate the User directory again — B1 (baseline role trim) has regressed.",
		)
