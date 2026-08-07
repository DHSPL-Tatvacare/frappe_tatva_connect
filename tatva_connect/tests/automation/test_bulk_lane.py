# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Gate: a bulk job is QUIET, and live work is untouched by that.

A bulk import runs every row through the full document lifecycle, so a 5,000-row file used to raise
5,000 assignments, notifications, timeline rows and follow-up tasks. The fix is two mechanisms, and this
file holds both honest:

  * the LANE — `is_enabled` answers differently inside a listed bulk job, decided by `frappe.local.job`,
    which frappe sets itself and which is absent in a web request.
  * the GATE — `lead/assignment.py` mixins, so the fork's assign-off-a-field finally asks a switch.

The lane's whole safety claim is "live traffic cannot take the bulk branch because the value does not
exist there", so the live-lane tests matter as much as the quiet ones: a regression that makes everything
quiet would silently stop assigning real leads to real reps, and nothing else would go red.

Bench hygiene: `frappe.local.job` and every switch touched here are restored in tearDown.
"""
import unittest

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.automation import settings
from tatva_connect.lead.assignment import LEAD_OWNER, TASK_ASSIGNEE

BULK_JOB = "tatva_connect.api.partner_bulk_worker.process_job"
KEY = "Task::Assignment::followup"  # a real row; any switch would do — the lane is key-agnostic


def _as_job(method=BULK_JOB):
	frappe.local.job = frappe._dict(site=frappe.local.site, method=method, job_name="t", kwargs={})


def _as_request():
	if hasattr(frappe.local, "job"):
		frappe.local.job = None


class TestBulkLane(FrappeTestCase):
	"""The lane itself: which process is asking, and what the row says about bulk."""

	def setUp(self):
		self._before = {
			k: frappe.db.get_value("CRM Tatva Automation", KEY, k) for k in ("enabled", "bulk_lane")
		}
		_as_request()

	def tearDown(self):
		for k, v in self._before.items():
			frappe.db.set_value("CRM Tatva Automation", KEY, k, v)
		frappe.db.commit()
		frappe.clear_cache()
		_as_request()

	def _set(self, enabled, bulk_lane):
		frappe.db.set_value("CRM Tatva Automation", KEY, {"enabled": enabled, "bulk_lane": bulk_lane})
		frappe.db.commit()
		frappe.clear_cache()

	def test_live_lane_is_unchanged(self):
		"""A rep's save behaves exactly as before. If this fails, the lane is leaking into live work."""
		self._set(1, "")
		_as_request()
		self.assertTrue(settings.is_enabled(KEY))
		self._set(0, "")
		self.assertFalse(settings.is_enabled(KEY))

	def test_bulk_lane_is_quiet_by_default(self):
		"""Enabled, but `bulk_lane` unset -> off inside a bulk job. The restrictive default."""
		self._set(1, "")
		_as_job()
		self.assertFalse(settings.is_enabled(KEY))

	def test_bulk_lane_quiet_is_the_same_as_blank(self):
		self._set(1, "Quiet")
		_as_job()
		self.assertFalse(settings.is_enabled(KEY))

	def test_follow_live_reopens_it_for_bulk(self):
		self._set(1, "Follow live")
		_as_job()
		self.assertTrue(settings.is_enabled(KEY))

	def test_follow_live_still_obeys_enabled(self):
		"""The lane opens a gate; it never forces one. A disabled switch stays off in both lanes."""
		self._set(0, "Follow live")
		_as_job()
		self.assertFalse(settings.is_enabled(KEY))

	def test_an_unlisted_job_runs_in_the_live_lane(self):
		"""Fail-safe direction: forgetting to list a worker means 'behaves as today', never 'went quiet'."""
		self._set(1, "")
		_as_job(method="tatva_connect.some.other.job")
		self.assertTrue(settings.is_enabled(KEY))

	def test_a_bench_script_reads_as_live(self):
		"""The migration harness is a bench script with no `frappe.local.job`; it must NOT be bulk-laned."""
		self._set(1, "")
		frappe.local.job = None
		self.assertTrue(settings.is_enabled(KEY))
		self.assertFalse(settings._in_bulk_lane())

	def test_the_harness_entrypoint_is_not_listed(self):
		"""Locks the decision that the harness keeps its own pre-req gate instead of joining this lane."""
		self.assertNotIn("harness", " ".join(settings._BULK_JOBS))
		self.assertEqual(settings._BULK_JOBS, frozenset({BULK_JOB}))


