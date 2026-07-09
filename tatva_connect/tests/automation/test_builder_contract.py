# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Task 14 sign-off - the v2 builder contract (plan Part G): `describe.builder_schema` is the ONE
emitter the Rule Form Script renders from, and `CRMAutomationRule.validate()` re-derives the SAME
call to reject any deviating rule. The server contract is the testable core (S.6) - the JS is UX,
verified separately by `node --check` + a payload smoke, never Playwright, per the task brief.

Real Frappe engine + meta as the oracle throughout; every "planted-bad" case below asserts a
concrete ValidationError/PermissionError, never a hardcoded pass, so the suite can't be blind
(S.6, recall==1.0 per case).
"""
import unittest

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.automation import describe, rules
from tatva_connect.automation.actions import _ACTION_LANES
from tatva_connect.tests.authz.grains import GRAINS, assert_masters_exist
from tatva_connect.tests.automation import field_allowlist

_DT = "CRM Automation Rule"
_GRAIN = GRAINS[0]

# Real CRM Lead fields, picked for their distinct schema types (live meta, not fixtures - S.6).
_CHOICE_FIELD = "custom_stage"          # Link -> CRM Lead Stage
_TEXT_FIELD = "custom_lsq_lead_number"  # Data
_DATE_FIELD = "custom_dob"              # Date


def _seed_watch(*fieldnames):
	for f in fieldnames:
		field_allowlist.seed_watchable("CRM Lead", f)


def _seed_read(*fieldnames):
	for f in fieldnames:
		field_allowlist.seed_readable("CRM Lead", f)


def _rule_doc(name, criteria=None, actions=None, **overrides):
	base = {
		"doctype": _DT,
		"rule_name": name,
		"enabled": 0,
		"on_doctype": "CRM Lead",
		"event": "Updated",
		"vertical": _GRAIN["vertical"],
		"group": _GRAIN["group"],
		"program": _GRAIN["program"],
		"criteria": criteria or [],
		"actions": actions or [],
	}
	base.update(overrides)
	return frappe.get_doc(base)


class TestBuilderSchemaFields(FrappeTestCase):
	"""`fields` = the typed catalog INTERSECTED with the READ allowlist (can_read, or can_watch which
	implies it)."""

	@classmethod
	def setUpClass(cls):
		assert_masters_exist()
		frappe.set_user("Administrator")
		_seed_watch(_CHOICE_FIELD)
		_seed_read(_TEXT_FIELD)

	@classmethod
	def tearDownClass(cls):
		field_allowlist.clear("CRM Lead")
		frappe.set_user("Administrator")

	# (a) a watchable field appears, typed off the live meta - watch implies read.
	def test_watchable_field_appears_typed(self):
		out = describe.builder_schema("CRM Lead", "Updated")
		entry = next((f for f in out["fields"] if f["key"] == _CHOICE_FIELD), None)
		self.assertIsNotNone(entry)
		live = frappe.get_meta("CRM Lead").get_field(_CHOICE_FIELD)
		self.assertEqual(entry["type"], live.fieldtype)
		self.assertEqual(entry["pick"], {"kind": "link", "target": live.options})

	# (a1) a can_read-only field appears too - a criterion may test a field no change of which fires
	# a rule (the activity-schema case: readable, never a column, so never watchable).
	def test_readable_only_field_appears(self):
		out = describe.builder_schema("CRM Lead", "Updated")
		self.assertIn(_TEXT_FIELD, {f["key"] for f in out["fields"]})

	# (b) PLANTED-BAD: a real CRM Lead field with NEITHER read nor watch must be ABSENT - the
	# allowlist is the fence, not just a hint.
	def test_non_readable_real_field_is_absent(self):
		out = describe.builder_schema("CRM Lead", "Updated")
		keys = {f["key"] for f in out["fields"]}
		self.assertNotIn("custom_patient_age", keys)  # real Int field, deliberately not seeded

	def test_no_on_doctype_is_empty(self):
		out = describe.builder_schema(None, "Updated")
		self.assertEqual(out["fields"], [])


class TestBuilderSchemaOperators(FrappeTestCase):
	"""`operators_by_type` - derived from rules.py's operator families, one source."""

	def test_covers_every_frozen_operator(self):
		out = describe.builder_schema("CRM Lead", "Updated")
		offered = {op for ops in out["operators_by_type"].values() for op in ops}
		frozen = {
			"is", "is not", "greater than", "less than", "at least", "at most",
			"is one of", "is not one of", "contains", "does not contain",
			"is set", "is not set", "is between", "changed to", "changed from…to",
		}
		self.assertEqual(offered, frozen)

	# (c) a choice type (Select/Link) offers equality/membership but not text `contains` or order ops.
	def test_choice_type_excludes_text_and_order_ops(self):
		out = describe.builder_schema("CRM Lead", "Updated")
		ops = set(out["operators_by_type"]["Link"])
		self.assertIn("is", ops)
		self.assertIn("is one of", ops)
		self.assertNotIn("contains", ops)
		self.assertNotIn("greater than", ops)

	# (d) a temporal type offers order ops + between but not `contains`.
	def test_temporal_type_offers_order_ops(self):
		out = describe.builder_schema("CRM Lead", "Updated")
		ops = set(out["operators_by_type"]["Date"])
		self.assertIn("greater than", ops)
		self.assertIn("is between", ops)
		self.assertNotIn("contains", ops)

	# (e) a text type offers `contains` but not the numeric order ops.
	def test_text_type_offers_contains_not_order(self):
		out = describe.builder_schema("CRM Lead", "Updated")
		ops = set(out["operators_by_type"]["Data"])
		self.assertIn("contains", ops)
		self.assertNotIn("greater than", ops)

	def test_operators_by_type_matches_module_function(self):
		self.assertEqual(describe.builder_schema("CRM Lead", "Updated")["operators_by_type"], describe.operators_by_type())


