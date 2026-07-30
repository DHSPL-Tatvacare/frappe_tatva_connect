# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Gate: the automation switch HIERARCHY is enforced, not merely declared.

Twelve `Auto` rows name a parent in `requires`. Before this sweep `is_enabled` read one row's
`enabled` column and never looked at the parent, so ten of the twelve ran while the automation they
lean on was switched off — a reconciler polling a provider whose channel is dormant, a rollup reading
a request log that is no longer written. The declaration existed and nothing consulted it.

Every test here is PARAMETERISED OVER `AUTOMATIONS`. A hand-typed list of parents and children goes
stale the day a link is added, and a stale list proves nothing while looking authoritative — so the
registry is the only source of pairs, and the non-vacuity assertions at the top of each sweep make an
emptied registry fail loudly instead of passing silently.

What each failure means in product terms is stated in the test's own docstring.

Bench hygiene: every switch this file flips is restored in `tearDown`, and the seed test rolls its
whole transaction back. A switch left armed by a test is an automation running on a bench nobody
believes is armed — the constitution treats that as serious, not untidy.
"""
import os
import re
import unittest
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.automation import registry, seed
from tatva_connect.automation.registry import AUTOMATIONS, Auto, assert_valid_graph
from tatva_connect.automation.settings import is_enabled

_DOCTYPE = "CRM Tatva Automation"

# The hierarchy, derived from the registry and nowhere else.
_PARENT = {auto.key: auto.requires for auto in AUTOMATIONS}
_PAIRS = [(key, parent) for key, parent in _PARENT.items() if parent]
_PARENTS = sorted({parent for _, parent in _PAIRS})
_CHILDREN_OF = {p: sorted(k for k, q in _PAIRS if q == p) for p in _PARENTS}

# The seven renames — literal, because a dead key exists nowhere to be derived from; test_10 asserts every NEW key is really in the registry, so this table cannot rot into a check of two dead strings.
_RENAMES = {
	"Voice::Channel::calls": "AI Voice::Channel::calls",
	"Voice::Reconciler::catchup": "AI Voice::Channel::reconcile",
	"Telephony::Acefone::calls": "Telephony::Channel::calls",
	"Telephony::Acefone::reconcile": "Telephony::Channel::reconcile",
	"WhatsApp::Channel::backfill": "WhatsApp::Channel::reconcile",
	"Storage::Recording::catchup": "Storage::Recording::retry",
	"Task::Automation::sends": "Workflow::Engine::sends",
}

_SCANNED_SUFFIXES = (".py", ".json", ".md", ".txt", ".js", ".ts", ".vue", ".html", ".yaml", ".yml", ".csv")
_SKIP_DIRS = {"__pycache__", ".archive", "node_modules", "dist", "patches"}


def _app_root():
	return frappe.get_app_path("tatva_connect")


def _walk_source(skip_dirs, suffixes):
	"""Every text file in the app package, minus the directories a caller excludes."""
	for base, dirs, files in os.walk(_app_root()):
		dirs[:] = [d for d in dirs if d not in skip_dirs]
		for name in files:
			if name.endswith(suffixes):
				yield os.path.join(base, name)


def _ancestors(key):
	"""Every key above this one, nearest first — the chain `is_enabled` must consult."""
	chain, parent = [], _PARENT.get(key, "")
	while parent:
		chain.append(parent)
		parent = _PARENT.get(parent, "")
	return chain


class TestSwitchHierarchy(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		# The rows must exist to be flipped; seeding is idempotent and only runs when one is missing.
		if any(not frappe.db.exists(_DOCTYPE, auto.key) for auto in AUTOMATIONS):
			seed.sync_catalog()

	def setUp(self):
		super().setUp()
		self._before = {
			row.name: int(row.enabled or 0)
			for row in frappe.get_all(_DOCTYPE, fields=["name", "enabled"])
		}
		# Activators are orthogonal to the hierarchy — neutralised so a controller flip in these tests cannot enqueue a full search-index build.
		activator_patch = patch.object(registry, "activator_for", return_value="")
		activator_patch.start()
		self.addCleanup(activator_patch.stop)

	def tearDown(self):
		# Restore every switch this test touched — the bench must end exactly as dormant as it began.
		for name, was in self._before.items():
			if frappe.db.exists(_DOCTYPE, name) and int(frappe.db.get_value(_DOCTYPE, name, "enabled") or 0) != was:
				frappe.db.set_value(_DOCTYPE, name, "enabled", was, update_modified=False)
		super().tearDown()

	# --- helpers ---------------------------------------------------------

	def _set(self, key, value):
		"""Write the stored `enabled` column directly — no controller, so no cascade and no activator."""
		frappe.db.set_value(_DOCTYPE, key, "enabled", 1 if value else 0, update_modified=False)

	def _stored(self, key):
		return int(frappe.db.get_value(_DOCTYPE, key, "enabled") or 0)

	def _save_enabled(self, key, value):
		"""Flip through the controller — this is the seam `validate` and `on_update` hang off."""
		doc = frappe.get_doc(_DOCTYPE, key)
		doc.enabled = 1 if value else 0
		doc.save(ignore_permissions=True)  # authz-ok: tier-c — test fixture, no user context
		return doc

	# --- 1-2: the read path ---------------------------------------------

	def test_01_a_child_is_off_while_its_parent_is_off(self):
		"""The defect this sweep exists for: a reconciler polling a provider whose channel the
		operator switched off. Red here means a child automation still runs on its own row alone."""
		self.assertTrue(_PAIRS, "the registry declares no `requires` at all — this sweep would be vacuous")
		for child, parent in _PAIRS:
			with self.subTest(child=child, parent=parent):
				for ancestor in _ancestors(child):
					self._set(ancestor, 1)
				self._set(child, 1)
				self._set(parent, 0)
				self.assertFalse(
					is_enabled(child),
					f"{child} reports enabled while its parent {parent} is off",
				)

	def test_02_a_child_is_on_when_its_whole_chain_is_on(self):
		"""The other half: the gate must not be a blanket off. Red here means enabling the parent no
		longer enables the child, and an operator's switch does nothing."""
		self.assertTrue(_PAIRS, "the registry declares no `requires` at all — this sweep would be vacuous")
		for child, parent in _PAIRS:
			with self.subTest(child=child, parent=parent):
				for ancestor in _ancestors(child):
					self._set(ancestor, 1)
				self._set(child, 1)
				self.assertTrue(
					is_enabled(child),
					f"{child} reports dormant although it and {parent} are both on",
				)

	# --- 3-6: the write path (the controller) ----------------------------

	def test_03_switching_a_parent_off_takes_its_children_with_it(self):
		"""An operator kills a channel and expects everything leaning on it to stop. Red here means
		the stored rows still read ON, so the desk shows armed switches the read path silently ignores."""
		self.assertTrue(_PARENTS, "the registry declares no parents — this sweep would be vacuous")
		for parent in _PARENTS:
			children = _CHILDREN_OF[parent]
			with self.subTest(parent=parent):
				self._set(parent, 1)
				for child in children:
					self._set(child, 1)
				frappe.clear_messages()
				self._save_enabled(parent, 0)
				still_on = [c for c in children if self._stored(c)]
				self.assertEqual(still_on, [], f"switching {parent} off left {still_on} armed")
				reported = str(frappe.local.message_log)
				unreported = [c for c in children if c not in reported]
				self.assertEqual(
					unreported,
					[],
					f"switching {parent} off silently disarmed {unreported} — the operator was not told",
				)

	def test_04_switching_a_parent_off_is_never_refused(self):
		"""A kill switch must be reachable exactly when an incident needs it. Red here means the
		validation made the parent unswitchable while its children were armed — the worst moment."""
		self.assertTrue(_PARENTS, "the registry declares no parents — this sweep would be vacuous")
		for parent in _PARENTS:
			with self.subTest(parent=parent):
				self._set(parent, 1)
				for child in _CHILDREN_OF[parent]:
					self._set(child, 1)
				self._save_enabled(parent, 0)
				self.assertEqual(self._stored(parent), 0, f"{parent} could not be switched off")

	def test_05_a_child_cannot_be_saved_on_while_an_ancestor_is_off(self):
		"""An operator ticks a child, the save appears to work, and nothing happens — because the read
		path refuses it anyway. Red here means the desk lies about what is armed."""
		self.assertTrue(_PAIRS, "the registry declares no `requires` at all — this sweep would be vacuous")
		for child, parent in _PAIRS:
			with self.subTest(child=child, parent=parent):
				for ancestor in _ancestors(child):
					self._set(ancestor, 0)
				self._set(child, 0)
				with self.assertRaises(frappe.ValidationError) as caught:
					self._save_enabled(child, 1)
				self.assertIn(
					parent,
					str(caught.exception),
					f"refusing {child} without naming {parent} leaves the operator nothing to act on",
				)
				self.assertEqual(self._stored(child), 0, f"{child} was stored on despite the refusal")

	def test_06_switching_a_parent_on_arms_no_child(self):
		"""Dormant by default: arming a channel must not silently start its reconcilers too. Red here
		means enabling one switch armed automations the operator never chose."""
		self.assertTrue(_PARENTS, "the registry declares no parents — this sweep would be vacuous")
		for parent in _PARENTS:
			children = _CHILDREN_OF[parent]
			with self.subTest(parent=parent):
				for ancestor in _ancestors(parent):
					self._set(ancestor, 1)
				self._set(parent, 0)
				for child in children:
					self._set(child, 0)
				self._save_enabled(parent, 1)
				armed = [c for c in children if self._stored(c)]
				self.assertEqual(armed, [], f"switching {parent} on armed {armed} without being asked")

	# --- 7-8: the import-time shape gate ---------------------------------

	def test_07_a_requires_naming_an_undeclared_key_fails_the_graph_check(self):
		"""A typo'd or renamed parent must stop the app at import, not leave a child permanently and
		invisibly dormant because it waits on a row that does not exist."""
		# Built in-test — the real AUTOMATIONS list is never mutated.
		dangling = [
			Auto(key="Proof::Graph::parent", fires_on="Doc Event"),
			Auto(key="Proof::Graph::child", fires_on="Doc Event", requires="Proof::Graph::ghost"),
		]
		with self.assertRaises(ValueError) as caught:
			assert_valid_graph(dangling)
		self.assertIn("Proof::Graph::ghost", str(caught.exception))
		# And the real catalog passes the same check, so the gate is not simply always-red.
		assert_valid_graph(AUTOMATIONS)

	def test_08_a_cycle_fails_the_graph_check(self):
		"""A cycle would make the enabled-walk run forever on the first read. It is refused where the
		hierarchy is declared, which is why the runtime walk carries no guard."""
		mutual = [
			Auto(key="Proof::Cycle::a", fires_on="Doc Event", requires="Proof::Cycle::b"),
			Auto(key="Proof::Cycle::b", fires_on="Doc Event", requires="Proof::Cycle::a"),
		]
		with self.assertRaises(ValueError):
			assert_valid_graph(mutual)
		itself = [Auto(key="Proof::Cycle::self", fires_on="Doc Event", requires="Proof::Cycle::self")]
		with self.assertRaises(ValueError):
			assert_valid_graph(itself)

	# --- 9: the seed ------------------------------------------------------

	def test_09_the_seed_prunes_and_inserts_in_an_order_a_link_accepts(self):
		"""The seven renames ship with no patch at all — the seed alone must retire the old rows and
		lay the new ones down. Red here means a deploy either strands dead switches in the desk or
		dies part-way through, leaving the catalog half-written."""
		self.addCleanup(frappe.db.rollback)
		orphan, dead_parent, dead_child = "Proof::Retired::orphan", "Proof::Retired::parent", "Proof::Retired::child"

		for key, requires in ((orphan, ""), (dead_parent, ""), (dead_child, dead_parent)):
			doc = frappe.new_doc(_DOCTYPE)
			doc.automation_key = key
			doc.fires_on = "Doc Event"
			doc.area = key.split("::")[0]
			doc.set("requires", requires)
			doc.insert(ignore_permissions=True)  # authz-ok: tier-c — test fixture, no user context

		seed.sync_catalog()

		for key in (orphan, dead_parent, dead_child):
			self.assertFalse(
				frappe.db.exists(_DOCTYPE, key),
				f"{key} left the registry and the seed kept its row — a dead switch in the operator's desk",
			)

		# Now the insert order: tear the whole hierarchy out, children first, and make the seed rebuild it.
		for parent in _PARENTS:
			for child in _CHILDREN_OF[parent]:
				frappe.delete_doc(_DOCTYPE, child, force=True, ignore_permissions=True)  # authz-ok: tier-c — test fixture, no user context
			frappe.delete_doc(_DOCTYPE, parent, force=True, ignore_permissions=True)  # authz-ok: tier-c — test fixture, no user context

		seed.sync_catalog()

		for child, parent in _PAIRS:
			with self.subTest(child=child, parent=parent):
				self.assertTrue(frappe.db.exists(_DOCTYPE, parent), f"{parent} was not re-seeded")
				self.assertTrue(frappe.db.exists(_DOCTYPE, child), f"{child} was not re-seeded")
				self.assertEqual(frappe.db.get_value(_DOCTYPE, child, "requires"), parent)
				self.assertEqual(int(frappe.db.get_value(_DOCTYPE, child, "enabled") or 0), 0)

	# --- 10: the vocabulary ----------------------------------------------

	def test_10_no_old_key_literal_survives_in_the_app_source(self):
		"""A surviving old key is a switch that gates nothing: the row it names is gone, so the read
		is fail-closed dormant for ever and the automation is dead with no error anywhere."""
		for old, new in _RENAMES.items():
			self.assertIn(new, _PARENT, f"{new} is not a registry key — this rename table is stale")
			self.assertNotIn(old, _PARENT, f"{old} is still declared in the registry")

		here = os.path.abspath(__file__)
		patterns = {}
		for old, new in _RENAMES.items():
			# A rename that only PREFIXES the old key (Voice -> AI Voice) makes the new literal contain the old one; exclude that prefix so the scan reports real survivors only.
			prefix = new[: -len(old)] if new.endswith(old) else ""
			lookbehind = f"(?<!{re.escape(prefix)})" if prefix else ""
			patterns[old] = re.compile(lookbehind + re.escape(old))

		offenders = []
		for path in _walk_source(_SKIP_DIRS, _SCANNED_SUFFIXES):
			if os.path.abspath(path) == here:
				continue
			with open(path, encoding="utf-8", errors="ignore") as handle:
				for lineno, line in enumerate(handle, 1):
					for old, pattern in patterns.items():
						if pattern.search(line):
							offenders.append(f"{os.path.relpath(path, _app_root())}:{lineno} {old}")
		self.assertEqual(offenders, [], "old automation keys still in the source:\n" + "\n".join(offenders))

	# --- 11: the desk surface --------------------------------------------

	def test_11_requires_and_area_carry_their_desk_flags(self):
		"""Sixty flat rows with no parent column and no area filter is why nobody could see that one
		switch takes three others with it. Red here means the operator is back to guessing."""
		meta = frappe.get_meta(_DOCTYPE)
		requires = meta.get_field("requires")
		self.assertEqual(requires.fieldtype, "Link", "`requires` must link the parent row, not restate its key")
		self.assertEqual(requires.options, _DOCTYPE)
		self.assertEqual(requires.in_list_view, 1, "the parent must be a column in the switch list")
		self.assertEqual(requires.read_only, 1, "the hierarchy is registry-owned; only `enabled` is the operator's")
		self.assertEqual(
			meta.get_field("area").in_standard_filter,
			1,
			"sixty rows in one table need the area filter to be readable",
		)

	# --- 12: every switch is really read somewhere -----------------------

	def test_12_every_registry_key_is_referenced_by_production_code(self):
		"""A key nothing outside the catalog names is a switch an operator can flip with no effect —
		either the gate was never wired, or a rename left the reader pointing at a dead string."""
		app = _app_root()
		excluded_files = {
			os.path.join(app, "automation", "registry.py"),
			os.path.join(app, "automation", "seed.py"),
		}
		sources = {}
		for path in _walk_source(_SKIP_DIRS, (".py",)):
			parts = os.path.relpath(path, app).split(os.sep)
			if "tests" in parts or os.path.basename(path).startswith("test_") or path in excluded_files:
				continue
			with open(path, encoding="utf-8", errors="ignore") as handle:
				sources[path] = handle.read()

		self.assertTrue(sources, "the source scan found no production modules — the walk is broken")
		unread = [key for key in _PARENT if not any(key in text for text in sources.values())]
		self.assertEqual(unread, [], f"registry keys no production module names: {unread}")


	# --- 13: a migrate can never be failed by the arming rule -------------

	def test_13_the_seed_survives_a_row_armed_above_a_dormant_parent(self):
		"""THE MIGRATE TRAP. `validate` refuses arming above a dormant parent, and the seed re-saves every
		row to refresh its labels — so a row already in that state would fail `bench migrate` PART-WAY, with
		the catalog half-written and the rest of after_migrate unrun.

		Only a code change can create the state: declaring a NEW `requires` over a switch an operator had
		already armed. The desk cannot — arming is refused and a parent takes its children with it.

		Red here means a future thirteenth link turns the next deploy into a broken migrate.
		"""
		self.addCleanup(frappe.db.rollback)
		child, parent = _PAIRS[0]
		# Written straight to the column, which is the only way this state can exist — it is what a code
		# change adding a link looks like from the seed's point of view.
		self._set(parent, 0)
		self._set(child, 1)

		seed.sync_catalog()

		self.assertEqual(
			self._stored(child), 0,
			f"{child} stayed armed above dormant {parent} — the row claims on while `is_enabled` says off",
		)
		self.assertFalse(is_enabled(child))

	def test_13b_the_seed_never_arms_anything(self):
		"""The other direction, and it is the one that must never bend: the seed disarms to match reality
		and does nothing else. Code arming an automation is the failure the whole dormant-by-default rule
		exists to prevent."""
		self.addCleanup(frappe.db.rollback)
		before = {row.name: int(row.enabled or 0) for row in frappe.get_all(_DOCTYPE, fields=["name", "enabled"])}

		seed.sync_catalog()

		after = {row.name: int(row.enabled or 0) for row in frappe.get_all(_DOCTYPE, fields=["name", "enabled"])}
		armed = [k for k, v in after.items() if v and not before.get(k, 0)]
		self.assertEqual(armed, [], f"the seed ARMED {armed} — code must never turn an automation on")


if __name__ == "__main__":
	unittest.main()