class TestBulkLaneHierarchy(FrappeTestCase):
	"""A parent's `bulk_lane` must not decide a child's — the lane test does not recurse."""

	CHILD = "Workflow::Cohort::drain"  # requires Workflow::Engine::run

	def setUp(self):
		from tatva_connect.automation.registry import parent_of
		self.parent = parent_of(self.CHILD)
		self.assertTrue(self.parent, "fixture assumes this row declares a parent")
		self._before = {
			k: {f: frappe.db.get_value("CRM Tatva Automation", k, f) for f in ("enabled", "bulk_lane")}
			for k in (self.CHILD, self.parent)
		}
		_as_request()

	def tearDown(self):
		for k, vals in self._before.items():
			frappe.db.set_value("CRM Tatva Automation", k, vals)
		frappe.db.commit()
		frappe.clear_cache()
		_as_request()

	def test_ancestor_still_enforced_inside_the_bulk_lane(self):
		"""Child says Follow live, parent is off -> still off. The hierarchy is not bypassed by the lane."""
		frappe.db.set_value("CRM Tatva Automation", self.CHILD, {"enabled": 1, "bulk_lane": "Follow live"})
		frappe.db.set_value("CRM Tatva Automation", self.parent, {"enabled": 0})
		frappe.db.commit()
		frappe.clear_cache()
		_as_job()
		self.assertFalse(settings.is_enabled(self.CHILD))

	def test_a_parents_bulk_lane_does_not_speak_for_the_child(self):
		"""Parent Follow live, child blank -> child is still quiet in bulk."""
		frappe.db.set_value("CRM Tatva Automation", self.CHILD, {"enabled": 1, "bulk_lane": ""})
		frappe.db.set_value("CRM Tatva Automation", self.parent, {"enabled": 1, "bulk_lane": "Follow live"})
		frappe.db.commit()
		frappe.clear_cache()
		_as_job()
		self.assertFalse(settings.is_enabled(self.CHILD))


class TestForkAssignmentGate(FrappeTestCase):
	"""The fork assigns off a field. It now asks a switch — and must still work when that switch is on."""

	def setUp(self):
		self._before = {
			k: {f: frappe.db.get_value("CRM Tatva Automation", k, f) for f in ("enabled", "bulk_lane")}
			for k in (LEAD_OWNER, TASK_ASSIGNEE)
		}
		self.made = []
		_as_request()

	def tearDown(self):
		for name in self.made:
			for td in frappe.get_all(
				"ToDo", filters={"reference_type": "CRM Lead", "reference_name": name}, pluck="name"
			):
				frappe.delete_doc("ToDo", td, force=True, ignore_permissions=True, delete_permanently=True)
			frappe.delete_doc("CRM Lead", name, force=True, ignore_permissions=True, delete_permanently=True)
		for k, vals in self._before.items():
			frappe.db.set_value("CRM Tatva Automation", k, vals)
		frappe.db.commit()
		frappe.clear_cache()
		_as_request()

	def _switch(self, enabled, bulk_lane=""):
		frappe.db.set_value(
			"CRM Tatva Automation", LEAD_OWNER, {"enabled": enabled, "bulk_lane": bulk_lane}
		)
		frappe.db.commit()
		frappe.clear_cache()

	def _lead(self, owner):
		doc = frappe.get_doc({"doctype": "CRM Lead", "first_name": "zz-bulklane", "lead_owner": owner})
		doc.flags.ignore_mandatory = True
		doc.insert(ignore_permissions=True)
		frappe.db.commit()
		self.made.append(doc.name)
		return doc

	def _todos(self, name):
		return frappe.db.count("ToDo", {"reference_type": "CRM Lead", "reference_name": name})

	def test_the_override_is_actually_in_the_path(self):
		"""If hooks.py stops pointing at our class, every other test here would pass for the wrong reason."""
		from frappe.model.base_document import get_controller

		from tatva_connect.lead.assignment import LeadAssignmentGate, TaskAssignmentGate
		self.assertTrue(issubclass(get_controller("CRM Lead"), LeadAssignmentGate))
		self.assertTrue(issubclass(get_controller("CRM Task"), TaskAssignmentGate))

	def test_switch_on_a_named_owner_is_assigned(self):
		"""Today's behaviour, preserved. A red here means a rep's Assign silently stopped working."""
		self._switch(1)
		lead = self._lead("Administrator")
		self.assertEqual(self._todos(lead.name), 1)

	def test_switch_off_no_assignment_is_created(self):
		self._switch(0)
		lead = self._lead("Administrator")
		self.assertEqual(self._todos(lead.name), 0)

	def test_bulk_lane_silences_the_fork_assignment(self):
		"""Enabled for live, blank for bulk -> a bulk job creates no ToDo. The 496-row incident, locked."""
		self._switch(1, "")
		_as_job()
		lead = self._lead("Administrator")
		self.assertEqual(self._todos(lead.name), 0)

	def test_bulk_lane_follow_live_reassigns(self):
		self._switch(1, "Follow live")
		_as_job()
		lead = self._lead("Administrator")
		self.assertEqual(self._todos(lead.name), 1)

	def test_a_lead_naming_nobody_is_untouched_either_way(self):
		"""The everyday path: no owner -> the fork never acts, so the switch is irrelevant to it."""
		self._switch(0)
		doc = frappe.get_doc({"doctype": "CRM Lead", "first_name": "zz-bulklane-noowner"})
		doc.flags.ignore_mandatory = True
		doc.insert(ignore_permissions=True)
		frappe.db.commit()
		self.made.append(doc.name)
		self.assertEqual(self._todos(doc.name), 0)