class TestBuilderSchemaVerbs(FrappeTestCase):
	"""`verbs` = actions._ACTION_LANES, the ONE verb registry."""

	def test_verbs_match_action_lanes(self):
		out = describe.builder_schema("CRM Lead", "Updated")
		got = {v["verb"]: v["lane"] for v in out["verbs"]}
		expected = {verb: lane for verb, (lane, _handler) in _ACTION_LANES.items()}
		self.assertEqual(got, expected)

	# (f) a verb's params are real Action-doctype fields, typed off the live meta.
	def test_create_task_params_are_typed(self):
		out = describe.builder_schema("CRM Lead", "Updated")
		verb = next(v for v in out["verbs"] if v["verb"] == "Create Task")
		names = {p["name"] for p in verb["params"]}
		self.assertIn("task_type", names)
		task_type_param = next(p for p in verb["params"] if p["name"] == "task_type")
		self.assertEqual(task_type_param["type"], "Link")
		self.assertEqual(task_type_param["pick"], {"kind": "link", "target": "CRM Task Type"})

	# (g) PLANTED-BAD: a verb with no _ACTION_LANES entry (hypothetical) never appears - proven by
	# asserting the full verb set is an exact match to the registry (test (a) above), not a superset.
	def test_verb_count_matches_registry_exactly(self):
		out = describe.builder_schema("CRM Lead", "Updated")
		self.assertEqual(len(out["verbs"]), len(_ACTION_LANES))


class TestBuilderSchemaSetTargets(FrappeTestCase):
	"""`set_targets` = the grain's can_set allowlist."""

	@classmethod
	def setUpClass(cls):
		assert_masters_exist()
		cls.row = field_allowlist.seed_settable(
			"CRM Lead", "custom_last_report_date",
			vertical=_GRAIN["vertical"], group=_GRAIN["group"], program=_GRAIN["program"],
		)

	@classmethod
	def tearDownClass(cls):
		field_allowlist.clear("CRM Lead")

	def test_settable_field_appears_at_matching_grain(self):
		out = describe.builder_schema("CRM Lead", "Updated", _GRAIN["vertical"], _GRAIN["group"], _GRAIN["program"])
		keys = {f["key"] for f in out["set_targets"]}
		self.assertIn("custom_last_report_date", keys)

	# (h) PLANTED-BAD: a different grain must NOT see this can_set row (grain scoping is real).
	def test_settable_field_absent_at_other_grain(self):
		other = GRAINS[2]
		out = describe.builder_schema("CRM Lead", "Updated", other["vertical"], other["group"], other["program"])
		keys = {f["key"] for f in out["set_targets"]}
		self.assertNotIn("custom_last_report_date", keys)


class TestBuilderSchemaPermission(FrappeTestCase):
	"""S.1 - fail-closed permission gate."""

	def test_permission_gate_denies_unauthorized_user(self):
		try:
			frappe.set_user("Guest")
			with self.assertRaises(frappe.PermissionError):
				describe.builder_schema("CRM Lead", "Updated")
		finally:
			frappe.set_user("Administrator")


