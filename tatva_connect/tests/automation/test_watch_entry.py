# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Leg G sign-off - the Field-Changed half of the unified wildcard router + the full end-to-end
dispatch path (Update Field, re-entrancy, the known-bad allowlist plant, and the Task-Completed
regression - now expressed as an Updated rule with a `status changed to Done` criterion, per the v2
vocabulary).

TATVA v2 (Task 4): this suite exercised `watch.py` (fire_field_change_rules / run_for_field_change /
_diff_watched_fields / _subject / _context_for), which is RETIRED into `automation/router.py` - the
same functions, the same logic, moved (not duplicated, A.8). The suite is rewritten onto
`router.on_updated` / `router.run_for_event` / `router._diff_watched_fields`.

Real Frappe engine as the oracle. frappe.flags.in_test makes `frappe.enqueue(..., now=True)` run
synchronously, so the whole fire -> enqueue -> run_for_event -> _run_rule -> _run_action chain
executes inside the test. The S.6 metamorphic pair (j)/(k) plants a known-bad and asserts
recall==1.0; (l) is the no-regression gate on the retired Task-Completed trigger's replacement shape.
"""
import unittest

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.automation import router
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


def _make_live_rule(name, grain=None, enabled=1):
	"""The minimal rule that makes CRM Lead a LIVE doctype (router.live_doctypes()) - a Create Note
	action, so the entry-guard tests don't need an Automation-Field allowlist row (they only exercise
	the enqueue guard ladder, not a write)."""
	g = grain or _GRAIN
	return frappe.get_doc({
		"doctype": _DT, "rule_name": name, "enabled": enabled,
		"on_doctype": "CRM Lead", "event": "Updated",
		"vertical": g["vertical"], "group": g["group"], "program": g["program"],
		"actions": [{"action_type": "Create Note", "comment_mode": "Literal", "comment_text": "guard probe"}],
	}).insert(ignore_permissions=True)


def _make_field_change_rule(name, watch_field, action_fieldname, expression, grain=None, enabled=1):
	"""v2: on_doctype/event, not a rule-wide watch_field - watch_field stays a PARAMETER purely so
	callers can name which field they expect the criterion/diff to react to."""
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
	"""router._diff_watched_fields - the cheap synchronous decision in router.on_updated."""

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
		changed = router._diff_watched_fields(ld)
		self.assertIn(_WATCH_FIELD, changed)
		self.assertEqual(changed[_WATCH_FIELD], ("A", "B"))

	# (b) no watched field changed -> empty diff.
	def test_diff_empty_when_nothing_changed(self):
		ld = _make_lead(stage="A")
		ld.custom_lead_temperature = "Hot"  # an unwatched field changes
		ld.save(ignore_permissions=True)
		self.assertEqual(router._diff_watched_fields(ld), {})

	# (c) a new doc has no before-state -> empty diff (the caller also skips is_new, but this is self-contained).
	def test_diff_empty_for_new_doc(self):
		ld = frappe.get_doc({"doctype": "CRM Lead", "first_name": "LegGNew", "lead_name": "LegG Probe",
				"custom_vertical": _GRAIN["vertical"], "custom_group": _GRAIN["group"],
				"custom_current_program": _GRAIN["program"], _WATCH_FIELD: "A"})
		ld.insert(ignore_permissions=True)
		try:
			# is_new() is False post-insert, but get_doc_before_save() is None on the first save.
			self.assertEqual(router._diff_watched_fields(ld), {})
		finally:
			ld.delete(ignore_permissions=True)


class TestWatchEntryGuards(FrappeTestCase):
	"""router.on_updated's cheap guards - the must-be-synchronous decisions."""

	@classmethod
	def setUpClass(cls):
		assert_masters_exist()
		cls.watchable = field_allowlist.seed_watchable("CRM Lead", _WATCH_FIELD)
		frappe.db.set_value("CRM Tatva Automation", "Task::Automation::rules", "enabled", 1)

	@classmethod
	def tearDownClass(cls):
		frappe.db.delete(_FIELD, {"name": cls.watchable})
		frappe.db.delete("CRM Lead", {"lead_name": "LegG Probe"})

	def tearDown(self):
		router.clear_live_doctypes_cache()

	# (e) enqueues when in_automation is False, switch ON, doctype live, watched field changed.
	def test_enqueues_on_watched_change(self):
		rule = _make_live_rule("LegG-guard-live")
		frappe.flags.in_automation = False
		frappe.flags.in_test = True  # enqueue runs synchronously
		ld = _make_lead(stage="A")  # insert (Created) happens BEFORE the spy - only the Updated fire is counted
		called = {"n": 0}
		orig = frappe.enqueue
		def _spy(method, **kw):
			if method == "tatva_connect.automation.router.run_for_event":
				called["n"] += 1
			return orig(method, **kw)
		frappe.enqueue = _spy
		try:
			ld.set(_WATCH_FIELD, "B")
			ld.save(ignore_permissions=True)
		finally:
			frappe.enqueue = orig
			frappe.flags.in_test = False
			frappe.delete_doc("CRM Automation Rule", rule.name, force=True, ignore_permissions=True)
		self.assertEqual(called["n"], 1)

	# (f) no-op when in_automation is True (re-entrancy guard).
	def test_noop_when_in_automation(self):
		rule = _make_live_rule("LegG-guard-reentry")
		frappe.flags.in_automation = True
		frappe.flags.in_test = True
		called = {"n": 0}
		orig = frappe.enqueue
		def _spy(method, **kw):
			if method == "tatva_connect.automation.router.run_for_event":
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
			frappe.delete_doc("CRM Automation Rule", rule.name, force=True, ignore_permissions=True)
		self.assertEqual(called["n"], 0)

	# (g) no-op when the kill switch is OFF.
	def test_noop_when_switch_off(self):
		rule = _make_live_rule("LegG-guard-off")
		frappe.db.set_value("CRM Tatva Automation", "Task::Automation::rules", "enabled", 0)
		frappe.flags.in_automation = False
		frappe.flags.in_test = True
		called = {"n": 0}
		orig = frappe.enqueue
		def _spy(method, **kw):
			if method == "tatva_connect.automation.router.run_for_event":
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
			frappe.delete_doc("CRM Automation Rule", rule.name, force=True, ignore_permissions=True)
		self.assertEqual(called["n"], 0)


