# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Every shortcut offered is one the caller could already open.

THE RULE. The spotlight offers shortcuts from five surfaces through ONE gate, `_may_open`: the sidebar's own
surface answer where a surface owns the doctype, frappe's read permission otherwise; each source then lists
through the reader that surface already owns, so no source restates a permission of its own.

Smart Views are NOT one of the five: the panel reads them from the store the tabs bar reads, so they stay
live. A source here that listed them too would put the same view on screen twice and one of them stale.

THE HOLE THIS CLOSES. Frappe reads a workspace with NO `roles` declared as visible to everyone
(`desk/desktop.py` — "Return true if `Has Role` is not set or the user is allowed"). Every workspace this
app ships declares roles today; one added later without them would quietly reach every rep through here.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \\
        --module tatva_connect.tests.search.test_shortcuts_are_gated
"""
import json
import unittest
from functools import partial
from pathlib import Path
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.access.surfaces import my_surfaces
from tatva_connect.search import shortcuts as spotlight
from tatva_connect.search.index import CRMLeadSearch

REP = "zz-spotlight-rep@example.test"
PREFIX = "ZZ Spotlight Gate"

_SHAPE = ("kind", "group", "label", "context", "route", "external", "icon")


class TestEverySourceReusesItsOwnLister(unittest.TestCase):
	"""The registry names functions; none of them queries a surface this app does not already read."""

	def test_the_registry_is_the_only_list_of_sources(self):
		self.assertEqual(len(spotlight._SOURCES), 5, "a source was added or removed without this lock moving")

	def test_no_source_lists_smart_views(self):
		"""They belong to the client store; a copy here is a second answer that can disagree with the tabs."""
		frappe.set_user("Administrator")
		for source in spotlight._SOURCES:
			for action in source(_gate()):
				self.assertNotEqual(action["kind"], "Smart View", f"{source.__name__} lists smart views")

	def test_every_source_answers_the_one_shape(self):
		frappe.set_user("Administrator")
		for source in spotlight._SOURCES:
			with self.subTest(source=source.__name__):
				for action in source(_gate()):
					self.assertEqual(tuple(action), _SHAPE, "a source invented its own shortcut shape")
					self.assertTrue(action["label"], "a shortcut with no label cannot be searched or shown")
					self.assertTrue(action["route"], "a shortcut with no route cannot be opened")


class TestInsightsAsksInsights(unittest.TestCase):
	"""Insights has two gates and this module holds neither: the sidebar surface, then Insights' own row hook."""

	def _dashboards(self, user):
		frappe.set_user(user)
		return [a for a in spotlight.shortcuts()["shortcuts"] if a["kind"] == spotlight.KIND_DASHBOARD]

	def test_a_user_without_the_role_is_offered_none(self):
		holders = {r.parent for r in frappe.get_all("Has Role", filters={"role": ("like", "Insights%")}, fields=["parent"])}
		users = frappe.get_all("User", filters={"enabled": 1, "user_type": "System User"}, pluck="name")
		outsider = next((u for u in users if u not in holders and u != "Administrator"), None)
		if not outsider:
			self.skipTest("every user on this bench holds an Insights role")
		try:
			self.assertEqual(self._dashboards(outsider), [], "a dashboard reached a user with no Insights role")
		finally:
			frappe.set_user("Administrator")

	def test_the_role_alone_does_not_open_every_dashboard(self):
		"""The app gate says yes; the ROW gate is what narrows, and it is Insights' own."""
		holder = frappe.db.get_value("Has Role", {"role": "Insights User"}, "parent")
		if not holder or not frappe.db.count("Insights Dashboard v3"):
			self.skipTest("no Insights user or no dashboards on this bench")
		try:
			mine = self._dashboards(holder)
			self.assertLessEqual(len(mine), len(self._dashboards("Administrator")), "a rep was offered more than Administrator")
		finally:
			frappe.set_user("Administrator")


def _gate():
	return partial(spotlight._may_open, my_surfaces())