class TestValidateReDerivesBuilderContract(FrappeTestCase):
	"""`CRMAutomationRule.validate()` re-derives builder_schema and rejects any deviation - the
	server-side guardrail (JS is only UX). One planted-bad per case (S.6, recall guard)."""

	@classmethod
	def setUpClass(cls):
		assert_masters_exist()
		_seed_watch(_CHOICE_FIELD, _DATE_FIELD)
		_seed_read(_TEXT_FIELD)

	@classmethod
	def tearDownClass(cls):
		field_allowlist.clear("CRM Lead")
		frappe.db.delete("CRM Automation Criterion", {"parent": ("like", "BuilderProbe-%")})
		frappe.db.delete(_DT, {"rule_name": ("like", "BuilderProbe-%")})

	# (i) POSITIVE control: a well-formed criterion on an allowlisted, correctly-typed, coercible
	# field saves clean - proves the guard isn't blindly rejecting everything.
	def test_valid_rule_saves(self):
		doc = _rule_doc(
			"BuilderProbe-valid",
			criteria=[{"field": _CHOICE_FIELD, "operator": "is", "value": "New"}],
		)
		doc.insert(ignore_permissions=True)
		self.assertTrue(frappe.db.exists(_DT, doc.name))

	# (j) PLANTED-BAD (a): a criterion field NOT in builder_schema's fields (not can_watch-enabled)
	# is rejected, even though it is a real field on CRM Lead.
	def test_rejects_field_not_in_allowlist(self):
		doc = _rule_doc(
			"BuilderProbe-badfield",
			criteria=[{"field": "custom_patient_age", "operator": "is", "value": "10"}],
		)
		with self.assertRaises(frappe.exceptions.ValidationError):
			doc.insert(ignore_permissions=True)

	# (k) PLANTED-BAD (b): an operator invalid for the field's schema type (`contains` on a Link)
	# is rejected.
	def test_rejects_operator_invalid_for_type(self):
		doc = _rule_doc(
			"BuilderProbe-badop",
			criteria=[{"field": _CHOICE_FIELD, "operator": "contains", "value": "New"}],
		)
		with self.assertRaises(frappe.exceptions.ValidationError):
			doc.insert(ignore_permissions=True)

	# (l) PLANTED-BAD (c): a value that won't coerce to the field's type (garbage text on a Date
	# field) is rejected.
	def test_rejects_uncoercible_value(self):
		doc = _rule_doc(
			"BuilderProbe-badvalue",
			criteria=[{"field": _DATE_FIELD, "operator": "is", "value": "not-a-date"}],
		)
		with self.assertRaises(frappe.exceptions.ValidationError):
			doc.insert(ignore_permissions=True)

	# (m) PLANTED-BAD (d): a `changed…` operator on event=Created is rejected (no before-state).
	def test_rejects_changed_operator_on_created(self):
		doc = _rule_doc(
			"BuilderProbe-changedcreated",
			event="Created",
			criteria=[{"field": _CHOICE_FIELD, "operator": "changed to", "value": "New"}],
		)
		with self.assertRaises(frappe.exceptions.ValidationError):
			doc.insert(ignore_permissions=True)

	# (n) REGRESSION: the same field/operator pair used in (m) is legal on event=Updated.
	def test_changed_operator_on_updated_is_legal(self):
		doc = _rule_doc(
			"BuilderProbe-changedupdated",
			event="Updated",
			criteria=[{"field": _CHOICE_FIELD, "operator": "changed to", "value": "New"}],
		)
		doc.insert(ignore_permissions=True)
		self.assertTrue(frappe.db.exists(_DT, doc.name))

	# (o) PLANTED-BAD (e): a `changed…` operator on a merely READABLE field is rejected. The router
	# only diffs WATCHED fields, so such a criterion would never match - it must fail at author time,
	# not silently at fire time. The same field with `is` saves (p) - the operator is the fence.
	def test_rejects_changed_operator_on_readable_only_field(self):
		doc = _rule_doc(
			"BuilderProbe-changedunwatched",
			criteria=[{"field": _TEXT_FIELD, "operator": "changed to", "value": "L-1"}],
		)
		with self.assertRaises(frappe.exceptions.ValidationError):
			doc.insert(ignore_permissions=True)

	# (p) REGRESSION: a non-transition operator on that same readable-only field saves clean.
	def test_readable_only_field_with_plain_operator_saves(self):
		doc = _rule_doc(
			"BuilderProbe-readonlyplain",
			criteria=[{"field": _TEXT_FIELD, "operator": "is", "value": "L-1"}],
		)
		doc.insert(ignore_permissions=True)
		self.assertTrue(frappe.db.exists(_DT, doc.name))


if __name__ == "__main__":
	unittest.main()