class TestWatchEndToEnd(FrappeTestCase):
	"""The full chain: on_update -> enqueue -> run_for_event -> _run_rule -> Update Field action."""

	@classmethod
	def setUpClass(cls):
		assert_masters_exist()
		cls.watchable = field_allowlist.seed_watchable("CRM Lead", _WATCH_FIELD)
		cls.allowlist = field_allowlist.seed_settable("CRM Lead", _SET_TARGET, _GRAIN["vertical"], _GRAIN["group"], _GRAIN["program"])
		frappe.db.set_value("CRM Tatva Automation", "Task::Automation::rules", "enabled", 1)

	@classmethod
	def tearDownClass(cls):
		# Child tables first (frappe.db.delete on the parent doesn't cascade) - else an orphaned
		# action/criterion row with a stale `parent` name pollutes the NEXT run of the same rule_name.
		frappe.db.delete("CRM Automation Action", {"parent": ("like", "LegG-%")})
		frappe.db.delete("CRM Automation Criterion", {"parent": ("like", "LegG-%")})
		frappe.db.delete(_DT, {"rule_name": ("like", "LegG-%")})
		frappe.db.delete(_FIELD, {"name": cls.allowlist})
		frappe.db.delete(_FIELD, {"name": cls.watchable})
		frappe.db.delete(_RUN_LOG, {"rule": ("like", "LegG-%")})
		frappe.db.delete("CRM Lead", {"lead_name": "LegG Probe"})

	def tearDown(self):
		router.clear_live_doctypes_cache()

	# (h) END-TO-END: change the watched field -> rule fires -> custom_last_report_date = custom_dob + 3, Run Log=Success.
	def test_end_to_end_field_change_fires_rule(self):
		_make_field_change_rule("LegG-e2e", _WATCH_FIELD, _SET_TARGET, "add_days(ctx['custom_dob'], 3)")
		frappe.flags.in_test = True  # synchronous enqueue
		try:
			ld = _make_lead(stage="A")
			ld.set(_WATCH_FIELD, "B")
			ld.save(ignore_permissions=True)  # triggers router.on_updated -> run_for_event (sync)
		finally:
			frappe.flags.in_test = False
		out = frappe.db.get_value("CRM Lead", ld.name, _SET_TARGET)
		self.assertEqual(frappe.utils.getdate(out), frappe.utils.add_days(frappe.utils.getdate("2026-07-06"), 3))
		logs = frappe.get_all(_RUN_LOG, filters={"rule": "LegG-e2e", "trigger_docname": ld.name}, pluck="outcome")
		self.assertIn("Success", logs, "no Success Run Log row for the fired rule")

	# (i) RE-ENTRANCY: the Update Field action's tdoc.save() does not re-fire (in_automation is set).
	def test_set_field_save_does_not_refire(self):
		_make_field_change_rule("LegG-reentry", _WATCH_FIELD, _SET_TARGET, "add_days(ctx['custom_dob'], 1)")
		frappe.flags.in_test = True
		try:
			ld = _make_lead(stage="A")
			ld.set(_WATCH_FIELD, "B")
			ld.save(ignore_permissions=True)
		finally:
			frappe.flags.in_test = False
		# Exactly one Run Log row for this rule+lead - the re-entrant save (Update Field) did NOT fire another.
		logs = frappe.get_all(_RUN_LOG, filters={"rule": "LegG-reentry", "trigger_docname": ld.name}, pluck="name")
		self.assertEqual(len(logs), 1, f"re-entrancy leaked: {len(logs)} Run Log rows for one fire")

	# (j) DIFFERENTIAL (S.6): a Set Field action on a non-allowlisted field -> PermissionError, same
	# verdict regardless of trigger shape (grain narrows, never widens).
	# NB: the dispatcher raises Python's builtin PermissionError (parity with the existing v1 code).
	def test_field_change_set_field_non_allowlisted_throws(self):
		from tatva_connect.automation import actions
		a = type("A", (), {"action_type": "Update Field", "target_doctype": "CRM Lead",
			"fieldname": _BAD_TARGET, "value_mode": "Literal", "value": "99"})()
		ld = _make_lead(stage="A")
		with self.assertRaises(PermissionError):
			actions._action_set_field(a, ld.name, {"custom_dob": "2026-07-06"},
				(_GRAIN["vertical"], _GRAIN["group"], _GRAIN["program"]), ld)

	# (k) KNOWN-BAD PLANT (S.6 recall): a rule authored while the field WAS allowlisted, then the
	# allowlist row is DISABLED - a subsequent fire must be caught at RUNTIME (the allowlist recheck
	# on every fire, defense in depth) -> Run Log outcome=Failed, the field is NOT written.
	# recall==1.0 (the planted bad was caught). This is the metamorphic relation: grain narrows
	# (a disabled allowlist row), never widens - the runtime verdict tracks the authoring verdict.
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

	# (l) REGRESSION: the retired "task completed" fire_rules path's replacement - v2 shape:
	# on_doctype=CRM Task, event=Updated + criterion `status changed to Done` (the migrated grammar -
	# see patches.reshape_automation_triggers). Proves the wildcard router covers CRM Task exactly as
	# it covers CRM Lead - no per-doctype hook needed (Frappe's *-registration, one router).
	def test_task_completed_still_fires(self):
		# A throwaway grain-keyed task type + a "task completed" rule that sets a field. `status` must
		# be watchable on CRM Task (router.on_updated only diffs watchable fields) - the go-live
		# allowlist/seed makes this true in production; a test builds it itself (out of Task 4's scope
		# per the brief, but required here to exercise the flow end-to-end).
		tt_name = f"{_GRAIN['vertical']}::{_GRAIN['group']}::{_GRAIN['program']}::LegGTask"
		if not frappe.db.exists("CRM Task Type", tt_name):
			frappe.get_doc({"doctype": "CRM Task Type", "type_name": "LegGTask",
				"vertical": _GRAIN["vertical"], "group": _GRAIN["group"], "program": _GRAIN["program"]}
			).insert(ignore_permissions=True)
		status_watchable = field_allowlist.seed_watchable("CRM Task", "status")
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
			task.save(ignore_permissions=True)  # first Done flip -> router.on_updated -> run_for_event (sync)
		finally:
			frappe.flags.in_test = False
			frappe.db.delete("CRM Task", {"reference_docname": ld.name})
			frappe.db.delete("CRM Automation Action", {"parent": tc_rule.name})
			frappe.db.delete("CRM Automation Criterion", {"parent": tc_rule.name})
			frappe.db.delete(_DT, {"name": tc_rule.name})
			frappe.db.delete("CRM Task Type", {"name": tt_name})
			frappe.db.delete(_FIELD, {"name": status_watchable})
			router.clear_live_doctypes_cache()
		logs = frappe.get_all(_RUN_LOG, filters={"rule": "LegG-taskcompleted", "trigger_doctype": "CRM Task"}, pluck="outcome")
		self.assertTrue(logs, "the retired Task-Completed path's v2 replacement did not fire - regression")


if __name__ == "__main__":
	unittest.main()
