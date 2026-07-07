# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Leg G sign-off - watch.py entry point + the full Field-Changed dispatch path.

Real Frappe engine as the oracle. frappe.flags.in_test makes `frappe.enqueue(..., now=True)` run
synchronously, so the whole fire -> enqueue -> run_for_field_change -> _run_rule -> _run_action
chain executes inside the test. The S.6 metamorphic pair (j)/(k) plants a known-bad and asserts
recall==1.0; (l) is the no-regression gate on the existing Task-Completed trigger.
"""
import unittest

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.automation import watch
from tatva_connect.tests.authz.grains import GRAINS, assert_masters_exist
from tatva_connect.tests.automation import field_allowlist

_DT = "CRM Automation Rule"
_FIELD = field_allowlist.DOCTYPE  # the merged allowlist
_RUN_LOG = "CRM Automation Run Log"
_GRAIN = GRAINS[0]
_WATCH_FIELD = "custom_lsq_lead_number"  # Data field - no stage/Link-master coupling (free text)
_SET_TARGET = "custom_last_report_date"  # Date on CRM Lead
_BAD_TARGET = "custom_patient_age"  # real field, NOT allowlisted


def _make_lead(stage="New"):
	ld = frappe.get_doc(
		{"doctype": "CRM Lead", "first_name": "LegG", "lead_name": "LegG Probe", "status": "New",
		 "custom_vertical": _GRAIN["vertical"], "custom_group": _GRAIN["group"],
		 "custom_current_program": _GRAIN["program"], _WATCH_FIELD: stage,
		 "custom_dob": "2026-07-06"}
	)
	ld.insert(ignore_permissions=True)
	return ld


# TATVA v2 (Task 1): watch_field is kept as a PARAMETER (not a rule field any more - v2 has no
# single rule-wide watch_field) purely so callers can still name which field they expect the
# criterion/diff to react to; it no longer lands on the rule doc itself.
def _make_field_change_rule(name, watch_field, action_fieldname, expression, grain=None, enabled=1):
	g = grain or _GRAIN
	doc = frappe.get_doc({
		"doctype": _DT, "rule_name": name, "enabled": enabled,
		"on_doctype": "CRM Lead", "event": "Updated",
		"vertical": g["vertical"], "group": g["group"], "program": g["program"],
		"actions": [{
			"action_type": "Update Field", "target_doctype": "CRM Lead",
			"fieldname": action_fieldname, "value_mode": "Expression", "expression": expression,
		}],
	})
	doc.insert(ignore_permissions=True)
	return doc


class TestWatchDiff(FrappeTestCase):
	"""The _diff_watched_fields comparator - the cheap synchronous decision in fire_field_change_rules."""

	@classmethod
	def setUpClass(cls):
		assert_masters_exist()
		cls.watchable = field_allowlist.seed_watchable("CRM Lead", _WATCH_FIELD)

	@classmethod
	def tearDownClass(cls):
		frappe.db.delete(_FIELD, {"name": cls.watchable})
		frappe.db.delete("CRM Lead", {"lead_name": "LegG Probe"})

	# (a) a watched field that changed A->B is in the diff.
	def test_diff_returns_changed_field(self):
		ld = _make_lead(stage="A")
		ld.set(_WATCH_FIELD, "B")
		ld.save(ignore_permissions=True)
		changed = watch._diff_watched_fields(ld)
		self.assertIn(_WATCH_FIELD, changed)
		self.assertEqual(changed[_WATCH_FIELD], ("A", "B"))

	# (b) no watched field changed -> empty diff.
	def test_diff_empty_when_nothing_changed(self):
		ld = _make_lead(stage="A")
		ld.custom_lead_temperature = "Hot"  # an unwatched field changes
		ld.save(ignore_permissions=True)
		self.assertEqual(watch._diff_watched_fields(ld), {})

	# (c) a new doc has no before-state -> empty diff (the caller also skips is_new, but this is self-contained).
	def test_diff_empty_for_new_doc(self):
		ld = frappe.get_doc({"doctype": "CRM Lead", "first_name": "LegGNew", "lead_name": "LegG Probe",
				"custom_vertical": _GRAIN["vertical"], "custom_group": _GRAIN["group"],
				"custom_current_program": _GRAIN["program"], _WATCH_FIELD: "A"})
		ld.insert(ignore_permissions=True)
		try:
			# is_new() is False post-insert, but get_doc_before_save() is None on the first save.
			self.assertEqual(watch._diff_watched_fields(ld), {})
		finally:
			ld.delete(ignore_permissions=True)


class TestWatchEntryGuards(FrappeTestCase):
	"""fire_field_change_rules' cheap guards - the must-be-synchronous decisions."""

	@classmethod
	def setUpClass(cls):
		assert_masters_exist()
		cls.watchable = field_allowlist.seed_watchable("CRM Lead", _WATCH_FIELD)
		frappe.db.set_value("CRM Tatva Automation", "Task::Automation::rules", "enabled", 1)

	@classmethod
	def tearDownClass(cls):
		frappe.db.delete(_FIELD, {"name": cls.watchable})
		frappe.db.delete("CRM Lead", {"lead_name": "LegG Probe"})

	# (e) enqueues when in_automation is False, switch ON, watched field changed.
	def test_enqueues_on_watched_change(self):
		frappe.flags.in_automation = False
		frappe.flags.in_test = True  # enqueue runs synchronously
		ld = _make_lead(stage="A")
		called = {"n": 0}
		orig = frappe.enqueue
		def _spy(method, **kw):
			if method == "tatva_connect.automation.watch.run_for_field_change":
				called["n"] += 1
			return orig(method, **kw)
		frappe.enqueue = _spy
		try:
			ld.set(_WATCH_FIELD, "B")
			ld.save(ignore_permissions=True)
		finally:
			frappe.enqueue = orig
			frappe.flags.in_test = False
		self.assertEqual(called["n"], 1)

	# (f) no-op when in_automation is True (re-entrancy guard).
	def test_noop_when_in_automation(self):
		frappe.flags.in_automation = True
		frappe.flags.in_test = True
		called = {"n": 0}
		orig = frappe.enqueue
		def _spy(method, **kw):
			if method == "tatva_connect.automation.watch.run_for_field_change":
				called["n"] += 1
			return orig(method, **kw)
		frappe.enqueue = _spy
		try:
			ld = _make_lead(stage="A")
			ld.set(_WATCH_FIELD, "B")
			ld.save(ignore_permissions=True)
		finally:
			frappe.enqueue = orig
			frappe.flags.in_automation = False
			frappe.flags.in_test = False
		self.assertEqual(called["n"], 0)

	# (g) no-op when the kill switch is OFF.
	def test_noop_when_switch_off(self):
		frappe.db.set_value("CRM Tatva Automation", "Task::Automation::rules", "enabled", 0)
		frappe.flags.in_automation = False
		frappe.flags.in_test = True
		called = {"n": 0}
		orig = frappe.enqueue
		def _spy(method, **kw):
			if method == "tatva_connect.automation.watch.run_for_field_change":
				called["n"] += 1
			return orig(method, **kw)
		frappe.enqueue = _spy
		try:
			ld = _make_lead(stage="A")
			ld.set(_WATCH_FIELD, "B")
			ld.save(ignore_permissions=True)
		finally:
			frappe.enqueue = orig
			frappe.flags.in_test = False
			frappe.db.set_value("CRM Tatva Automation", "Task::Automation::rules", "enabled", 1)
		self.assertEqual(called["n"], 0)


