# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Task 8 — the Require Location GUARD-lane verb (+ optional geofence), the severed
`tasks.enforce_location` backstop, and the "reject unregistered verbs" author-time gap-closer.

Real Frappe engine as the oracle: real saves, real throws, a spy (never a hardcoded verdict, S.6) on
`location.api.location_required` proving the guard consults the ONE existing brain rather than
reimplementing the required-decision (A.8).
"""
import unittest

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.automation import router, seed
from tatva_connect.location import api as location_api
from tatva_connect.tasks import tasks
from tatva_connect.tests.authz.grains import GRAINS, assert_masters_exist
from tatva_connect.tests.automation import field_allowlist

_DT = "CRM Automation Rule"
_RUN_LOG = "CRM Automation Run Log"
_GRAIN = GRAINS[0]
_ANCHOR_LAT, _ANCHOR_LNG = 12.9716, 77.5946  # an arbitrary clinic anchor (Bengaluru)
_SWITCHES = ("Task::Automation::rules", "Location::Google::capture", "Task::CRM Task::guards")


def _flip_switches(on):
	for key in _SWITCHES:
		frappe.db.set_value("CRM Tatva Automation", key, "enabled", 1 if on else 0)


def _make_lead(**extra):
	payload = {
		"doctype": "CRM Lead", "first_name": "GuardVerb", "lead_name": "GuardVerb Probe", "status": "New",
		"custom_vertical": _GRAIN["vertical"], "custom_group": _GRAIN["group"],
		"custom_current_program": _GRAIN["program"],
		"custom_clinic_latitude": _ANCHOR_LAT, "custom_clinic_longitude": _ANCHOR_LNG,
	}
	payload.update(extra)
	return frappe.get_doc(payload).insert(ignore_permissions=True)


def _make_task_type(name, **extra):
	pk = f"{_GRAIN['vertical']}::{_GRAIN['group']}::{_GRAIN['program']}::{name}"
	if frappe.db.exists("CRM Task Type", pk):
		return pk
	payload = {
		"doctype": "CRM Task Type", "type_name": name,
		"vertical": _GRAIN["vertical"], "group": _GRAIN["group"], "program": _GRAIN["program"],
		"visit_mode": "In-Person",
	}
	payload.update(extra)
	frappe.get_doc(payload).insert(ignore_permissions=True)
	return pk


def _make_rule(name, task_type, geofence_meters=None):
	action = {"action_type": "Require Location"}
	if geofence_meters is not None:
		action["geofence_meters"] = geofence_meters
	return frappe.get_doc({
		"doctype": _DT, "rule_name": name, "enabled": 1,
		"on_doctype": "CRM Task", "event": "Updated",
		"vertical": _GRAIN["vertical"], "group": _GRAIN["group"], "program": _GRAIN["program"],
		# Scoped to THIS task type (not just "any Done save in the grain") so the coverage helper's
		# "is this task type covered" check is genuinely exercised, not a grain-wide rubber stamp.
		# `status is Done` (not `changed to`) so the SAME criteria evaluate correctly whether the rule
		# fires through a real .save() (Todo->Done) or via a direct backstop call with no before-state.
		"criteria": [
			{"field": "custom_task_type", "operator": "is", "value": task_type},
			{"field": "status", "operator": "is", "value": "Done"},
		],
		"actions": [action],
	}).insert(ignore_permissions=True)


def _make_task(lead, task_type, **extra):
	# No `assigned_to` on purpose: native CRMTask.after_insert() calls assign_to() when it's set,
	# which does its own internal write on this SAME row (via frappe.desk.form.assign_to.add) outside
	# our held doc object — the very next .save() below would then see a stale in-memory `modified`
	# and raise TimestampMismatchError before validate even runs. Unrelated to the guard verb itself.
	payload = {
		"doctype": "CRM Task", "title": "GuardVerb task",
		"reference_doctype": "CRM Lead", "reference_docname": lead, "custom_task_type": task_type,
		"status": "Todo",
	}
	payload.update(extra)
	return frappe.get_doc(payload).insert(ignore_permissions=True)


def _track_grain(radius_m=100):
	"""Append + save a location_tracked_grains row for our test grain on the CRM Maps Settings
	singleton; returns a cleanup thunk that removes exactly that row."""
	settings = frappe.get_doc("CRM Maps Settings")
	settings.append("location_tracked_grains", {
		"vertical": _GRAIN["vertical"], "group": _GRAIN["group"], "program": _GRAIN["program"],
		"radius_m": radius_m,
	})
	settings.save(ignore_permissions=True)
	row_name = settings.location_tracked_grains[-1].name

	def _cleanup():
		frappe.db.delete("CRM Maps Tracked Grain", {"name": row_name})
		frappe.clear_document_cache("CRM Maps Settings", "CRM Maps Settings")

	return _cleanup


class _GuardVerbBase(FrappeTestCase):
	"""Shared fixture: a location-tracked grain, an In-Person task type, a lead anchored at
	(_ANCHOR_LAT, _ANCHOR_LNG), and the three switches this guard needs. Subclasses add their own rule
	+ task rows and clean those up themselves."""

	@classmethod
	def setUpClass(cls):
		assert_masters_exist()
		seed.sync_catalog()  # ensures every switch row this guard needs exists (idempotent)
		cls._orig_switches = {
			key: frappe.db.get_value("CRM Tatva Automation", key, "enabled") for key in _SWITCHES
		}
		_flip_switches(True)
		cls._untrack = _track_grain()
		cls.status_watchable = field_allowlist.seed_watchable("CRM Task", "status")
		cls.lead = _make_lead()

	@classmethod
	def tearDownClass(cls):
		frappe.db.delete(_RUN_LOG, {"lead": cls.lead.name})
		frappe.db.delete("CRM Task", {"reference_docname": cls.lead.name})
		frappe.db.delete("CRM Lead", {"name": cls.lead.name})
		frappe.db.delete(field_allowlist.DOCTYPE, {"name": cls.status_watchable})
		cls._untrack()
		for key, val in cls._orig_switches.items():
			frappe.db.set_value("CRM Tatva Automation", key, "enabled", val or 0)

	def setUp(self):
		# Synchronous dispatch (same convention as test_two_lane.py/test_watch_entry.py): makes
		# `frappe.enqueue(..., now=...)` run in-process instead of racing a REAL background worker
		# against this test's own save() calls (a genuine flake, not a guard-verb correctness issue).
		frappe.flags.in_test = True

	def tearDown(self):
		frappe.flags.in_test = False
		router.clear_live_doctypes_cache()


class TestRequireLocationBlocksMissingCoords(_GuardVerbBase):
	"""No captured coordinates on a Done save -> the guard raises IN validate, blocking the save
	before it ever reaches the DB. Spies on location.api.location_required to prove the guard consults
	the ONE existing brain rather than reimplementing the required-decision (A.8)."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.task_type = _make_task_type("GV-NoCoords")
		cls.rule = _make_rule("GV-NoCoords-rule", cls.task_type)

	@classmethod
	def tearDownClass(cls):
		frappe.db.delete("CRM Automation Action", {"parent": cls.rule.name})
		frappe.db.delete("CRM Automation Criterion", {"parent": cls.rule.name})
		frappe.db.delete(_DT, {"name": cls.rule.name})
		frappe.db.delete("CRM Task Type", {"name": cls.task_type})
		super().tearDownClass()

	def test_missing_coords_raises_in_validate_via_the_consulted_brain(self):
		task = _make_task(self.lead.name, self.task_type)
		calls = []
		orig = location_api.location_required

		def spy(task_type, lead, values):
			calls.append((task_type, lead))
			return orig(task_type, lead, values)

		location_api.location_required = spy
		try:
			task.status = "Done"
			with self.assertRaises(frappe.exceptions.ValidationError):
				task.save(ignore_permissions=True)
		finally:
			location_api.location_required = orig
		self.assertTrue(calls, "location.api.location_required was never consulted — the guard reimplemented the decision")
		self.assertEqual(calls[0], (self.task_type, self.lead.name))
		# The save never committed - status must still read Todo on the DB row.
		self.assertEqual(frappe.db.get_value("CRM Task", task.name, "status"), "Todo")


