# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The Wait effect verb + the resume queue, against IMMUTABLE rule versions.

A `Wait` is a SEGMENT BOUNDARY: it parks the remaining actions as one `CRM Automation Resume` row bound
to a frozen `CRM Automation Rule Version`, and `resume.sweep_resume()` resumes them through the SAME
`dispatcher.run_effects` executor (A.8) once `resume_at` arrives. Atomicity is per-SEGMENT, never across
a Wait — proven honestly below: the pre-wait segment commits and stays even when a later segment fails.

The versioning contract is what the second half of this file proves, and it is the whole reason the
queue exists in this shape: a parked execution must survive its rule being edited underneath it.

  * SUFFIX edit (a step the lead has not reached)  -> the lead ADOPTS the new version.
  * PREFIX edit (a step the lead already ran)      -> the lead is RETAINED on its own version, and no
                                                      action it already performed is ever performed twice.
  * The parking Wait's delay edited               -> adopted, and `resume_at` is RE-DERIVED from
                                                      `parked_at`, not restarted from now.
  * Rule disabled                                 -> HOLD (still Pending). Disable is pause, not destroy.
  * Rule deleted (even `force=1`)                 -> Cancelled via `on_trash`.

Real Frappe engine as the oracle — real saves, real Resume/Run Log rows, a real back-dated `resume_at`
set ONLY inside this test's own transaction (the HARD SAFETY CONSTRAINT: never a persisted global change,
never a raw DB write outside a FrappeTestCase). `sweep_resume` commits per execution, so every test that
calls it does so under `_no_commit()`, which both neutralises the commit for isolation AND counts it —
the per-execution commit is itself a contract, and a behavioural test for it is impossible inside a
harness whose whole job is to roll the transaction back. S.6: every planted-bad asserts a genuine
`frappe.throw` / a concrete artefact count, never a hardcoded verdict.
"""
import contextlib
import unittest

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.automation import actions, dispatcher, resume, versions
from tatva_connect.tests.authz.grains import GRAINS, assert_masters_exist
from tatva_connect.tests.automation import field_allowlist

_RULE_DT = "CRM Automation Rule"
_RUN_LOG = "CRM Automation Run Log"
_RESUME_DT = "CRM Automation Resume"
_VERSION_DT = versions.DOCTYPE
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


def _fire(lead_name, rule_name, trigger_doc):
	return dispatcher.run_effects(lead_name, versions.current_name(rule_name), trigger_doc, _AXES, "grain", {}, {})


def _cleanup(prefix, commit=False):
	"""Delete this suite's rows by RULE-NAME prefix — never by "rules that still exist", because the
	on_trash test deletes its rule and would otherwise strand its queue rows.

	`commit=True` for the class that calls `frappe.delete_doc`: that path issues DDL, which implicitly
	commits the open transaction, so FrappeTestCase's rollback can no longer undo anything the test
	wrote. Those rows are real and this suite must remove them itself."""
	like = ("like", f"{prefix}%")
	frappe.db.delete("CRM Automation Action", {"parent": like})
	frappe.db.delete("CRM Automation Criterion", {"parent": like})
	frappe.db.delete(_RUN_LOG, {"rule": like})
	frappe.db.delete(_RESUME_DT, {"rule": like})
	frappe.db.delete(_VERSION_DT, {"rule": like})
	frappe.db.delete(_RULE_DT, {"rule_name": like})
	if commit:
		frappe.db.commit()


def _back_date(resume_name, minutes_ago=1):
	"""Back-date a parked row's resume_at INSIDE this test's own transaction only — never a real,
	persisted global DB write (the hard safety constraint). FrappeTestCase wraps every test in a
	transaction it rolls back, so this never survives past the test."""
	frappe.db.set_value(_RESUME_DT, resume_name, "resume_at", frappe.utils.add_to_date(frappe.utils.now_datetime(), minutes=-minutes_ago))


@contextlib.contextmanager
def _no_commit(counter):
	"""Neutralise `sweep_resume`'s per-execution commit for test isolation, and COUNT it. The count is
	the only way to assert that contract here: a real commit would escape FrappeTestCase's rollback."""
	original = frappe.db.commit
	frappe.db.commit = lambda: counter.append(1)
	try:
		yield
	finally:
		frappe.db.commit = original


def _sweep():
	commits = []
	with _no_commit(commits):
		resume.sweep_resume()
	return len(commits)


