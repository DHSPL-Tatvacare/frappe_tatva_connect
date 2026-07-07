# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Leg B sign-off - CRM Automation Rule trigger fields (Task 1: on_doctype/event) + validate().

Real Frappe engine as the oracle. Uses the canonical authz grain (GoodFlip Care::Anaya::Nivolumab)
and a throwaway CRM Task Type for the "task completed" v2 regression (on_doctype=CRM Task,
event=Updated + criterion status changed to Done - the no-sugar-labels grammar, plan Global
Constraints). No mocked verdicts.

TATVA v2 (Task 1): rewritten off the retired trigger_type/task_type/watch_doctype/watch_field
fields onto on_doctype/event (plan Part A/C Task 1) - see the brief's fold-in step 5.
"""
import unittest

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.tests.authz.grains import GRAINS, assert_masters_exist
from tatva_connect.tests.automation import field_allowlist

_DT = "CRM Automation Rule"
_LEAD_FIELD = "custom_stage"  # real CRM Lead Select field (pre-spike)
_GRAIN = GRAINS[0]  # GoodFlip Care::Anaya::Nivolumab


def _make_task_type(name):
	"""A throwaway grain-keyed CRM Task Type for the "task completed" regression path."""
	tt = f"{_GRAIN['vertical']}::{_GRAIN['group']}::{_GRAIN['program']}::{name}"
	if not frappe.db.exists("CRM Task Type", tt):
		frappe.get_doc(
			{
				"doctype": "CRM Task Type",
				"type_name": name,
				"vertical": _GRAIN["vertical"],
				"group": _GRAIN["group"],
				"program": _GRAIN["program"],
			}
		).insert(ignore_permissions=True)
	return tt


class TestRuleValidateFieldChange(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		assert_masters_exist()  # fail loud if the canonical grain masters aren't seeded
		cls.task_type = _make_task_type("LegBProbe")
		# Seed a can_watch row so a rule can legally watch it (still consulted by the runtime
		# dispatcher — Task 4/5 own rewiring the read side onto on_doctype/event).
		field_allowlist.seed_watchable("CRM Lead", _LEAD_FIELD)

	@classmethod
	def tearDownClass(cls):
		frappe.db.delete(_DT, {"rule_name": ("like", "LegB%")})
		frappe.db.delete("CRM Task Type", {"name": cls.task_type})
		field_allowlist.clear("CRM Lead")

	def _rule_doc(self, **overrides):
		base = {
			"doctype": _DT,
			"rule_name": f"LegB-{self._testMethodName}",
			"enabled": 0,
			"on_doctype": "CRM Lead",
			"event": "Updated",
			"vertical": _GRAIN["vertical"],
			"group": _GRAIN["group"],
			"program": _GRAIN["program"],
		}
		base.update(overrides)
		return frappe.get_doc(base)

	# (a) a well-formed rule saves.
	def test_valid_rule_saves(self):
		doc = self._rule_doc()
		doc.insert(ignore_permissions=True)
		self.assertTrue(frappe.db.exists(_DT, doc.name))
		self.assertEqual(doc.on_doctype, "CRM Lead")
		self.assertEqual(doc.event, "Updated")

	# (b) missing on_doctype raises (incomplete trigger). NB: event isn't a useful "missing" probe -
	# the schema's "Updated" default backfills any falsy value before validate() runs.
	def test_missing_on_doctype_throws(self):
		doc = self._rule_doc(on_doctype=None)
		with self.assertRaises(frappe.exceptions.ValidationError):
			doc.insert(ignore_permissions=True)

	# (c) an unknown criterion field raises ("unwatched field" retargeted: v2 has no per-criterion
	# watch allowlist — any real on_doctype meta field is a legal criterion field — so the
	# equivalent v2 authoring guard is a field that doesn't exist on on_doctype at all).
	def test_unknown_criterion_field_throws(self):
		doc = self._rule_doc(criteria=[{"field": "no_such_field_xyz", "operator": "is", "value": "x"}])
		with self.assertRaises(frappe.exceptions.ValidationError):
			doc.insert(ignore_permissions=True)

	# (d) an unsupported on_doctype raises.
	def test_unsupported_on_doctype_throws(self):
		doc = self._rule_doc(on_doctype="Customer")
		with self.assertRaises(frappe.exceptions.ValidationError):
			doc.insert(ignore_permissions=True)

	# (e) REGRESSION: an invalid event value raises (native Select validation — the v2 replacement
	# for the old "Task Completed without task_type" incompleteness check).
	def test_invalid_event_value_throws(self):
		doc = self._rule_doc(event="Bogus")
		with self.assertRaises(frappe.exceptions.ValidationError):
			doc.insert(ignore_permissions=True)

	# (f) REGRESSION: the migrated shape of the old "Task Completed" trigger still saves -
	# on_doctype=CRM Task, event=Updated + criterion `status changed to Done` (mirrors
	# patches.reshape_automation_triggers's output for a Task-Completed rule).
	def test_migrated_task_completed_shape_still_saves(self):
		doc = self._rule_doc(
			on_doctype="CRM Task", event="Updated",
			criteria=[{"field": "status", "operator": "changed to", "value": "Done"}],
		)
		doc.insert(ignore_permissions=True)
		self.assertTrue(frappe.db.exists(_DT, doc.name))

	# (g) an Update Field targeting a doctype OUTSIDE the rule's scope (not the Lead, not the
	# trigger doc) is rejected at AUTHOR time — mirrors the dispatcher's runtime scope guard.
	def test_update_field_out_of_scope_target_throws(self):
		doc = self._rule_doc(actions=[{
			"action_type": "Update Field", "target_doctype": "CRM Task", "fieldname": "title",
			"value_mode": "Literal", "value": "x",
		}])  # rule triggers on CRM Lead -> a CRM Task target is out of scope
		with self.assertRaises(frappe.exceptions.ValidationError):
			doc.insert(ignore_permissions=True)


if __name__ == "__main__":
	unittest.main()