class TestWorkerFlags(FrappeTestCase):
	"""Mechanism B: the bulk worker sets frappe's own flag, and NEVER one that would break the write.

	Asserted over the AST, never over the file's text. A grep for a flag NAME also matches the comment
	that explains why the flag is not used — so a text assertion fails on correct code and, worse, would
	pass on wrong code whose comment happened to be worded differently. Only an assignment is behaviour.
	"""

	@staticmethod
	def _assigned_flags(module):
		"""Every `frappe.flags.<name> = ...` the module performs, as a set of names."""
		import ast
		import inspect

		names = set()
		for node in ast.walk(ast.parse(inspect.getsource(module))):
			if not isinstance(node, ast.Assign):
				continue
			for target in node.targets:
				if (
					isinstance(target, ast.Attribute)
					and isinstance(target.value, ast.Attribute)
					and target.value.attr == "flags"
				):
					names.add(target.attr)
		return names

	def test_the_worker_sets_in_patch(self):
		"""The one flag mechanism B relies on — frappe's own gate for Assignment Rule, Notification,
		webhooks and realtime, none of which any Tatva switch reaches."""
		from tatva_connect.api import partner_bulk_worker
		self.assertIn("in_patch", self._assigned_flags(partner_bulk_worker))

	def test_the_worker_sets_no_other_in_flag(self):
		"""`in_import` disables the Select validator the field brain delegates to (api/_base.py:309), and
		`in_migrate`/`in_install` stop `creation` and `owner` being stamped (document.py:784). None of the
		family is available to the bulk lane; only `in_patch` is."""
		from tatva_connect.api import partner_bulk_worker
		assigned = self._assigned_flags(partner_bulk_worker)
		for flag in ("in_import", "in_migrate", "in_install"):
			self.assertNotIn(flag, assigned, f"{flag} must never be set in the bulk path")

	def test_the_detector_would_notice_a_planted_flag(self):
		"""Red control: a module that DOES set the forbidden flag is caught, and one that merely names it
		in a comment is not. Without this the two tests above could be passing on an empty set."""
		import linecache
		import sys
		import types

		def probe(source):
			module = types.ModuleType("zz_bulk_lane_probe")
			module.__file__ = "<probe>"
			sys.modules[module.__name__] = module
			# inspect.getsource reads the file named by __file__, so the probe is fed through linecache.
			linecache.cache[module.__file__] = (len(source), None, source.splitlines(True), module.__file__)
			try:
				return self._assigned_flags(module)
			finally:
				del sys.modules[module.__name__]
				linecache.cache.pop(module.__file__, None)

		self.assertEqual(probe("import frappe\nfrappe.flags.in_import = True\n"), {"in_import"})
		self.assertEqual(probe("# in_import is deliberately never set here\nx = 1\n"), set())


if __name__ == "__main__":
	unittest.main()