def _parked(rule_name):
	return frappe.get_all(
		_RESUME_DT, filters={"rule": rule_name},
		fields=["name", "status", "cursor", "resume_at", "parked_at", "rule_version"], order_by="creation asc",
	)


class TestWaitParksAndResumes(FrappeTestCase):
	"""`[Create Task, Wait(days=14), Update Field]`: firing runs Create Task NOW and parks the remainder;
	a back-dated resume_at + sweep runs Update Field and marks the row Done; a second sweep is a no-op."""

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
		frappe.db.set_value("CRM Tatva Automation", "Task::Automation::resume", "enabled", 1)

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
		result = _fire(self.lead.name, rule.name, self.lead)
		self.assertTrue(result.parked)
		self.assertFalse(result.failed)

		tasks = frappe.get_all("CRM Task", filters={
			"reference_doctype": "CRM Lead", "reference_docname": self.lead.name, "custom_task_type": self.tt_name,
		})
		self.assertTrue(tasks, "Create Task (before the Wait) did not run")

		field_val = frappe.db.get_value("CRM Lead", self.lead.name, "custom_last_report_date")
		self.assertNotEqual(
			frappe.utils.getdate(field_val) if field_val else None, frappe.utils.getdate("2027-01-01"),
			"Update Field (after the Wait) ran before the wait elapsed",
		)

		rows = _parked(rule.name)
		self.assertEqual(len(rows), 1, "exactly one Resume row must be parked")
		self.assertEqual(rows[0].status, "Pending")
		self.assertEqual(rows[0].cursor, 2, "cursor must point at Update Field")
		self.assertEqual(rows[0].rule_version, versions.current_name(rule.name), "the execution must bind to the live version")
		# resume_at is a CACHE of parked_at + the delay — assert the derivation, not a wall clock.
		self.assertEqual(
			frappe.utils.get_datetime(rows[0].resume_at),
			frappe.utils.add_to_date(frappe.utils.get_datetime(rows[0].parked_at), days=14),
		)

		_back_date(rows[0].name)
		self.assertEqual(_sweep(), 1, "sweep_resume must commit once per execution")

		field_val = frappe.db.get_value("CRM Lead", self.lead.name, "custom_last_report_date")
		self.assertEqual(frappe.utils.getdate(field_val), frappe.utils.getdate("2027-01-01"), "Update Field did not run on resume")
		self.assertEqual(frappe.db.get_value(_RESUME_DT, rows[0].name, "status"), "Done")

		# Idempotency: a second sweep over the same window must not re-run Update Field.
		frappe.db.set_value("CRM Lead", self.lead.name, "custom_last_report_date", "2020-01-01")
		self.assertEqual(_sweep(), 0, "a terminal row must not be picked up again")
		self.assertEqual(
			frappe.utils.getdate(frappe.db.get_value("CRM Lead", self.lead.name, "custom_last_report_date")),
			frappe.utils.getdate("2020-01-01"),
			"a second sweep re-ran an already-Done resume row",
		)


class TestChainedWaitReParks(FrappeTestCase):
	"""`[Update Field A, Wait, Update Field B, Wait, Create Note]`: firing runs A and parks before B;
	resuming past the first Wait runs B and parks AGAIN before the note — a RESUMED run re-parks through
	the very same executor."""

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
		frappe.db.set_value("CRM Tatva Automation", "Task::Automation::resume", "enabled", 1)

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
		_fire(self.lead.name, rule.name, self.lead)

		self.assertEqual(
			frappe.utils.getdate(frappe.db.get_value("CRM Lead", self.lead.name, "custom_last_report_date")),
			frappe.utils.getdate("2027-02-01"),
			"the first segment (before the first Wait) did not run",
		)
		rows = _parked(rule.name)
		self.assertEqual(len(rows), 1, "exactly one Resume row after the first fire")
		self.assertEqual(rows[0].cursor, 2)

		_back_date(rows[0].name)
		_sweep()

		self.assertEqual(
			frappe.db.get_value("CRM Lead", self.lead.name, "custom_patient_age"), 42,
			"the second segment (between the two Waits) did not run on the first resume",
		)
		rows2 = _parked(rule.name)
		self.assertEqual(len(rows2), 2, "resuming past the second Wait must produce a NEW parked row (chained re-park)")
		self.assertEqual(rows2[0].status, "Done", "the first resumed row must be marked Done, not re-run")
		self.assertEqual(frappe.db.get_value(_RESUME_DT, rows2[0].name, "status_reason"), "re-parked at a later Wait")
		pending = [r for r in rows2 if r.status == "Pending"]
		self.assertEqual(len(pending), 1)
		self.assertEqual(pending[0].cursor, 4)

		self.assertFalse(self._notes(), "the third segment ran before its own Wait resumed")

		_back_date(pending[0].name)
		_sweep()
		self.assertEqual(len(self._notes()), 1, "the third segment did not run exactly once on the second resume")

	def _notes(self):
		return [
			c for c in frappe.get_all(
				"Comment", filters={"reference_doctype": "CRM Lead", "reference_name": self.lead.name}, pluck="content",
			)
			if "WR-chain: final step" in (c or "")
		]