class TestRequireLocationGeofence(_GuardVerbBase):
	"""Coordinates present: within the action's geofence_meters passes; outside it raises."""

	GEOFENCE_M = 50

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.task_type = _make_task_type("GV-Geofence")
		cls.rule = _make_rule("GV-Geofence-rule", cls.task_type, geofence_meters=cls.GEOFENCE_M)

	@classmethod
	def tearDownClass(cls):
		frappe.db.delete("CRM Automation Action", {"parent": cls.rule.name})
		frappe.db.delete("CRM Automation Criterion", {"parent": cls.rule.name})
		frappe.db.delete(_DT, {"name": cls.rule.name})
		frappe.db.delete("CRM Task Type", {"name": cls.task_type})
		super().tearDownClass()

	def test_within_geofence_passes(self):
		# ~11m north of the anchor (well inside 50m).
		task = _make_task(
			self.lead.name, self.task_type,
			custom_location_latitude=_ANCHOR_LAT + 0.0001, custom_location_longitude=_ANCHOR_LNG,
		)
		task.status = "Done"
		task.save(ignore_permissions=True)  # must not raise
		self.assertEqual(frappe.db.get_value("CRM Task", task.name, "status"), "Done")

	def test_outside_geofence_raises(self):
		# ~111m north of the anchor (outside 50m).
		task = _make_task(
			self.lead.name, self.task_type,
			custom_location_latitude=_ANCHOR_LAT + 0.001, custom_location_longitude=_ANCHOR_LNG,
		)
		task.status = "Done"
		with self.assertRaises(frappe.exceptions.ValidationError):
			task.save(ignore_permissions=True)
		self.assertEqual(frappe.db.get_value("CRM Task", task.name, "status"), "Todo")