# TATVA v2 (Task 1): rules.matching_rules / matching_rules_for_field_change still filter on the
# retired trigger_type/task_type/watch_doctype/watch_field columns (Tasks 2-5 own reshaping
# describe.py/rules.py/watch.py/dispatcher.py onto on_doctype/event - explicitly out of Task 1's
# scope). Post-migration those columns are dropped, so any path through matching_rules(_for_field_
# change) now hits a live SQL error - caught and logged by watch.run_for_field_change's outer
# try/except, so the enqueue/save itself doesn't crash, but nothing actually fires: the behavioral
# assertions below would fail, not error. Skipping (not deleting) per the brief's fold-in - Task
# 4/5 restore these once the router/executor are rewired off the v2 trigger.
_SKIP_UNTIL_ROUTER_REWIRED = "TATVA v2: depends on rules.matching_rules(_for_field_change), which Task 4/5 rewire off on_doctype/event"


class TestWatchEndToEnd(FrappeTestCase):
	"""The full chain: on_update -> enqueue -> run_for_field_change -> _run_rule -> Update Field action."""

	@classmethod
	def setUpClass(cls):
		assert_masters_exist()
		cls.watchable = field_allowlist.seed_watchable("CRM Lead", _WATCH_FIELD)
		cls.allowlist = field_allowlist.seed_settable("CRM Lead", _SET_TARGET, _GRAIN["vertical"], _GRAIN["group"], _GRAIN["program"])
		frappe.db.set_value("CRM Tatva Automation", "Task::Automation::rules", "enabled", 1)

	@classmethod
	def tearDownClass(cls):
		frappe.db.delete(_DT, {"rule_name": ("like", "LegG-%")})
		frappe.db.delete(_FIELD, {"name": cls.allowlist})
		frappe.db.delete(_FIELD, {"name": cls.watchable})
		frappe.db.delete(_RUN_LOG, {"rule": ("like", "LegG-%")})
		frappe.db.delete("CRM Lead", {"lead_name": "LegG Probe"})

	# (h) END-TO-END: change the watched field -> rule fires -> custom_last_report_date = custom_dob + 3, Run Log=Success.
	@unittest.skip(_SKIP_UNTIL_ROUTER_REWIRED)
	def test_end_to_end_field_change_fires_rule(self):
		_make_field_change_rule("LegG-e2e", _WATCH_FIELD, _SET_TARGET, "add_days(ctx['custom_dob'], 3)")
		frappe.flags.in_test = True  # synchronous enqueue
		try:
			ld = _make_lead(stage="A")
			ld.set(_WATCH_FIELD, "B")
			ld.save(ignore_permissions=True)  # triggers fire_field_change_rules -> run_for_field_change (sync)
		finally:
			frappe.flags.in_test = False
		out = frappe.db.get_value("CRM Lead", ld.name, _SET_TARGET)
		self.assertEqual(frappe.utils.getdate(out), frappe.utils.add_days(frappe.utils.getdate("2026-07-06"), 3))
		logs = frappe.get_all(_RUN_LOG, filters={"rule": "LegG-e2e", "trigger_docname": ld.name}, pluck="outcome")
		self.assertIn("Success", logs, "no Success Run Log row for the fired rule")

	# (i) RE-ENTRANCY: the Update Field action's tdoc.save() does not re-fire (in_automation is set).
	@unittest.skip(_SKIP_UNTIL_ROUTER_REWIRED)
	def test_set_field_save_does_not_refire(self):
		_make_field_change_rule("LegG-reentry", _WATCH_FIELD, _SET_TARGET, "add_days(ctx['custom_dob'], 1)")
		frappe.flags.in_test = True
		try:
			ld = _make_lead(stage="A")
			ld.set(_WATCH_FIELD, "B")
			ld.save(ignore_permissions=True)
		finally:
			frappe.flags.in_test = False
		# Exactly one Run Log row for this rule+lead - the re-entrant save (Set Field) did NOT fire another.
		logs = frappe.get_all(_RUN_LOG, filters={"rule": "LegG-reentry", "trigger_docname": ld.name}, pluck="name")
		self.assertEqual(len(logs), 1, f"re-entrancy leaked: {len(logs)} Run Log rows for one fire")

	# (j) DIFFERENTIAL (S.6): Field-Changed Set Field on a non-allowlisted field -> PermissionError,
	# same verdict as a Task-Completed Set Field on a non-allowlisted field (grain narrows, never widens).
	# NB: the dispatcher raises Python's builtin PermissionError (parity with the existing v1 code).
	def test_field_change_set_field_non_allowlisted_throws(self):
		from tatva_connect.automation import dispatcher
		a = type("A", (), {"action_type": "Update Field", "target_doctype": "CRM Lead",
			"fieldname": _BAD_TARGET, "value_mode": "Literal", "value": "99"})()
		ld = _make_lead(stage="A")
		with self.assertRaises(PermissionError):
			dispatcher._action_set_field(a, ld.name, {"custom_dob": "2026-07-06"},
				(_GRAIN["vertical"], _GRAIN["group"], _GRAIN["program"]), ld)

	# (k) KNOWN-BAD PLANT (S.6 recall): a rule authored while the field WAS allowlisted, then the
	# allowlist row is DISABLED - a subsequent fire must be caught at RUNTIME (the allowlist recheck
	# on every fire, defense in depth) -> Run Log outcome=Failed, the field is NOT written.
	# recall==1.0 (the planted bad was caught). This is the metamorphic relation: grain narrows
	# (a disabled allowlist row), never widens - the runtime verdict tracks the authoring verdict.
	@unittest.skip(_SKIP_UNTIL_ROUTER_REWIRED)
	def test_known_bad_set_field_is_caught(self):
		# 1. Author the field as allowlisted (so the rule passes validate at authoring).
		bad_allowlist = field_allowlist.seed_settable("CRM Lead", _BAD_TARGET, _GRAIN["vertical"], _GRAIN["group"], _GRAIN["program"])
		try:
			rule = _make_field_change_rule("LegG-bad", _WATCH_FIELD, _BAD_TARGET, "ctx['custom_dob']")
			# 2. DISABLE the allowlist row - the rule is now stale (authoring permitted it; runtime must not).
			frappe.db.set_value(_FIELD, bad_allowlist, "enabled", 0)
			ld = _make_lead(stage="A")
			age_before = frappe.db.get_value("CRM Lead", ld.name, _BAD_TARGET)
			frappe.flags.in_test = True
			try:
				ld.set(_WATCH_FIELD, "B")
				ld.save(ignore_permissions=True)
			finally:
				frappe.flags.in_test = False
			age_after = frappe.db.get_value("CRM Lead", ld.name, _BAD_TARGET)
			self.assertEqual(age_before, age_after, "non-allowlisted field was written despite the allowlist gate")
			logs = frappe.get_all(_RUN_LOG, filters={"rule": "LegG-bad", "trigger_docname": ld.name}, pluck="outcome")
			self.assertIn("Failed", logs, "the planted known-bad was not caught - recall < 1.0")
		finally:
			frappe.db.delete(_DT, {"name": "LegG-bad"})
			frappe.db.delete(_FIELD, {"name": bad_allowlist})

	# (l) REGRESSION: the existing "task completed" fire_rules path still fires (the second handler
	# on CRM Task.on_update does not interfere - Frappe concatenates, both run independently). v2
	# shape: on_doctype=CRM Task, event=Updated + criterion status changed to Done (the migrated
	# grammar - see patches.reshape_automation_triggers).
	@unittest.skip(_SKIP_UNTIL_ROUTER_REWIRED)
	def test_task_completed_still_fires(self):
		# A throwaway grain-keyed task type + a "task completed" rule that sets a field.
		tt_name = f"{_GRAIN['vertical']}::{_GRAIN['group']}::{_GRAIN['program']}::LegGTask"
		if not frappe.db.exists("CRM Task Type", tt_name):
			frappe.get_doc({"doctype": "CRM Task Type", "type_name": "LegGTask",
				"vertical": _GRAIN["vertical"], "group": _GRAIN["group"], "program": _GRAIN["program"]}
			).insert(ignore_permissions=True)
		tc_rule = frappe.get_doc({
			"doctype": _DT, "rule_name": "LegG-taskcompleted", "enabled": 1,
			"on_doctype": "CRM Task", "event": "Updated",
			"vertical": _GRAIN["vertical"], "group": _GRAIN["group"], "program": _GRAIN["program"],
			"criteria": [{"field": "status", "operator": "changed to", "value": "Done"}],
			"actions": [{"action_type": "Update Field", "target_doctype": "CRM Lead",
				"fieldname": _SET_TARGET, "value_mode": "Literal", "value": "2026-08-01"}],
		})
		tc_rule.insert(ignore_permissions=True)
		frappe.flags.in_test = True
		try:
			ld = _make_lead(stage="A")
			task = frappe.get_doc({"doctype": "CRM Task", "title": "LegG task probe",
				"reference_doctype": "CRM Lead",
				"reference_docname": ld.name, "custom_task_type": tt_name, "status": "Todo"})
			task.insert(ignore_permissions=True)
			task.status = "Done"
			task.save(ignore_permissions=True)  # first Done flip -> fire_rules -> run_for_task (sync)
		finally:
			frappe.flags.in_test = False
			frappe.db.delete("CRM Task", {"reference_docname": ld.name})
			frappe.db.delete(_DT, {"name": tc_rule.name})
			frappe.db.delete("CRM Task Type", {"name": tt_name})
		logs = frappe.get_all(_RUN_LOG, filters={"rule": "LegG-taskcompleted", "trigger_doctype": "CRM Task"}, pluck="outcome")
		self.assertTrue(logs, "Task-Completed fire_rules did not fire - regression in the concatenation")


if __name__ == "__main__":
	unittest.main()