class TestRuleEditedUnderAParkedLead(FrappeTestCase):
	"""THE versioning contract. One shape — `[Create Note, Wait, Update Field]` — parked at cursor 2,
	then edited four different ways. The note is the durable artefact: if a prefix edit ever re-routed a
	parked lead backwards, the note would exist twice."""

	@classmethod
	def setUpClass(cls):
		assert_masters_exist()
		cls.set_row = field_allowlist.seed_settable(
			"CRM Lead", "custom_last_report_date", _GRAIN["vertical"], _GRAIN["group"], _GRAIN["program"]
		)
		frappe.db.set_value("CRM Tatva Automation", "Task::Automation::rules", "enabled", 1)
		frappe.db.set_value("CRM Tatva Automation", "Task::Automation::resume", "enabled", 1)

	@classmethod
	def tearDownClass(cls):
		_cleanup("WR-edit", commit=True)  # this class calls frappe.delete_doc — see _cleanup's docstring
		frappe.db.delete(_FIELD, {"name": cls.set_row})

	def setUp(self):
		self.lead = _make_lead()

	def tearDown(self):
		frappe.db.delete("Comment", {"reference_doctype": "CRM Lead", "reference_name": self.lead.name})
		frappe.db.delete("CRM Lead", {"name": self.lead.name})

	def _park(self, name):
		rule = _make_rule(name, [
			{"action_type": "Create Note", "comment_mode": "Literal", "comment_text": f"{name}: step one"},
			{"action_type": "Wait", "wait_expression": "{'days': 7}"},
			{"action_type": "Update Field", "target_doctype": "CRM Lead", "fieldname": "custom_last_report_date",
			 "value_mode": "Literal", "value": "2027-04-01"},
		])
		_fire(self.lead.name, rule.name, self.lead)
		row = _parked(rule.name)[0]
		self.assertEqual(row.cursor, 2)
		return rule, row

	def _notes(self, name):
		return [
			c for c in frappe.get_all(
				"Comment", filters={"reference_doctype": "CRM Lead", "reference_name": self.lead.name}, pluck="content",
			)
			if f"{name}: step one" in (c or "")
		]

	def test_suffix_edit_is_adopted_by_the_parked_lead(self):
		rule, row = self._park("WR-edit-suffix")
		v1 = row.rule_version

		rule.actions[2].value = "2028-09-09"  # a step the lead has NOT reached
		rule.save(ignore_permissions=True)
		v2 = versions.current_name(rule.name)
		self.assertNotEqual(v1, v2, "editing an action must mint a new version")

		self.assertEqual(
			frappe.db.get_value(_RESUME_DT, row.name, "rule_version"), v2,
			"a suffix-only edit must be adopted by the parked lead",
		)
		_back_date(row.name)
		_sweep()
		self.assertEqual(
			frappe.utils.getdate(frappe.db.get_value("CRM Lead", self.lead.name, "custom_last_report_date")),
			frappe.utils.getdate("2028-09-09"),
			"the resumed lead ran the OLD suffix",
		)

	def test_prefix_reorder_retains_the_lead_and_never_repeats_an_action(self):
		"""THE duplicate-message hazard, exactly. The note sits BEFORE the Wait, so a parked lead has
		already run it. Moving it AFTER the cursor means a wrongly-adopted lead would run it a second
		time — a real patient, messaged twice. Retention is what forbids that.

		PLANTED-BAD: force `versions.prefix_matches` to return True and this test fails."""
		rule, row = self._park("WR-edit-reorder")
		v1 = row.rule_version
		self.assertEqual(len(self._notes("WR-edit-reorder")), 1)

		rule.actions.append(rule.actions.pop(0))  # the already-run note moves past the cursor
		rule.save(ignore_permissions=True)
		self.assertNotEqual(versions.current_name(rule.name), v1)

		self.assertEqual(
			frappe.db.get_value(_RESUME_DT, row.name, "rule_version"), v1,
			"a reordered prefix must NOT be adopted — the lead's own history moved under it",
		)
		_back_date(row.name)
		_sweep()

		self.assertEqual(
			len(self._notes("WR-edit-reorder")), 1,
			"the reordered note re-ran — a patient would have been messaged twice",
		)
		self.assertEqual(
			frappe.utils.getdate(frappe.db.get_value("CRM Lead", self.lead.name, "custom_last_report_date")),
			frappe.utils.getdate("2027-04-01"),
			"the retained lead did not finish its own version",
		)
		self.assertEqual(frappe.db.get_value(_RESUME_DT, row.name, "status"), "Done")

	def test_retyping_the_parking_wait_retains_the_lead_without_blocking_the_save(self):
		"""AUDIT REGRESSION: retyping the Wait a lead sleeps in (Wait -> Create Note, same row) is a
		prefix change. The save must SUCCEED (not hard-block with a bogus 'cannot be rescheduled'), the
		lead must RETAIN its old version, and on resume it must finish that version, never silently skip
		the retyped step. Before the fix this either threw on save or silently dropped an action."""
		rule, row = self._park("WR-edit-retype")
		v1 = row.rule_version

		rule.actions[1].action_type = "Create Note"
		rule.actions[1].comment_mode = "Literal"
		rule.actions[1].comment_text = "was a wait"
		rule.actions[1].wait_expression = None
		rule.save(ignore_permissions=True)  # must not throw

		self.assertEqual(frappe.db.get_value(_RESUME_DT, row.name, "rule_version"), v1, "a retyped prefix must retain the lead")
		_back_date(row.name)
		_sweep()
		self.assertEqual(
			frappe.utils.getdate(frappe.db.get_value("CRM Lead", self.lead.name, "custom_last_report_date")),
			frappe.utils.getdate("2027-04-01"),
			"the retained lead did not finish its own frozen version",
		)
		self.assertEqual(frappe.db.get_value(_RESUME_DT, row.name, "status"), "Done")

	def test_deleting_the_parking_wait_retains_the_lead_and_cancels_nothing(self):
		rule, row = self._park("WR-edit-delwait")
		v1 = row.rule_version

		rule.actions.pop(1)  # delete the Wait the lead is asleep in
		rule.save(ignore_permissions=True)

		self.assertEqual(frappe.db.get_value(_RESUME_DT, row.name, "rule_version"), v1)
		self.assertEqual(frappe.db.get_value(_RESUME_DT, row.name, "status"), "Pending", "an edit must never cancel a lead")
		_back_date(row.name)
		_sweep()
		self.assertEqual(
			frappe.utils.getdate(frappe.db.get_value("CRM Lead", self.lead.name, "custom_last_report_date")),
			frappe.utils.getdate("2027-04-01"),
			"the orphan-proof lead did not finish on its own frozen version",
		)

	def test_editing_the_parking_waits_delay_reschedules_from_parked_at(self):
		rule, row = self._park("WR-edit-delay")

		rule.actions[1].wait_expression = "{'days': 30}"
		rule.save(ignore_permissions=True)

		after = frappe.db.get_value(_RESUME_DT, row.name, ["rule_version", "resume_at", "parked_at"], as_dict=True)
		self.assertNotEqual(after.rule_version, row.rule_version, "the delay edit must be adopted")
		self.assertEqual(
			frappe.utils.get_datetime(after.resume_at),
			frappe.utils.add_to_date(frappe.utils.get_datetime(after.parked_at), days=30),
			"resume_at must be RE-DERIVED from parked_at, not restarted from now",
		)

	def test_disabled_rule_holds_the_lead_rather_than_cancelling_it(self):
		rule, row = self._park("WR-edit-disable")
		frappe.db.set_value(_RULE_DT, rule.name, "enabled", 0)  # a pause, not a delete
		_back_date(row.name)

		self.assertEqual(_sweep(), 1, "the sweep still visits the row (and commits) — it just does nothing")
		self.assertEqual(frappe.db.get_value(_RESUME_DT, row.name, "status"), "Pending", "a disabled rule must HOLD, never cancel")
		self.assertIsNone(frappe.db.get_value("CRM Lead", self.lead.name, "custom_last_report_date"))

		frappe.db.set_value(_RULE_DT, rule.name, "enabled", 1)
		_sweep()
		self.assertEqual(frappe.db.get_value(_RESUME_DT, row.name, "status"), "Done", "re-enabling must let the held lead resume")

	def test_deleting_the_rule_cancels_the_parked_lead_even_under_force(self):
		rule, row = self._park("WR-edit-trash")
		# `force=1` is exactly what the operator db-seed uses. It bypasses the LINK check, not on_trash.
		frappe.delete_doc(_RULE_DT, rule.name, force=1, ignore_permissions=True)

		after = frappe.db.get_value(_RESUME_DT, row.name, ["status", "status_reason"], as_dict=True)
		self.assertEqual(after.status, "Cancelled")
		self.assertIn("deleted", after.status_reason)