class TestUnregisteredVerbRejectedAtAuthorTime(FrappeTestCase):
	"""validate() must reject a rule whose action verb has no `_ACTION_LANES` handler — defense in
	depth so a Select option added without a handler fails LOUD at save, not silently at fire time.
	Every shipped v2 verb now HAS a handler, so we simulate the gap by temporarily de-registering one
	(patch.dict on the ONE registry) and prove the guard bites — a stable test that won't rot as the
	verb set changes (unlike the old 'Wait' planted-bad, which Task 9 made valid)."""

	def test_unregistered_verb_raises_on_rule_save(self):
		from unittest.mock import patch

		from tatva_connect.automation import actions

		lanes = dict(actions._ACTION_LANES)
		lanes.pop("Create Note")  # a valid Select option, temporarily without a handler
		with patch.object(actions, "_ACTION_LANES", lanes):
			with self.assertRaisesRegex(frappe.exceptions.ValidationError, "no registered handler"):
				frappe.get_doc({
					"doctype": _DT, "rule_name": "GV-unregistered-verb", "enabled": 1,
					"on_doctype": "CRM Task", "event": "Updated",
					"vertical": _GRAIN["vertical"], "group": _GRAIN["group"], "program": _GRAIN["program"],
					"criteria": [],
					"actions": [{"action_type": "Create Note", "comment_text": "x"}],
				}).insert(ignore_permissions=True)
		self.assertFalse(
			frappe.db.exists(_DT, {"rule_name": "GV-unregistered-verb"}),
			"a rule with an unregistered verb was persisted despite validate() raising",
		)


class TestBackstopStandsDownUnderACoveringRule(_GuardVerbBase):
	"""The severed `tasks.enforce_location` backstop must NOT re-throw when an authored Require
	Location rule already covers THIS task type (no double-throw) — proven by calling the backstop
	DIRECTLY (bypassing .save()) on a doc with no coordinates, which would otherwise fail-closed.
	A sibling task type with NO covering rule (recall guard) proves the standdown is conditional on
	the rule actually matching, not a blanket bypass once any rule exists in the grain."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.covered_type = _make_task_type("GV-Backstop-Covered")
		cls.uncovered_type = _make_task_type("GV-Backstop-Uncovered")
		cls.rule = _make_rule("GV-Backstop-rule", cls.covered_type)
		cls.covered_task = _make_task(cls.lead.name, cls.covered_type)
		cls.uncovered_task = _make_task(cls.lead.name, cls.uncovered_type)

	@classmethod
	def tearDownClass(cls):
		frappe.db.delete("CRM Automation Action", {"parent": cls.rule.name})
		frappe.db.delete("CRM Automation Criterion", {"parent": cls.rule.name})
		frappe.db.delete(_DT, {"name": cls.rule.name})
		frappe.db.delete("CRM Task Type", {"name": cls.covered_type})
		frappe.db.delete("CRM Task Type", {"name": cls.uncovered_type})
		super().tearDownClass()

	def test_backstop_stands_down_when_a_rule_covers_this_save(self):
		doc = frappe.get_doc("CRM Task", self.covered_task.name)
		doc.status = "Done"
		# No coordinates set — normally fail-closed; the covering rule must make this a no-op here.
		tasks.enforce_location(doc)  # must NOT raise

	def test_backstop_still_fires_when_no_rule_covers_this_task_type(self):
		"""Recall guard: no Require Location rule targets this sibling task type -> the backstop's own
		fail-closed check still runs and raises on missing coordinates."""
		doc = frappe.get_doc("CRM Task", self.uncovered_task.name)
		doc.status = "Done"
		self.assertFalse(tasks._location_guard_covers(doc))
		with self.assertRaises(frappe.exceptions.ValidationError):
			tasks.enforce_location(doc)


if __name__ == "__main__":
	unittest.main()
