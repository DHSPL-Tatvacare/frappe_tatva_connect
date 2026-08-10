# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Drift-lock on the DESK USER floor — the baseline every System User inherits.

`Desk User` is auto-appended to every System User at runtime (frappe/permissions.py get_roles), so its
grants are the floor a rep / student / agent starts from. A Frappe or app upgrade can silently WIDEN that
floor — re-add a `select`, or grant a new doctype — and nobody would notice until the next pentest. This
test freezes the REVIEWED floor and fails the build the moment the live floor drifts, so a widening is loud
instead of invisible. Same shape as tests/authz/test_bypass_writes.py (a source/state drift-lock, not a
runtime guard).

Baseline captured 2026-08-08, after B1 removed `Desk User` select on `User`. To change the floor
DELIBERATELY, update the set below in the same commit — that edit is the review record.
"""

import frappe
from frappe.tests import IntegrationTestCase

# Doctypes Desk User may READ or SELECT (appear in lists / link pickers).
# Reviewed: read-only reference data + each user's OWN desk personalisation. Note `User` is deliberately
# ABSENT — the staff directory must never be enumerable by the auto role (that is finding W2 / B1).
REVIEWED_READABLE = {
	"Calendar View", "Dashboard", "Dashboard Chart", "Dashboard Settings", "Desktop Icon",
	"Desktop Layout", "DocType Layout", "Document Follow", "Email Template", "Event", "Form Tour",
	"Google Calendar", "Google Contacts", "Kanban Board", "Letter Head", "List Filter",
	"Module Onboarding", "Network Printer Settings", "Note", "Number Card", "Onboarding Step",
	"Print Format", "Print Heading", "Reminder", "Report", "Submission Queue", "UTM Campaign",
	"UTM Medium", "UTM Source", "User Group", "Workflow Action", "Workflow State", "Workspace",
	"Workspace Sidebar",
}

# Doctypes Desk User may WRITE / CREATE / DELETE.
# Reviewed: every one is own-scoped (your layout, your notes, your reminders) or scoped by a frappe
# has_permission hook (Event, Workflow Action). None writes a row another user reads.
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
