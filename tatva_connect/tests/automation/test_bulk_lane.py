# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Gate: a bulk job runs in the lane its own row names (handbook ADR 01), and live work is untouched by that."""
import unittest

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.automation import settings
from tatva_connect.automation.registry import is_guard
from tatva_connect.lead.assignment import LEAD_OWNER, TASK_ASSIGNEE

BULK_JOB = "tatva_connect.api.partner_bulk_worker.process_job"
KEY = "Task::Review::mirror"  # a real row and not a guard; any effect toggle would do
GUARD = "Lead::CRM Lead::dedup"  # a data check the registry marks `guard`
_JOBS = []  # CRM Bulk Job rows this module made, removed in tearDown (a toggle flip commits them)


def _bulk_job(lane):
	"""A real CRM Bulk Job row, through the document API, so the lane is read exactly as the worker's job is."""
	doc = frappe.get_doc({"doctype": "CRM Bulk Job", "partner": "Administrator", "operation": "lead_create",
	                      "status": "Open", "bulk_lane": lane})
	doc.insert(ignore_permissions=True)
	_JOBS.append(doc.name)
	return doc.name


def _as_job(lane=None, method=BULK_JOB):
	kwargs = {"bulk_job_id": _bulk_job(lane)} if lane is not None else {}
	frappe.local.job = frappe._dict(site=frappe.local.site, method=method, job_name="t", kwargs=kwargs)


def _as_request():
	if hasattr(frappe.local, "job"):
		frappe.local.job = None


def _toggle(key, enabled):
	frappe.db.set_value("CRM Tatva Automation", key, "enabled", enabled)
	frappe.db.commit()
	frappe.clear_cache()


class _Restores(FrappeTestCase):
	"""Every toggle a test flips is put back, and the process leaves the bulk lane."""

	KEYS = ()

	def setUp(self):
		self._before = {k: frappe.db.get_value("CRM Tatva Automation", k, "enabled") for k in self.KEYS}
		_as_request()

	def tearDown(self):
		for k, v in self._before.items():
			frappe.db.set_value("CRM Tatva Automation", k, "enabled", v)
		while _JOBS:
			frappe.delete_doc("CRM Bulk Job", _JOBS.pop(), force=True, ignore_permissions=True)
		frappe.db.commit()
		frappe.clear_cache()
		_as_request()


class TestBulkLane(_Restores):
	"""The lane itself: which process is asking, and what its job row says."""

	KEYS = (KEY, GUARD)

	def test_live_lane_is_unchanged(self):
		"""A rep's save behaves exactly as before. If this fails, the lane is leaking into live work."""
		_toggle(KEY, 1)
		self.assertTrue(settings.is_enabled(KEY))
		_toggle(KEY, 0)
		self.assertFalse(settings.is_enabled(KEY))

	def test_a_quiet_job_keeps_an_effect_off(self):
		_toggle(KEY, 1)
		_as_job(settings.QUIET)
		self.assertFalse(settings.is_enabled(KEY))

	def test_a_blank_lane_reads_as_quiet(self):
		_toggle(KEY, 1)
		_as_job("")
		self.assertFalse(settings.is_enabled(KEY))

	def test_a_job_naming_no_row_reads_as_quiet(self):
		_toggle(KEY, 1)
		_as_job()
		self.assertFalse(settings.is_enabled(KEY))

	def test_a_live_job_answers_as_live_work_does(self):
		_toggle(KEY, 1)
		_as_job(settings.LIVE)
		self.assertTrue(settings.is_enabled(KEY))
		_toggle(KEY, 0)
		self.assertFalse(settings.is_enabled(KEY), "the lane opened a toggle the operator turned off")

	def test_a_guard_runs_in_a_quiet_job(self):
		"""A data check is not a side effect: the dedup guard still refuses in a Quiet load."""
		self.assertTrue(is_guard(GUARD))
		self.assertFalse(is_guard(KEY))
		_toggle(GUARD, 1)
		_as_job(settings.QUIET)
		self.assertTrue(settings.is_enabled(GUARD))

	def test_a_guard_still_obeys_enabled(self):
		_toggle(GUARD, 0)
		_as_job(settings.QUIET)
		self.assertFalse(settings.is_enabled(GUARD))

	def test_an_unlisted_job_runs_in_the_live_lane(self):
		"""Fail-safe direction: forgetting to list a worker means 'behaves as today', never 'went quiet'."""
		_toggle(KEY, 1)
		_as_job(settings.QUIET, method="tatva_connect.some.other.job")
		self.assertTrue(settings.is_enabled(KEY))

	def test_a_bench_script_reads_as_live(self):
		"""The migration harness is a bench script with no `frappe.local.job`; it must NOT be bulk-laned."""
		_toggle(KEY, 1)
		frappe.local.job = None
		self.assertTrue(settings.is_enabled(KEY))
		self.assertFalse(settings._in_bulk_lane())

	def test_the_harness_entrypoint_is_not_listed(self):
		"""Locks the decision that the harness keeps its own pre-req gate instead of joining this lane."""
		self.assertNotIn("harness", " ".join(settings._BULK_JOBS))
		self.assertEqual(settings._BULK_JOBS, frozenset({BULK_JOB}))