class TestWaitAuthorTimeGuards(FrappeTestCase):
	"""Misconfiguration is blocked BEFORE it commits, in the Desk form — not months later on a sweep.
	Each case is a planted-bad asserting a real `frappe.throw`."""

	@classmethod
	def setUpClass(cls):
		assert_masters_exist()
		cls.set_row = field_allowlist.seed_settable(
			"CRM Lead", "custom_last_report_date", _GRAIN["vertical"], _GRAIN["group"], _GRAIN["program"]
		)

	@classmethod
	def tearDownClass(cls):
		_cleanup("WR-guard")
		frappe.db.delete(_FIELD, {"name": cls.set_row})

	def _rule_with_wait(self, name, expression, trailing=True):
		rows = [{"action_type": "Wait", "wait_expression": expression}]
		if trailing:
			rows.append({"action_type": "Create Note", "comment_mode": "Literal", "comment_text": "after"})
		return _make_rule(name, rows)

	def test_wait_as_the_last_action_is_rejected(self):
		with self.assertRaises(frappe.exceptions.ValidationError):
			self._rule_with_wait("WR-guard-last", "{'days': 1}", trailing=False)

	def test_non_dict_literal_expression_is_rejected(self):
		with self.assertRaises(frappe.exceptions.ValidationError):
			self._rule_with_wait("WR-guard-nondict", "'not a dict'")

	def test_empty_dict_literal_expression_is_rejected(self):
		with self.assertRaises(frappe.exceptions.ValidationError):
			self._rule_with_wait("WR-guard-empty", "{}")

	def test_unusable_add_to_date_kwargs_are_rejected(self):
		with self.assertRaises(frappe.exceptions.ValidationError):
			self._rule_with_wait("WR-guard-kwargs", "{'not_a_real_kwarg': 1}")

	def test_context_dependent_expression_is_allowed_through(self):
		"""POSITIVE control: `ctx` cannot be evaluated against an empty author-time context, so only its
		syntax is checked. Proves the guard above is not blindly rejecting every expression."""
		rule = self._rule_with_wait("WR-guard-ctx", "{'days': ctx.get('lead_time', 3)}")
		self.assertTrue(frappe.db.exists(_RULE_DT, rule.name))


