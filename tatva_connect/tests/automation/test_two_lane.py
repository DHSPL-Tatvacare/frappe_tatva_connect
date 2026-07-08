# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Task 5 — the two-lane executor. An after-commit action can DO but cannot DENY, so the executor
splits into GUARD (synchronous, validate, can `frappe.throw` and BLOCK the save — no Run Log, the
save may never happen) and EFFECT (after commit, savepoint-atomic, Run Log per fire — unchanged from
Task 4). Real Frappe engine as the oracle: real saves, real throws, real Run Log rows.

`frappe.flags.in_test = True` makes `frappe.enqueue(..., now=True)` run the effect lane synchronously
inside `.save()` (same pattern as test_router.py / test_watch_entry.py) so the after-commit half is
directly observable without a real request-boundary commit.
"""
import unittest

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.automation import dispatcher, router
from tatva_connect.tests.authz.grains import GRAINS, assert_masters_exist
from tatva_connect.tests.automation import field_allowlist

_DT = "CRM Automation Rule"
_RUN_LOG = "CRM Automation Run Log"
_FIELD = field_allowlist.DOCTYPE
_GRAIN = GRAINS[0]


def _make_lead(**extra):
	payload = {
		"doctype": "CRM Lead", "first_name": "TwoLane", "lead_name": "TwoLane Probe", "status": "New",
		"custom_vertical": _GRAIN["vertical"], "custom_group": _GRAIN["group"],
		"custom_current_program": _GRAIN["program"],
	}
	payload.update(extra)
	return frappe.get_doc(payload).insert(ignore_permissions=True)


def _make_rule(name, actions, criteria=None, grain=None, on_doctype="CRM Lead", event="Updated"):
	g = grain or _GRAIN
	return frappe.get_doc({
		"doctype": _DT, "rule_name": name, "enabled": 1,
		"on_doctype": on_doctype, "event": event,
		"vertical": g["vertical"], "group": g["group"], "program": g["program"],
		"criteria": criteria or [],
		"actions": actions,
	}).insert(ignore_permissions=True)


def _cleanup(prefix):
	frappe.db.delete("CRM Automation Action", {"parent": ("like", f"{prefix}%")})
	frappe.db.delete("CRM Automation Criterion", {"parent": ("like", f"{prefix}%")})
	frappe.db.delete(_DT, {"rule_name": ("like", f"{prefix}%")})
	frappe.db.delete(_RUN_LOG, {"rule": ("like", f"{prefix}%")})


class TestGuardBlocksSave(FrappeTestCase):
	"""(a)/(b) — a Require Fields guard blocks the save when its field is blank, and passes when
	filled. Both cases run through a real `.save()` — router.run_guards is wired on validate."""

	@classmethod
	def setUpClass(cls):
		assert_masters_exist()
		frappe.db.set_value("CRM Tatva Automation", "Task::Automation::rules", "enabled", 1)
		cls.rule = _make_rule("TwoLane-guard", [
			{"action_type": "Require Fields", "require_fields": "custom_dob"},
		])

	@classmethod
	def tearDownClass(cls):
		_cleanup("TwoLane-guard")
		frappe.db.delete("CRM Lead", {"lead_name": "TwoLane Probe"})

	def tearDown(self):
		router.clear_live_doctypes_cache()

	def test_blank_field_blocks_save_and_writes_no_run_log(self):
		ld = _make_lead(custom_dob=None)
		ld.status = "Contacted"
		with self.assertRaises(frappe.exceptions.ValidationError):
			ld.save(ignore_permissions=True)
		logs = frappe.get_all(_RUN_LOG, filters={"rule": self.rule.name})
		self.assertEqual(len(logs), 0, "a blocked guard must never write a Run Log row (the save never happened)")

	def test_filled_field_passes_the_guard(self):
		ld = _make_lead(custom_dob="1990-01-01")
		ld.status = "Contacted"
		ld.save(ignore_permissions=True)  # must not raise
		self.assertEqual(frappe.db.get_value("CRM Lead", ld.name, "status"), "Contacted")


class TestGuardPlantedBad(FrappeTestCase):
	"""(e) recall guard — a Require Fields guard whose CRITERIA don't match must not block the save,
	even though its listed field is blank. Proves run_guards evaluates criteria before running
	guard-lane actions, not just "any live guard verb blocks everything"."""

	@classmethod
	def setUpClass(cls):
		assert_masters_exist()
		frappe.db.set_value("CRM Tatva Automation", "Task::Automation::rules", "enabled", 1)
		# Task 14: validate() now re-derives builder_schema, which scopes a criterion's field to the
		# can_watch allowlist - status must be watchable to legally appear as a criterion field.
		cls.watch_row = field_allowlist.seed_watchable("CRM Lead", "status")
		cls.rule = _make_rule(
			"TwoLane-plantedbad",
			actions=[{"action_type": "Require Fields", "require_fields": "custom_dob"}],
			criteria=[{"field": "status", "operator": "is", "value": "TwoLaneNoSuchStatus"}],
		)

	@classmethod
	def tearDownClass(cls):
		_cleanup("TwoLane-plantedbad")
		frappe.db.delete("CRM Lead", {"lead_name": "TwoLane Probe"})
		frappe.db.delete(_FIELD, {"name": cls.watch_row})

	def tearDown(self):
		router.clear_live_doctypes_cache()

	def test_non_matching_criteria_does_not_block(self):
		ld = _make_lead(custom_dob=None)  # the guarded field IS blank
		ld.status = "Contacted"  # but criteria wants status == "TwoLaneNoSuchStatus" - never matches
		ld.save(ignore_permissions=True)  # must not raise - the guard never evaluated its action


class TestEffectLaneRegression(FrappeTestCase):
	"""(c) — unchanged from Task 4: a rule with two effect actions where the 2nd fails at runtime
	rolls the 1st back (per-rule savepoint) and the Run Log records the failure. Proves the lane
	split didn't touch the existing effect-lane guarantee."""

	@classmethod
	def setUpClass(cls):
		assert_masters_exist()
		cls.watch_row = field_allowlist.seed_watchable("CRM Lead", "custom_stage")
		cls.set_row = field_allowlist.seed_settable(
			"CRM Lead", "custom_last_report_date", _GRAIN["vertical"], _GRAIN["group"], _GRAIN["program"]
		)
		cls.lead = _make_lead(custom_dob="1990-01-01")

	@classmethod
	def tearDownClass(cls):
		_cleanup("TwoLane-atomic")
		frappe.db.delete("CRM Lead", {"lead_name": "TwoLane Probe"})
		frappe.db.delete(_FIELD, {"name": cls.watch_row})
		frappe.db.delete(_FIELD, {"name": cls.set_row})

	def test_second_effect_failing_rolls_back_the_first(self):
		baseline = frappe.db.get_value("CRM Lead", self.lead.name, "custom_last_report_date")
		rule = _make_rule("TwoLane-atomic", actions=[
			{"action_type": "Update Field", "target_doctype": "CRM Lead", "fieldname": "custom_last_report_date",
			 "value_mode": "Literal", "value": "2027-01-01"},
			{"action_type": "Update Field", "target_doctype": "CRM Lead", "fieldname": "custom_last_report_date",
			 "value_mode": "Expression", "expression": "add_days(ctx['no_such_key'], 1)"},  # raises at fire
		])
		ctx = {"custom_dob": "1990-01-01"}
		dispatcher.run_effects(self.lead.name, frappe._dict(name=rule.name), self.lead, (_GRAIN["vertical"], _GRAIN["group"], _GRAIN["program"]), "grain", {}, ctx)
		after = frappe.db.get_value("CRM Lead", self.lead.name, "custom_last_report_date")
		self.assertEqual(after, baseline, "action 1 persisted despite action 2 failing - the rule group is not atomic")
		logs = frappe.get_all(_RUN_LOG, filters={"rule": rule.name}, fields=["outcome", "actions_failed"])
		self.assertTrue(logs, "no Run Log row written for the failed fire")
		self.assertIn(logs[0].outcome, ("Partial", "Failed"))
		self.assertGreater(logs[0].actions_failed, 0)


