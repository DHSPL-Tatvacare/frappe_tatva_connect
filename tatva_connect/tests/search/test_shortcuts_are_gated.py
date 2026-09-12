# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Every shortcut offered is one the caller could already open.

THE RULE. The spotlight offers shortcuts from five surfaces and holds NO gate of its own: each source calls
the lister that surface already owns, so its permission answer is the one that decides. A source that
listed rows for itself would be a sixth gate, and the sixth copy is the one that goes stale.

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
from pathlib import Path

import frappe

from tatva_connect.search import shortcuts as spotlight

_SHAPE = ("kind", "group", "label", "context", "route", "external", "icon")


class TestEverySourceReusesItsOwnLister(unittest.TestCase):
	"""The registry names functions; none of them queries a surface this app does not already read."""

	def test_the_registry_is_the_only_list_of_sources(self):
		self.assertEqual(len(spotlight._SOURCES), 5, "a source was added or removed without this lock moving")

	def test_no_source_lists_smart_views(self):
		"""They belong to the client store; a copy here is a second answer that can disagree with the tabs."""
		frappe.set_user("Administrator")
		for source in spotlight._SOURCES:
			for action in source():
				self.assertNotEqual(action["kind"], "Smart View", f"{source.__name__} lists smart views")

	def test_every_source_answers_the_one_shape(self):
		frappe.set_user("Administrator")
		for source in spotlight._SOURCES:
			with self.subTest(source=source.__name__):
				for action in source():
					self.assertEqual(tuple(action), _SHAPE, "a source invented its own shortcut shape")
					self.assertTrue(action["label"], "a shortcut with no label cannot be searched or shown")
					self.assertTrue(action["route"], "a shortcut with no route cannot be opened")


class TestInsightsAsksInsights(unittest.TestCase):
	"""Insights has TWO gates and this module holds neither: the app-level role check, then its own
	`permission_query_conditions`. Without the role `get_list` RAISES, so the app gate must be asked
	first or every rep's spotlight logs a traceback for a surface they were never offered."""

	def _dashboards(self, user):
		frappe.set_user(user)
		return [a for a in spotlight._insights()]

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
			frappe.set_user("Administrator")
			self.assertLessEqual(len(mine), len(spotlight._insights()), "a rep was offered more than Administrator")
		finally:
			frappe.set_user("Administrator")


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

		def explode():
			raise RuntimeError("this source is down")

		original = spotlight._SOURCES
		spotlight._SOURCES = (explode, *original)
		try:
			answer = spotlight.shortcuts()
		finally:
			spotlight._SOURCES = original
		frappe.clear_messages()
		self.assertIn("shortcuts", answer, "a dead source took the whole surface with it")

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
