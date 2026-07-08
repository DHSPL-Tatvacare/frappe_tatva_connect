# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Leg E+F sign-off - Action extensions (Expression / Add Comment / due_mode) + dispatcher handlers.

Calls the dispatcher action handlers directly with a hand-built context (the full fire-rules path
is Leg G's sign-off). Real Frappe engine as the oracle - real saves, real allowlist, real throws.
"""
import datetime
import unittest

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.automation import dispatcher
from tatva_connect.tests.authz.grains import GRAINS, assert_masters_exist
from tatva_connect.tests.automation import field_allowlist

_DT = "CRM Automation Rule"
_FIELD = field_allowlist.DOCTYPE
_GRAIN = GRAINS[0]
_SET_TARGET = "custom_last_report_date"  # real CRM Lead Date field (pre-spike)
_SET_SOURCE = "custom_dob"  # real CRM Lead Date field - the expression input


def _make_lead(dob=None):
	"""A throwaway lead on the canonical grain."""
	ld = frappe.get_doc(
		{
			"doctype": "CRM Lead",
			"first_name": "LegEF",
			"lead_name": "LegEF Probe",
			"status": "New",
			"custom_vertical": _GRAIN["vertical"],
			"custom_group": _GRAIN["group"],
			"custom_current_program": _GRAIN["program"],
			"custom_dob": dob or "1990-01-01",
		}
	)
	ld.insert(ignore_permissions=True)
	return ld


def _action_row(**fields):
	"""A duck-typed action row - the dispatcher reads attributes off it."""
	class _A:
		pass
	a = _A()
	for k, v in fields.items():
		setattr(a, k, v)
	return a


class TestActionExpressionAndComment(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		assert_masters_exist()
		cls.allowlist = field_allowlist.seed_settable("CRM Lead", _SET_TARGET, _GRAIN["vertical"], _GRAIN["group"], _GRAIN["program"])
		cls.lead = _make_lead(dob="2026-07-06")
		cls.lead_axes = (_GRAIN["vertical"], _GRAIN["group"], _GRAIN["program"])

	@classmethod
	def tearDownClass(cls):
		frappe.db.delete("CRM Lead", {"lead_name": "LegEF Probe"})
		frappe.db.delete(_FIELD, {"name": cls.allowlist})
		frappe.db.delete("Comment", {"reference_doctype": "CRM Lead", "reference_name": cls.lead.name})
		frappe.db.delete("CRM Task", {"reference_doctype": "CRM Lead", "reference_docname": cls.lead.name})

	def _ctx(self, **extra):
		ctx = {
			"custom_dob": "2026-07-06",
			"custom_stage": "B",
			"custom_stage__before": "A",
		}
		ctx.update(extra)
		return ctx

	# (a) Set Field Expression: custom_last_report_date = custom_dob + 3 days.
	def test_set_field_expression_writes_computed_date(self):
		a = _action_row(
			action_type="Set Field",
			target_doctype="CRM Lead",
			fieldname=_SET_TARGET,
			value_mode="Expression",
			expression="add_days(ctx['custom_dob'], 3)",
		)
		dispatcher._action_set_field(a, self.lead.name, self._ctx(), self.lead_axes, self.lead)
		out = frappe.db.get_value("CRM Lead", self.lead.name, _SET_TARGET)
		self.assertEqual(frappe.utils.getdate(out), datetime.date(2026, 7, 9))

	# (b) Set Field Expression referencing a MISSING ctx key -> raises -> no partial write.
	def test_set_field_expression_missing_ctx_key_raises_and_does_not_write(self):
		before = frappe.db.get_value("CRM Lead", self.lead.name, _SET_TARGET)
		a = _action_row(
			action_type="Set Field",
			target_doctype="CRM Lead",
			fieldname=_SET_TARGET,
			value_mode="Expression",
			expression="add_days(ctx['no_such_key'], 3)",
		)
		with self.assertRaises(Exception):
			dispatcher._action_set_field(a, self.lead.name, self._ctx(), self.lead_axes, self.lead)
		after = frappe.db.get_value("CRM Lead", self.lead.name, _SET_TARGET)
		self.assertEqual(before, after, "field was written despite the action raising - partial write leak")

	# (c) Create Task due_mode=Expression -> task created with the resolved due date.
	def test_create_task_expression_due_date(self):
		# A throwaway grain-keyed task type for the Create Task target.
		tt_name = f"{_GRAIN['vertical']}::{_GRAIN['group']}::{_GRAIN['program']}::LegEFTask"
		if not frappe.db.exists("CRM Task Type", tt_name):
			frappe.get_doc(
				{"doctype": "CRM Task Type", "type_name": "LegEFTask",
				 "vertical": _GRAIN["vertical"], "group": _GRAIN["group"], "program": _GRAIN["program"]}
			).insert(ignore_permissions=True)
		try:
			a = _action_row(
				action_type="Create Task",
				task_type=tt_name,
				due_mode="Expression",
				due_expression="ctx['custom_dob']",
			)
			dispatcher._action_create_task(a, self.lead.name, self._ctx(), self.lead_axes, self.lead)
			tasks = frappe.get_all(
				"CRM Task",
				filters={"reference_doctype": "CRM Lead", "reference_docname": self.lead.name, "custom_task_type": tt_name},
				pluck="name",
			)
			self.assertTrue(tasks, "follow-up task was not created")
			# create_followup_task sets `due_date` from the resolved due_at - pin it to the input.
			due = frappe.db.get_value("CRM Task", tasks[0], "due_date")
			self.assertEqual(
				frappe.utils.getdate(due),
				frappe.utils.getdate("2026-07-06"),
				"due_mode=Expression did not propagate the resolved date onto the task",
			)
		finally:
			frappe.db.delete("CRM Task", {"reference_doctype": "CRM Lead", "reference_docname": self.lead.name})
			frappe.db.delete("CRM Task Type", {"name": tt_name})

	# (d) Add Comment Literal -> a Comment row lands on the subject lead.
	def test_add_comment_literal_lands_on_lead(self):
		a = _action_row(
			action_type="Add Comment",
			comment_mode="Literal",
			comment_text="Auto: lead dropped after scheduled visit outcome.",
		)
		dispatcher._action_add_comment(a, self.lead.name, self._ctx(), self.lead_axes, self.lead)
		comments = frappe.get_all(
			"Comment",
			filters={"reference_doctype": "CRM Lead", "reference_name": self.lead.name},
			pluck="content",
		)
		self.assertTrue(any("Auto: lead dropped" in (c or "") for c in comments), "comment did not land on the lead")

	# (e) Add Comment Expression -> resolved string lands. NB: safe_eval strips `str` from builtins,
	# so use an f-string (it calls __format__, not str()) - the same boundary Notification/Workflow
	# conditions already live within. No new trust boundary (plan §4).
	def test_add_comment_expression_lands(self):
		a = _action_row(
			action_type="Add Comment",
			comment_mode="Expression",
			comment_expression="f'Stage moved: {ctx[\"custom_stage__before\"]} -> {ctx[\"custom_stage\"]}'",
		)
		dispatcher._action_add_comment(a, self.lead.name, self._ctx(), self.lead_axes, self.lead)
		comments = frappe.get_all(
			"Comment",
			filters={"reference_doctype": "CRM Lead", "reference_name": self.lead.name},
			pluck="content",
		)
		# Comment.content is an HTML field, so `->` lands as `-&gt;`. Assert on the resolved
		# before/after values (the real contract) - escape-robust, not asserting the arrow glyph.
		self.assertTrue(
			any("Stage moved" in (c or "") and "A" in (c or "") and "B" in (c or "") for c in comments),
			"expression comment did not land or did not resolve",
		)

	# (f) Set Field on a NON-allowlisted field -> PermissionError (the allowlist is the SOLE gate).
	# NB: the dispatcher raises Python's builtin PermissionError (parity with the existing v1 code),
	# not frappe.exceptions.PermissionError - assert the builtin.
	def test_set_field_non_allowlisted_throws(self):
		a = _action_row(
			action_type="Set Field",
			target_doctype="CRM Lead",
			fieldname="custom_patient_age",  # real field, NOT in the allowlist
			value_mode="Literal",
			value="99",
		)
		with self.assertRaises(PermissionError):
			dispatcher._action_set_field(a, self.lead.name, self._ctx(), self.lead_axes, self.lead)

	# (g) REGRESSION: Set Field Literal mode still works (the existing v1 path).
	def test_set_field_literal_still_works(self):
		a = _action_row(
			action_type="Set Field",
			target_doctype="CRM Lead",
			fieldname=_SET_TARGET,
			value_mode="Literal",
			value="2026-12-31",
		)
		dispatcher._action_set_field(a, self.lead.name, self._ctx(), self.lead_axes, self.lead)
		out = frappe.db.get_value("CRM Lead", self.lead.name, _SET_TARGET)
		self.assertEqual(frappe.utils.getdate(out), datetime.date(2026, 12, 31))

	# (h) Add Comment does NOT re-save the subject -> no re-entrancy surface from add_comment.
	def test_add_comment_does_not_resave_subject(self):
		modified_before = frappe.db.get_value("CRM Lead", self.lead.name, "modified")
		a = _action_row(
			action_type="Add Comment",
			comment_mode="Literal",
			comment_text="no-resave probe",
		)
		dispatcher._action_add_comment(a, self.lead.name, self._ctx(), self.lead_axes, self.lead)
		modified_after = frappe.db.get_value("CRM Lead", self.lead.name, "modified")
		self.assertEqual(modified_before, modified_after, "add_comment re-saved the subject - new on_update re-entrancy surface")

	# (i) WRITE-GAP: a Field-Changed rule on a Task can Set a field ON THAT TASK (not only the Lead).
	# trigger_doc IS the task; _resolve_write_target targets it. Closes the gap the merge fixed.
	def test_set_field_targets_the_trigger_task(self):
		task_row = field_allowlist.seed_settable("CRM Task", "title", _GRAIN["vertical"], _GRAIN["group"], _GRAIN["program"])
		task = frappe.get_doc({"doctype": "CRM Task", "title": "gap probe",
			"reference_doctype": "CRM Lead", "reference_docname": self.lead.name, "status": "Todo"}).insert(ignore_permissions=True)
		try:
			a = _action_row(action_type="Set Field", target_doctype="CRM Task", fieldname="title",
				value_mode="Literal", value="written-by-automation")
			dispatcher._action_set_field(a, self.lead.name, self._ctx(), self.lead_axes, task)
			self.assertEqual(frappe.db.get_value("CRM Task", task.name, "title"), "written-by-automation")
		finally:
			frappe.db.delete("CRM Task", {"name": task.name})
			frappe.db.delete(_FIELD, {"name": task_row})

	# (j) SCOPE GUARD: a Set Field target that is neither the Lead nor the trigger doc raises — even
	# when the allowlist admits it (the allowlist gates the field; the scope guard gates the record).
	# Here the trigger_doc is the Lead, so a CRM Task target is out of scope.
	def test_set_field_out_of_scope_target_raises(self):
		row = field_allowlist.seed_settable("CRM Task", "title", _GRAIN["vertical"], _GRAIN["group"], _GRAIN["program"])
		try:
			a = _action_row(action_type="Set Field", target_doctype="CRM Task", fieldname="title",
				value_mode="Literal", value="x")
			# trigger_doc is the Lead; target CRM Task is neither the Lead nor the trigger doc -> ValueError.
			with self.assertRaises(ValueError):
				dispatcher._action_set_field(a, self.lead.name, self._ctx(), self.lead_axes, self.lead)
		finally:
			frappe.db.delete(_FIELD, {"name": row})

	# (k) GROUP ATOMICITY: a rule with two Update Field actions where the SECOND fails at runtime
	# rolls BOTH back (the per-rule savepoint) - the first action's write must not persist.
	def test_rule_group_atomicity(self):
		watch_row = field_allowlist.seed_watchable("CRM Lead", "custom_stage")
		baseline = frappe.db.get_value("CRM Lead", self.lead.name, _SET_TARGET)
		rule = frappe.get_doc({
			"doctype": _DT, "rule_name": "LegEF-atomic", "enabled": 1,
			"on_doctype": "CRM Lead", "event": "Updated",
			"vertical": _GRAIN["vertical"], "group": _GRAIN["group"], "program": _GRAIN["program"],
			"actions": [
				{"action_type": "Update Field", "target_doctype": "CRM Lead", "fieldname": _SET_TARGET,
				 "value_mode": "Literal", "value": "2027-01-01"},
				{"action_type": "Update Field", "target_doctype": "CRM Lead", "fieldname": _SET_TARGET,
				 "value_mode": "Expression", "expression": "add_days(ctx['no_such_key'], 1)"},  # raises at fire
			],
		}).insert(ignore_permissions=True)
		try:
			# run_effects catches the action failure, rolls the savepoint back, and logs (no re-raise).
			dispatcher.run_effects(self.lead.name, frappe._dict(name=rule.name), self.lead, self.lead_axes, "grain", {}, self._ctx())
			after = frappe.db.get_value("CRM Lead", self.lead.name, _SET_TARGET)
			self.assertEqual(after, baseline, "action 1 persisted despite action 2 failing - the rule group is not atomic")
		finally:
			frappe.db.delete(_DT, {"name": rule.name})
			frappe.db.delete(_FIELD, {"name": watch_row})
			frappe.db.delete("CRM Automation Run Log", {"rule": rule.name})


if __name__ == "__main__":
	unittest.main()