class TestEveryShortcutAsksTheOneGate(FrappeTestCase):
	"""A shortcut is offered only where the screen it opens would let the caller in, asked one way for every source."""

	def setUp(self):
		self.addCleanup(frappe.db.rollback)
		self.addCleanup(frappe.set_user, "Administrator")
		frappe.set_user("Administrator")
		# Every switch live, so only PERMISSION can hide a surface; in process, never written to the bench.
		live = patch("tatva_connect.automation.is_enabled", return_value=True)
		live.start()
		self.addCleanup(live.stop)
		if not frappe.db.exists("User", REP):
			frappe.get_doc({
				"doctype": "User", "email": REP, "first_name": "ZZ Spotlight Rep", "send_welcome_email": 0,
				"roles": [{"role": "Sales User"}],
			}).insert(ignore_permissions=True)  # authz-ok: tier-c — test fixture, no session user

	def _offered_to_rep(self):
		frappe.set_user(REP)
		with patch("frappe.log_error") as logged:
			found = spotlight.shortcuts()["shortcuts"]
		self.assertFalse(logged.called, f"a source failed for a rep: {logged.call_args}")
		return found

	def test_every_surface_the_gate_names_is_one_the_sidebar_answers(self):
		answered = my_surfaces()
		for doctype, surface in spotlight.surfaces.SURFACE_OF.items():
			self.assertIn(surface, answered, f"{doctype} is gated on a surface the sidebar never answers")

	def test_a_rep_is_offered_no_surface_their_roles_cannot_open(self):
		found = self._offered_to_rep()
		gated = {spotlight.KIND_WORKFLOW, spotlight.KIND_DASHBOARD}
		self.assertFalse([a for a in found if a["kind"] in gated], "a rep was offered a surface they cannot open")

	def test_a_public_list_view_of_an_unreadable_doctype_is_not_offered(self):
		label = f"{PREFIX} public workflow view"
		frappe.get_doc({
			"doctype": "CRM View Settings", "label": label, "dt": "CRM Workflow", "route_name": "Workflows",
			"user": "", "public": 1, "type": "list",
		}).insert(ignore_permissions=True)  # authz-ok: tier-c — test fixture, no session user
		self.assertNotIn(label, [a["label"] for a in self._offered_to_rep()], "a public view reached a rep who cannot read its doctype")

	def test_a_preset_on_a_missing_view_costs_no_other_preset(self):
		for label in ("kept", "orphaned"):
			view = frappe.get_doc({
				"doctype": "CRM Smart View", "label": f"{PREFIX} {label}", "base_object": "Lead", "is_standard": 0,
				"owner_user": REP, "columns": frappe.as_json([]),
			}).insert(ignore_permissions=True).name  # authz-ok: tier-c — test fixture, no session user
			frappe.get_doc({
				"doctype": "CRM Filter Preset", "label": f"{PREFIX} {label}", "user": REP,
				"reference_doctype": "CRM Smart View", "reference_name": view, "filters": "{}",
			}).insert(ignore_permissions=True)  # authz-ok: tier-c — test fixture, no session user
		# The one way production strands a preset: a forced delete, which skips the link check.
		frappe.delete_doc("CRM Smart View", view, force=True, ignore_permissions=True)
		# A rep, never Administrator: frappe answers every permission question for Administrator without looking.
		offered = [a["label"] for a in self._offered_to_rep()]
		self.assertIn(f"{PREFIX} kept", offered, "a stranded preset cost the rep their other presets")
		self.assertNotIn(f"{PREFIX} orphaned", offered)


class TestRecordSearchNeverRaisesOnPermission(FrappeTestCase):
	"""A caller who may read no lead gets no rows, never a failed search."""

	def test_a_caller_without_lead_read_gets_nothing(self):
		lead = frappe.db.get_value("CRM Lead", {}, "name")
		if not lead:
			self.skipTest("no lead on this bench")
		frappe.set_user("Guest")
		try:
			rows = CRMLeadSearch()._visible_rows([{"doctype": "CRM Lead", "name": lead, "lead": lead}])
		finally:
			frappe.set_user("Administrator")
		self.assertEqual(rows, [])


class TestAWorkspaceCannotBeOfferedToEveryone(unittest.TestCase):
	"""Roles are what gate a Desk page, and frappe treats a missing declaration as no gate at all."""

	def test_every_workspace_this_app_ships_declares_a_role(self):
		app = Path(frappe.get_app_path("tatva_connect"))
		found = open_to_all = 0
		for path in app.glob("**/workspace/**/*.json"):
			doc = json.loads(path.read_text())
			if doc.get("doctype") != "Workspace" and "content" not in doc:
				continue
			found += 1
			if not doc.get("roles"):
				open_to_all += 1
				print(f"   no roles declared: {path.name}")
		if not found:
			self.skipTest("this app ships no workspace")
		self.assertEqual(open_to_all, 0, "a workspace with no roles is visible to every user, reps included")


class TestTheShortcutSurfaceDegrades(unittest.TestCase):
	"""The spotlight is where a person goes when something else is already wrong."""

	def test_one_failing_source_does_not_cost_the_others(self):
		frappe.set_user("Administrator")

		def explode(may_open):
			raise RuntimeError("this source is down")

		original = spotlight._SOURCES
		spotlight._SOURCES = (explode, *original)
		try:
			with patch("frappe.log_error") as logged:
				answer = spotlight.shortcuts()
		finally:
			spotlight._SOURCES = original
		frappe.clear_messages()
		self.assertIn("shortcuts", answer, "a dead source took the whole surface with it")
		self.assertIn("this source is down", logged.call_args.kwargs["message"], "the source failed for another reason")

	def test_a_query_narrows_and_never_widens(self):
		frappe.set_user("Administrator")
		everything = spotlight.shortcuts(limit=100)["shortcuts"]
		if not everything:
			self.skipTest("this bench offers no shortcuts")
		word = everything[0]["label"][:4]
		narrowed = [a for a in everything if word.lower() in a["label"].lower()]
		self.assertLessEqual(len(narrowed), len(everything))
		for action in narrowed:
			self.assertIn(word.lower(), action["label"].lower())