class TestMixedGuardAndEffect(FrappeTestCase):
	"""(d) — a rule mixing a guard + an effect: the guard runs synchronously in validate, the effect
	runs after commit (order proven via a spy on dispatcher.run_guards/run_effects)."""

	@classmethod
	def setUpClass(cls):
		assert_masters_exist()
		frappe.db.set_value("CRM Tatva Automation", "Task::Automation::rules", "enabled", 1)
		cls.set_row = field_allowlist.seed_settable(
			"CRM Lead", "custom_last_report_date", _GRAIN["vertical"], _GRAIN["group"], _GRAIN["program"]
		)
		# The EFFECT lane only enqueues when a WATCHED field actually changed on this save
		# (router.on_updated); the GUARD lane has no such gate (Task 5, by design - see router.run_guards).
		# `status` must be watchable so the after-commit half of this test fires at all.
		cls.watch_row = field_allowlist.seed_watchable("CRM Lead", "status")
		cls.rule = _make_rule("TwoLane-mixed", actions=[
			{"action_type": "Require Fields", "require_fields": "lead_name"},  # always set - passes
			{"action_type": "Update Field", "target_doctype": "CRM Lead", "fieldname": "custom_last_report_date",
			 "value_mode": "Literal", "value": "2027-06-01"},
		])

	@classmethod
	def tearDownClass(cls):
		_cleanup("TwoLane-mixed")
		frappe.db.delete("CRM Lead", {"lead_name": "TwoLane Probe"})
		frappe.db.delete(_FIELD, {"name": cls.set_row})
		frappe.db.delete(_FIELD, {"name": cls.watch_row})

	def tearDown(self):
		router.clear_live_doctypes_cache()

	def test_guard_runs_before_effect(self):
		order = []
		orig_guards, orig_effects = dispatcher.run_guards, dispatcher.run_effects

		def _spy_guards(*a, **kw):
			order.append("guard")
			return orig_guards(*a, **kw)

		def _spy_effects(*a, **kw):
			order.append("effect")
			return orig_effects(*a, **kw)

		dispatcher.run_guards = _spy_guards
		dispatcher.run_effects = _spy_effects
		frappe.flags.in_test = True  # synchronous enqueue - the effect lane runs inside .save()
		# router._watchable_fields_for caches per-flags (request-scoped in prod); an earlier test in
		# this same process may have cached CRM Lead's watchable set BEFORE `status` was seeded above -
		# drop it so this test observes the fresh seed (pre-existing cache shape, not a Task 5 change).
		frappe.flags.pop("_watchable_fields_cache", None)
		try:
			ld = _make_lead()
			ld.status = "Contacted"
			ld.save(ignore_permissions=True)
		finally:
			dispatcher.run_guards = orig_guards
			dispatcher.run_effects = orig_effects
			frappe.flags.in_test = False

		self.assertEqual(order, ["guard", "effect"], "guard did not run before effect")
		out = frappe.db.get_value("CRM Lead", ld.name, "custom_last_report_date")
		self.assertEqual(frappe.utils.getdate(out), frappe.utils.getdate("2027-06-01"))
		frappe.db.delete("CRM Lead", {"name": ld.name})


if __name__ == "__main__":
	unittest.main()