class TestBulkLaneHierarchy(_Restores):
	"""A Live job answers as live work does, so a disabled ancestor still closes its child."""

	CHILD = "Workflow::Engine::sends"  # requires Workflow::Engine::run

	def setUp(self):
		from tatva_connect.automation.registry import parent_of
		self.parent = parent_of(self.CHILD)
		self.assertTrue(self.parent, "fixture assumes this row declares a parent")
		self.KEYS = (self.CHILD, self.parent)
		super().setUp()

	def test_ancestor_still_enforced_in_a_live_job(self):
		_toggle(self.CHILD, 1)
		_toggle(self.parent, 0)
		_as_job(settings.LIVE)
		self.assertFalse(settings.is_enabled(self.CHILD))


class TestForkAssignmentGate(_Restores):
	"""The fork assigns off a field. It asks a toggle, so the job's lane decides whether a bulk row is assigned."""

	KEYS = (LEAD_OWNER, TASK_ASSIGNEE)

	def setUp(self):
		super().setUp()
		self.made = []

	def tearDown(self):
		for name in self.made:
			for td in frappe.get_all(
				"ToDo", filters={"reference_type": "CRM Lead", "reference_name": name}, pluck="name"
			):
				frappe.delete_doc("ToDo", td, force=True, ignore_permissions=True, delete_permanently=True)
			frappe.delete_doc("CRM Lead", name, force=True, ignore_permissions=True, delete_permanently=True)
		super().tearDown()

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

	def test_toggle_on_a_named_owner_is_assigned(self):
		"""Today's behaviour, preserved. A red here means a rep's Assign silently stopped working."""
		_toggle(LEAD_OWNER, 1)
		self.assertEqual(self._todos(self._lead("Administrator").name), 1)

	def test_toggle_off_no_assignment_is_created(self):
		_toggle(LEAD_OWNER, 0)
		self.assertEqual(self._todos(self._lead("Administrator").name), 0)

	def test_a_quiet_job_creates_no_assignment(self):
		"""The 496-row incident, locked: a Quiet load assigns nobody."""
		_toggle(LEAD_OWNER, 1)
		_as_job(settings.QUIET)
		self.assertEqual(self._todos(self._lead("Administrator").name), 0)

	def test_a_live_job_assigns_as_live_work_does(self):
		_toggle(LEAD_OWNER, 1)
		_as_job(settings.LIVE)
		self.assertEqual(self._todos(self._lead("Administrator").name), 1)

	def test_a_lead_naming_nobody_is_untouched_either_way(self):
		"""The everyday path: no owner -> the fork never acts, so the toggle is irrelevant to it."""
		_toggle(LEAD_OWNER, 0)
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
		"""A Quiet job's one flag — frappe's own gate for Assignment Rule, Notification, webhooks and realtime."""
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