class TestWaitPlantedBadAtFireTime(FrappeTestCase):
	"""A ctx-dependent Wait expression passes the author-time syntax check and can still blow up at fire
	time. `[Update Field, Wait(bad ctx), Create Note]` must roll the Update Field back too and park
	NOTHING — a failing Wait is an ordinary action failure to `run_effects` (it never reaches
	`_ParkSignal`), so per-segment atomicity applies unchanged."""

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
		frappe.db.delete("Comment", {"reference_doctype": "CRM Lead", "reference_name": self.lead.name})
		frappe.db.delete("CRM Lead", {"name": self.lead.name})

	def test_bad_wait_expression_rolls_back_segment_and_parks_nothing(self):
		baseline = frappe.db.get_value("CRM Lead", self.lead.name, "custom_last_report_date")
		rule = _make_rule("WR-badexpr", [
			{"action_type": "Update Field", "target_doctype": "CRM Lead", "fieldname": "custom_last_report_date",
			 "value_mode": "Literal", "value": "2027-03-01"},
			{"action_type": "Wait", "wait_expression": "{'days': ctx['never_set']}"},
			{"action_type": "Create Note", "comment_mode": "Literal", "comment_text": "unreachable"},
		])
		result = _fire(self.lead.name, rule.name, self.lead)
		self.assertTrue(result.failed, "run_effects must REPORT the failure, not swallow it")
		self.assertFalse(result.parked)

		after = frappe.db.get_value("CRM Lead", self.lead.name, "custom_last_report_date")
		self.assertEqual(after, baseline, "Update Field persisted despite the Wait failing the segment")
		self.assertFalse(_parked(rule.name), "a failed Wait must not park anything")
		logs = frappe.get_all(_RUN_LOG, filters={"rule": rule.name}, fields=["outcome", "actions_success", "actions_failed"])
		self.assertTrue(logs, "no Run Log row written for the failed fire")
		# AUDIT REGRESSION: a fully rolled-back segment must read Failed with zero successes, NOT Partial.
		# The per-action `success += 1` counter used to survive the rollback and mislabel it Partial.
		self.assertEqual(logs[0].outcome, "Failed", "a rolled-back segment must not read as Partial")
		self.assertEqual(logs[0].actions_success, 0, "nothing persisted, so actions_success must be 0")
		self.assertGreater(logs[0].actions_failed, 0)

	def test_a_failure_after_resume_marks_the_execution_failed_not_done(self):
		"""The resumed segment fails because its Update Field target left the can_set allowlist while the
		lead slept (the runtime backstop). The row must read Failed. PLANTED-BAD: revert `run_effects` to
		returning None and this test fails — a dead drip would report success."""
		rule = _make_rule("WR-badexpr-resume", [
			{"action_type": "Create Note", "comment_mode": "Literal", "comment_text": "before the wait"},
			{"action_type": "Wait", "wait_expression": "{'days': 3}"},
			{"action_type": "Update Field", "target_doctype": "CRM Lead", "fieldname": "custom_last_report_date",
			 "value_mode": "Literal", "value": "2027-05-01"},
		])
		_fire(self.lead.name, rule.name, self.lead)
		row = _parked(rule.name)[0]

		frappe.db.set_value(_FIELD, self.set_row, "enabled", 0)  # the allowlist row goes away under the lead
		try:
			_back_date(row.name)
			_sweep()
		finally:
			frappe.db.set_value(_FIELD, self.set_row, "enabled", 1)

		after = frappe.db.get_value(_RESUME_DT, row.name, ["status", "status_reason"], as_dict=True)
		self.assertEqual(after.status, "Failed", "a failed resumed segment must not be marked Done")
		self.assertIn("Run Log", after.status_reason)

	def test_a_failed_park_never_writes_a_lying_parked_run_log(self):
		"""AUDIT REGRESSION: park runs BEFORE the Run Log write and its failure propagates, so the log can
		never claim 'parked' while no queue row exists. The failure reaches the caller's own handler (no
		second, hand-rolled error path in run_effects). Here run_effects is called directly, so the raise
		surfaces; in production router/resume catch it."""
		rule = _make_rule("WR-badexpr-park", [
			{"action_type": "Create Note", "comment_mode": "Literal", "comment_text": "pre-wait note"},
			{"action_type": "Wait", "wait_expression": "{'days': 3}"},
			{"action_type": "Update Field", "target_doctype": "CRM Lead", "fieldname": "custom_last_report_date",
			 "value_mode": "Literal", "value": "2027-06-01"},
		])

		def _boom(**kwargs):
			raise RuntimeError("planted park failure")

		original = resume.park
		resume.park = _boom
		try:
			with self.assertRaises(RuntimeError):
				_fire(self.lead.name, rule.name, self.lead)
		finally:
			resume.park = original

		self.assertFalse(_parked(rule.name), "no queue row exists after a failed park")
		parked_logs = [
			log for log in frappe.get_all(_RUN_LOG, filters={"rule": rule.name}, fields=["details"])
			if "parked" in (log.details or "")
		]
		self.assertEqual(parked_logs, [], "no Run Log may claim 'parked' when park failed")


class TestWaitVerbUnit(FrappeTestCase):
	"""The verb itself, unit-level: it never writes and never parks — it raises `_ParkSignal` carrying
	both the instant it parked and the instant it wakes, so the queue stores the input beside the answer."""

	@classmethod
	def setUpClass(cls):
		assert_masters_exist()

	def test_park_signal_carries_parked_at_and_resume_at(self):
		a = _action_row(action_type="Wait", wait_expression="{'days': 14}")
		with self.assertRaises(actions._ParkSignal) as caught:
			actions._action_wait(a, "unused-lead", {}, _AXES, None)
		signal = caught.exception
		self.assertEqual(signal.resume_at, frappe.utils.add_to_date(signal.parked_at, days=14))

	def test_wait_resume_at_is_the_one_arithmetic(self):
		"""The Wait verb and the version migration must derive a wake time identically — one function."""
		base = frappe.utils.get_datetime("2026-01-01 10:00:00")
		self.assertEqual(
			actions.wait_resume_at("{'months': 1}", {}, base),
			frappe.utils.add_to_date(base, months=1),
		)


if __name__ == "__main__":
	unittest.main()
