# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Task 9 — the Wait effect verb + the resume queue. A `Wait` action is a SEGMENT BOUNDARY: it parks
the rule's remaining effect actions as one `CRM Automation Resume` row and `resume.sweep_resume()`
resumes them through the SAME `dispatcher.run_effects` executor (A.8) once `resume_at` arrives — no
forked executor, no second expression path. Atomicity is per-SEGMENT, never across a Wait (see
`dispatcher.run_effects`'s docstring) — these tests prove that honestly: the pre-wait segment commits
and stays even when a later segment later fails/re-parks.

Real Frappe engine as the oracle — real saves, real Resume/Run Log rows, a real back-dated `resume_at`
set ONLY inside this test's own transaction (the HARD SAFETY CONSTRAINT: never a persisted global
change, never a raw DB write outside a FrappeTestCase). S.6: the planted-bad cases assert a genuine
`frappe.throw`, never a hardcoded verdict.
"""
import unittest

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.automation import actions, dispatcher, resume
from tatva_connect.tests.authz.grains import GRAINS, assert_masters_exist
from tatva_connect.tests.automation import field_allowlist

_RULE_DT = "CRM Automation Rule"
_RUN_LOG = "CRM Automation Run Log"
_RESUME_DT = "CRM Automation Resume"
_FIELD = field_allowlist.DOCTYPE
_GRAIN = GRAINS[0]
_AXES = (_GRAIN["vertical"], _GRAIN["group"], _GRAIN["program"])


def _action_row(**fields):
	"""A duck-typed action row - the handlers read attributes off it (same shape as a real
	`CRM Automation Action` child row). Mirrors test_effect_verbs._action_row."""
	class _A:
		pass
	a = _A()
	for k, v in fields.items():
		setattr(a, k, v)
	return a


def _make_lead(**extra):
	payload = {
		"doctype": "CRM Lead", "first_name": "WaitResume", "lead_name": "WaitResume Probe", "status": "New",
		"custom_vertical": _GRAIN["vertical"], "custom_group": _GRAIN["group"],
		"custom_current_program": _GRAIN["program"],
	}
	payload.update(extra)
	return frappe.get_doc(payload).insert(ignore_permissions=True)


def _make_rule(name, action_rows, grain=None, on_doctype="CRM Lead", event="Updated"):
	g = grain or _GRAIN
	return frappe.get_doc({
		"doctype": _RULE_DT, "rule_name": name, "enabled": 1,
		"on_doctype": on_doctype, "event": event,
		"vertical": g["vertical"], "group": g["group"], "program": g["program"],
		"criteria": [],
		"actions": action_rows,
	}).insert(ignore_permissions=True)


def _cleanup(prefix):
	frappe.db.delete("CRM Automation Action", {"parent": ("like", f"{prefix}%")})
	frappe.db.delete("CRM Automation Criterion", {"parent": ("like", f"{prefix}%")})
	frappe.db.delete(_RULE_DT, {"rule_name": ("like", f"{prefix}%")})
	frappe.db.delete(_RUN_LOG, {"rule": ("like", f"{prefix}%")})
	frappe.db.delete(_RESUME_DT, {"rule": ("like", f"{prefix}%")})


def _back_date(resume_name, minutes_ago=1):
	"""Back-date a parked row's resume_at INSIDE this test's own transaction only — never a real,
	persisted global DB write (the hard safety constraint). FrappeTestCase wraps every test in a
	savepoint-rolled-back transaction, so this never survives past the test."""
	frappe.db.set_value(_RESUME_DT, resume_name, "resume_at", frappe.utils.add_to_date(frappe.utils.now_datetime(), minutes=-minutes_ago))


class TestWaitParksAndResumes(FrappeTestCase):
	"""(a)/(b)/(c) — `[Create Task, Wait(days=14), Update Field]`: firing runs Create Task NOW and
	parks the remainder (Update Field has NOT run); a back-dated resume_at + sweep_resume() runs
	Update Field and marks the row Done; a second sweep is a no-op (idempotency)."""

	@classmethod
	def setUpClass(cls):
		assert_masters_exist()
		cls.set_row = field_allowlist.seed_settable(
			"CRM Lead", "custom_last_report_date", _GRAIN["vertical"], _GRAIN["group"], _GRAIN["program"]
		)
		cls.tt_name = f"{_GRAIN['vertical']}::{_GRAIN['group']}::{_GRAIN['program']}::WRFollowUp"
		if not frappe.db.exists("CRM Task Type", cls.tt_name):
			frappe.get_doc({
				"doctype": "CRM Task Type", "type_name": "WRFollowUp",
				"vertical": _GRAIN["vertical"], "group": _GRAIN["group"], "program": _GRAIN["program"],
			}).insert(ignore_permissions=True)
		frappe.db.set_value("CRM Tatva Automation", "Task::Automation::rules", "enabled", 1)

	@classmethod
	def tearDownClass(cls):
		_cleanup("WR-basic")
		frappe.db.delete("CRM Task Type", {"name": cls.tt_name})
		frappe.db.delete(_FIELD, {"name": cls.set_row})

	def setUp(self):
		self.lead = _make_lead()

	def tearDown(self):
		frappe.db.delete("CRM Task", {"reference_doctype": "CRM Lead", "reference_docname": self.lead.name})
		frappe.db.delete("CRM Lead", {"name": self.lead.name})

	def test_wait_parks_remainder_runs_task_now_then_resumes_and_is_idempotent(self):
		rule = _make_rule("WR-basic", [
			{"action_type": "Create Task", "task_type": self.tt_name, "due_mode": "From Context", "due_from": None},
			{"action_type": "Wait", "wait_expression": "{'days': 14}"},
			{"action_type": "Update Field", "target_doctype": "CRM Lead", "fieldname": "custom_last_report_date",
			 "value_mode": "Literal", "value": "2027-01-01"},
		])
		dispatcher.run_effects(self.lead.name, frappe._dict(name=rule.name), self.lead, _AXES, "grain", {}, {})

		# (a) Create Task ran now.
		tasks = frappe.get_all("CRM Task", filters={
			"reference_doctype": "CRM Lead", "reference_docname": self.lead.name, "custom_task_type": self.tt_name,
		})
		self.assertTrue(tasks, "Create Task (before the Wait) did not run")

		# Update Field (after the Wait) has NOT run.
		field_val = frappe.db.get_value("CRM Lead", self.lead.name, "custom_last_report_date")
		self.assertNotEqual(
			frappe.utils.getdate(field_val) if field_val else None, frappe.utils.getdate("2027-01-01"),
			"Update Field (after the Wait) ran before the wait elapsed",
		)

		# Exactly one Resume row, parked at the Update Field (0-based index 2), ~14 days out.
		rows = frappe.get_all(
			_RESUME_DT, filters={"rule": rule.name},
			fields=["name", "status", "next_action_idx", "resume_at"],
		)
		self.assertEqual(len(rows), 1, "exactly one Resume row must be parked")
		self.assertEqual(rows[0].status, "Pending")
		self.assertEqual(rows[0].next_action_idx, 2, "next_action_idx must point at Update Field")
		expected = frappe.utils.add_to_date(frappe.utils.now_datetime(), days=14)
		drift_seconds = abs((frappe.utils.get_datetime(rows[0].resume_at) - expected).total_seconds())
		self.assertLess(drift_seconds, 120, "resume_at is not ~14 days out")

		# (b) Back-date resume_at INSIDE this test's transaction, then sweep — Update Field runs.
		_back_date(rows[0].name)
		resume.sweep_resume()

		field_val = frappe.db.get_value("CRM Lead", self.lead.name, "custom_last_report_date")
		self.assertEqual(frappe.utils.getdate(field_val), frappe.utils.getdate("2027-01-01"), "Update Field did not run on resume")
		self.assertEqual(frappe.db.get_value(_RESUME_DT, rows[0].name, "status"), "Done")

		# (c) Idempotency: a second sweep over the same window must not re-run Update Field.
		frappe.db.set_value("CRM Lead", self.lead.name, "custom_last_report_date", "2020-01-01")
		resume.sweep_resume()
		field_val = frappe.db.get_value("CRM Lead", self.lead.name, "custom_last_report_date")
		self.assertEqual(
			frappe.utils.getdate(field_val), frappe.utils.getdate("2020-01-01"),
			"a second sweep re-ran an already-Done resume row",
		)


class TestChainedWaitReParks(FrappeTestCase):
	"""(d) chained — `[Update Field A, Wait, Update Field B, Wait, Create Note]`: firing runs A and
	parks before B; resuming past the first Wait runs B and parks AGAIN before the Create Note,
	proving a RESUMED run can itself re-park through the very same executor."""

	@classmethod
	def setUpClass(cls):
		assert_masters_exist()
		cls.field_a = field_allowlist.seed_settable(
			"CRM Lead", "custom_last_report_date", _GRAIN["vertical"], _GRAIN["group"], _GRAIN["program"]
		)
		cls.field_b = field_allowlist.seed_settable(
			"CRM Lead", "custom_patient_age", _GRAIN["vertical"], _GRAIN["group"], _GRAIN["program"]
		)
		frappe.db.set_value("CRM Tatva Automation", "Task::Automation::rules", "enabled", 1)

	@classmethod
	def tearDownClass(cls):
		_cleanup("WR-chain")
		frappe.db.delete(_FIELD, {"name": cls.field_a})
		frappe.db.delete(_FIELD, {"name": cls.field_b})

	def setUp(self):
		self.lead = _make_lead()

	def tearDown(self):
		frappe.db.delete("Comment", {"reference_doctype": "CRM Lead", "reference_name": self.lead.name})
		frappe.db.delete("CRM Lead", {"name": self.lead.name})

	def test_chained_waits_re_park_on_resume(self):
		rule = _make_rule("WR-chain", [
			{"action_type": "Update Field", "target_doctype": "CRM Lead", "fieldname": "custom_last_report_date",
			 "value_mode": "Literal", "value": "2027-02-01"},
			{"action_type": "Wait", "wait_expression": "{'days': 7}"},
			{"action_type": "Update Field", "target_doctype": "CRM Lead", "fieldname": "custom_patient_age",
			 "value_mode": "Literal", "value": "42"},
			{"action_type": "Wait", "wait_expression": "{'days': 7}"},
			{"action_type": "Create Note", "comment_mode": "Literal", "comment_text": "WR-chain: final step"},
		])
		dispatcher.run_effects(self.lead.name, frappe._dict(name=rule.name), self.lead, _AXES, "grain", {}, {})

		self.assertEqual(
			frappe.utils.getdate(frappe.db.get_value("CRM Lead", self.lead.name, "custom_last_report_date")),
			frappe.utils.getdate("2027-02-01"),
			"the first segment (before the first Wait) did not run",
		)
		rows = frappe.get_all(_RESUME_DT, filters={"rule": rule.name}, fields=["name", "next_action_idx"], order_by="creation asc")
		self.assertEqual(len(rows), 1, "exactly one Resume row after the first fire")
		self.assertEqual(rows[0].next_action_idx, 2)

		_back_date(rows[0].name)
		resume.sweep_resume()

		self.assertEqual(
			frappe.db.get_value("CRM Lead", self.lead.name, "custom_patient_age"), 42,
			"the second segment (between the two Waits) did not run on the first resume",
		)
		rows2 = frappe.get_all(
			_RESUME_DT, filters={"rule": rule.name}, fields=["name", "status", "next_action_idx"], order_by="creation asc",
		)
		self.assertEqual(len(rows2), 2, "resuming past the second Wait must produce a NEW parked row (chained re-park)")
		self.assertEqual(rows2[0].status, "Done", "the first resumed row must be marked Done, not re-run")
		pending = [r for r in rows2 if r.status == "Pending"]
		self.assertEqual(len(pending), 1)
		self.assertEqual(pending[0].next_action_idx, 4)

		comments_before = frappe.get_all(
			"Comment", filters={"reference_doctype": "CRM Lead", "reference_name": self.lead.name}, pluck="content",
		)
		self.assertFalse(
			any("WR-chain: final step" in (c or "") for c in comments_before),
			"the third segment ran before its own Wait resumed",
		)

		_back_date(pending[0].name)
		resume.sweep_resume()

		comments_after = frappe.get_all(
			"Comment", filters={"reference_doctype": "CRM Lead", "reference_name": self.lead.name}, pluck="content",
		)
		self.assertTrue(
			any("WR-chain: final step" in (c or "") for c in comments_after),
			"the third segment did not run on the second resume",
		)


class TestWaitPlantedBadExpression(FrappeTestCase):
	"""(e) recall guard — a Wait whose expression does not evaluate to a non-empty add_to_date-kwargs
	dict must RAISE (frappe.throw), never silently resolve to a zero-length/no-op wait. Exercises the
	verb handler directly (unit-level) with three distinct bad shapes."""

	@classmethod
	def setUpClass(cls):
		assert_masters_exist()

	def setUp(self):
		self.lead = _make_lead()

	def tearDown(self):
		frappe.db.delete("CRM Lead", {"name": self.lead.name})

	def test_non_dict_expression_raises(self):
		a = _action_row(action_type="Wait", wait_expression="'not a dict'")
		with self.assertRaises(frappe.exceptions.ValidationError):
			actions._action_wait(a, self.lead.name, {}, _AXES, self.lead)

	def test_empty_dict_expression_raises(self):
		a = _action_row(action_type="Wait", wait_expression="{}")
		with self.assertRaises(frappe.exceptions.ValidationError):
			actions._action_wait(a, self.lead.name, {}, _AXES, self.lead)

	def test_unusable_add_to_date_kwargs_raises(self):
		a = _action_row(action_type="Wait", wait_expression="{'not_a_real_kwarg': 1}")
		with self.assertRaises(frappe.exceptions.ValidationError):
			actions._action_wait(a, self.lead.name, {}, _AXES, self.lead)


class TestWaitPlantedBadRollsBackSegment(FrappeTestCase):
	"""Planted-bad through the REAL executor: `[Update Field, Wait(bad expression)]` must roll the
	Update Field back too and park NOTHING — a bad Wait expression is a REGULAR action failure to
	`run_effects` (it never reaches `_ParkSignal`), so the existing per-segment atomicity applies
	unchanged (proves `_ParkSignal`'s except clause does not swallow a genuine failure)."""

	@classmethod
	def setUpClass(cls):
		assert_masters_exist()
		cls.set_row = field_allowlist.seed_settable(
			"CRM Lead", "custom_last_report_date", _GRAIN["vertical"], _GRAIN["group"], _GRAIN["program"]
		)

	@classmethod
	def tearDownClass(cls):
		_cleanup("WR-badexpr")
		frappe.db.delete(_FIELD, {"name": cls.set_row})

	def setUp(self):
		self.lead = _make_lead()

	def tearDown(self):
		frappe.db.delete("CRM Lead", {"name": self.lead.name})

	def test_bad_wait_expression_rolls_back_segment_and_parks_nothing(self):
		baseline = frappe.db.get_value("CRM Lead", self.lead.name, "custom_last_report_date")
		rule = _make_rule("WR-badexpr", [
			{"action_type": "Update Field", "target_doctype": "CRM Lead", "fieldname": "custom_last_report_date",
			 "value_mode": "Literal", "value": "2027-03-01"},
			{"action_type": "Wait", "wait_expression": "'not a dict'"},
		])
		dispatcher.run_effects(self.lead.name, frappe._dict(name=rule.name), self.lead, _AXES, "grain", {}, {})

		after = frappe.db.get_value("CRM Lead", self.lead.name, "custom_last_report_date")
		self.assertEqual(after, baseline, "Update Field persisted despite the Wait's bad expression failing the segment")
		self.assertFalse(frappe.get_all(_RESUME_DT, filters={"rule": rule.name}), "a failed Wait must not park anything")
		logs = frappe.get_all(_RUN_LOG, filters={"rule": rule.name}, fields=["outcome", "actions_failed"])
		self.assertTrue(logs, "no Run Log row written for the failed fire")
		self.assertIn(logs[0].outcome, ("Partial", "Failed"))
		self.assertGreater(logs[0].actions_failed, 0)


if __name__ == "__main__":
	unittest.main()
