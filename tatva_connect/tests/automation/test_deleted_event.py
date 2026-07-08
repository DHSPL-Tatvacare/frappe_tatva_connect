# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Task 10 sign-off — the Deleted event (`on_trash`). This WIRES a brand-new hook (`router.on_deleted`
+ `router.run_for_delete`), so this suite is the primary proof, not a rewiring:
  1. An end-to-end real delete: an enabled `On CRM Task Deleted` rule fires after commit and its
     effect lands on the still-existing subject lead; the Run Log records the trigger doctype.
  2. THE nuance: `run_for_delete` must NOT re-fetch the trigger doc (it's gone by the time the
     after-commit job runs) — proven by calling it directly with a docname that never existed in the
     DB at all; a re-fetch would raise `DoesNotExistError`, so a landed effect built from the exact
     text we captured proves the CONTEXT was used, not a re-fetch.
  3. A doctype/grain with no Deleted rule -> nothing fires (early-return; no Run Log, no effect).
  4. Planted-bad: `changed to` needs a before-state that a delete never has - CRMAutomationRule's
     authoring guard already rejects it for any event != Updated (same guard Created is proven by,
     see test_criterion_changed_from_to.py) - proven here for event=Deleted specifically.

Real Frappe engine as the oracle; frappe.flags.in_test makes `frappe.enqueue(..., now=True)` run
synchronously so the effect is observable directly in the same test."""
import unittest

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.automation import router
from tatva_connect.tests.authz.grains import GRAINS, assert_masters_exist

_DT = "CRM Automation Rule"
_RUN_LOG = "CRM Automation Run Log"
_GRAIN = GRAINS[0]


def _make_lead(**extra):
	payload = {
		"doctype": "CRM Lead", "first_name": "DeletedProbe", "lead_name": "Deleted Probe", "status": "New",
		"custom_vertical": _GRAIN["vertical"], "custom_group": _GRAIN["group"],
		"custom_current_program": _GRAIN["program"],
	}
	payload.update(extra)
	return frappe.get_doc(payload).insert(ignore_permissions=True)


def _make_task(lead_name, **extra):
	payload = {
		"doctype": "CRM Task", "title": "DeletedProbe task", "assigned_to": "Administrator",
		"reference_doctype": "CRM Lead", "reference_docname": lead_name, "status": "Todo",
	}
	payload.update(extra)
	return frappe.get_doc(payload).insert(ignore_permissions=True)


def _make_deleted_rule(name, actions, criteria=None, on_doctype="CRM Task", enabled=1, grain=None):
	g = grain or _GRAIN
	return frappe.get_doc({
		"doctype": _DT, "rule_name": name, "enabled": enabled,
		"on_doctype": on_doctype, "event": "Deleted",
		"vertical": g["vertical"], "group": g["group"], "program": g["program"],
		"actions": actions, "criteria": criteria or [],
	}).insert(ignore_permissions=True)


def _purge_fixtures():
	"""Defensive pre-clean, by our own naming pattern only — belt-and-braces against a prior run's
	fixture surviving a crash mid-setup (before this suite's try/finally cleanup existed)."""
	rule_names = frappe.get_all(_DT, filters={"rule_name": ("like", "DeletedProbe-%")}, pluck="name")
	for name in rule_names:
		frappe.db.delete("CRM Automation Action", {"parent": name})
	if rule_names:
		frappe.db.delete(_DT, {"name": ("in", rule_names)})
		frappe.db.delete(_RUN_LOG, {"rule": ("in", rule_names)})
	lead_names = frappe.get_all("CRM Lead", filters={"first_name": "DeletedProbe"}, pluck="name")
	for name in lead_names:
		frappe.db.delete("Comment", {"reference_doctype": "CRM Lead", "reference_name": name})
	if lead_names:
		frappe.db.delete("CRM Lead", {"name": ("in", lead_names)})


class TestDeletedEventFiresEffect(FrappeTestCase):
	"""(1) An enabled `On CRM Task Deleted` rule fires from `on_trash`; its effect lands on the
	still-existing subject lead after commit, and the Run Log records the trigger doctype."""

	@classmethod
	def setUpClass(cls):
		assert_masters_exist()
		_purge_fixtures()
		frappe.db.set_value("CRM Tatva Automation", router.KILL_SWITCH, "enabled", 1)

	def tearDown(self):
		router.clear_live_doctypes_cache()

	def test_delete_fires_rule_effect_on_subject_lead(self):
		lead, rule = None, None
		try:
			lead = _make_lead()
			rule = _make_deleted_rule(
				"DeletedProbe-fire",
				actions=[{"action_type": "Create Note", "comment_mode": "Literal", "comment_text": "deleted probe fired"}],
			)
			task = _make_task(lead.name)
			frappe.flags.in_test = True
			frappe.delete_doc("CRM Task", task.name, ignore_permissions=True)
			comments = frappe.get_all(
				"Comment", filters={"reference_doctype": "CRM Lead", "reference_name": lead.name}, pluck="content",
			)
			self.assertTrue(
				any("deleted probe fired" in (c or "") for c in comments),
				"Create Note effect did not land on the subject lead after the trigger task was deleted",
			)
			logs = frappe.get_all(
				_RUN_LOG, filters={"rule": rule.name},
				fields=["trigger_doctype", "trigger_docname", "outcome", "lead"],
			)
			self.assertTrue(logs, "no Run Log row written for the Deleted fire")
			self.assertEqual(len(logs), 1, "more than one Run Log row for this rule - a stray rule is cross-firing")
			self.assertEqual(logs[0].trigger_doctype, "CRM Task")
			# CRM Task autonames as an integer; trigger_docname is a Data (string) column.
			self.assertEqual(logs[0].trigger_docname, str(task.name))
			self.assertEqual(logs[0].lead, lead.name)
			self.assertEqual(logs[0].outcome, "Success")
		finally:
			frappe.flags.in_test = False
			if lead:
				frappe.db.delete("Comment", {"reference_doctype": "CRM Lead", "reference_name": lead.name})
				frappe.db.delete("CRM Lead", {"name": lead.name})
			if rule:
				frappe.db.delete(_RUN_LOG, {"rule": rule.name})
				frappe.db.delete("CRM Automation Action", {"parent": rule.name})
				frappe.db.delete(_DT, {"name": rule.name})


class TestDeletedEventUsesCapturedContextNotRefetch(FrappeTestCase):
	"""(2) THE nuance: the trigger row is gone by the time the effect lane runs, so `run_for_delete`
	must build the trigger doc from the CONTEXT captured synchronously in `on_deleted`, never re-fetch
	it. Calling `run_for_delete` directly with a docname that never existed makes any re-fetch attempt
	raise `frappe.DoesNotExistError` immediately - a landed effect proves the captured context (not a
	DB read) is what the handler actually used."""

	@classmethod
	def setUpClass(cls):
		assert_masters_exist()
		_purge_fixtures()
		frappe.db.set_value("CRM Tatva Automation", router.KILL_SWITCH, "enabled", 1)

	def test_run_for_delete_builds_trigger_from_captured_context(self):
		lead, rule = None, None
		ghost_name = "DP-GHOST-does-not-exist-0001"
		try:
			lead = _make_lead()
			self.assertFalse(frappe.db.exists("CRM Task", ghost_name), "test premise: the ghost docname must never exist")
			rule = _make_deleted_rule(
				"DeletedProbe-norefetch",
				actions=[{
					"action_type": "Create Note", "comment_mode": "Expression",
					"comment_expression": "'captured: ' + (ctx.get('title') or '')",
				}],
			)
			context = {
				"name": ghost_name, "title": "ghost trigger title", "assigned_to": "Administrator",
				"reference_doctype": "CRM Lead", "reference_docname": lead.name, "status": "Done",
			}
			router.run_for_delete("CRM Task", ghost_name, lead.name, context)
			comments = frappe.get_all(
				"Comment", filters={"reference_doctype": "CRM Lead", "reference_name": lead.name}, pluck="content",
			)
			self.assertTrue(
				any("captured: ghost trigger title" in (c or "") for c in comments),
				"the effect did not use the CAPTURED context - a re-fetch of the (nonexistent) trigger "
				"doc would have raised, not silently produced the wrong text",
			)
			logs = frappe.get_all(_RUN_LOG, filters={"rule": rule.name}, fields=["trigger_docname", "outcome"])
			self.assertTrue(logs, "no Run Log row written")
			self.assertEqual(logs[0].trigger_docname, ghost_name, "Run Log did not carry the passed (ghost) docname")
			self.assertEqual(logs[0].outcome, "Success")
		finally:
			if lead:
				frappe.db.delete("Comment", {"reference_doctype": "CRM Lead", "reference_name": lead.name})
				frappe.db.delete("CRM Lead", {"name": lead.name})
			if rule:
				frappe.db.delete(_RUN_LOG, {"rule": rule.name})
				frappe.db.delete("CRM Automation Action", {"parent": rule.name})
				frappe.db.delete(_DT, {"name": rule.name})


class TestDeletedEventNoRuleFiresNothing(FrappeTestCase):
	"""(3) A doctype/grain with no Deleted rule -> nothing fires. Deletes a real CRM Task with no
	Deleted-event rule registered anywhere for it; asserts no Run Log row and no effect landed. Robust
	to whether CRM Task happens to be "live" from an unrelated Updated-event rule elsewhere in the
	suite - `matching_rules(doctype, "Deleted", ...)` only ever returns Deleted rows."""

	@classmethod
	def setUpClass(cls):
		assert_masters_exist()
		_purge_fixtures()
		frappe.db.set_value("CRM Tatva Automation", router.KILL_SWITCH, "enabled", 1)

	def tearDown(self):
		router.clear_live_doctypes_cache()

	def test_no_deleted_rule_enqueues_nothing(self):
		lead = None
		try:
			lead = _make_lead()
			task = _make_task(lead.name)
			frappe.flags.in_test = True
			frappe.delete_doc("CRM Task", task.name, ignore_permissions=True)
			logs = frappe.get_all(_RUN_LOG, filters={"trigger_doctype": "CRM Task", "trigger_docname": task.name})
			self.assertFalse(logs, "a Run Log row was written despite no Deleted rule existing for CRM Task")
			comments = frappe.get_all(
				"Comment", filters={"reference_doctype": "CRM Lead", "reference_name": lead.name},
			)
			self.assertFalse(comments, "an effect landed on the lead despite no Deleted rule existing")
		finally:
			frappe.flags.in_test = False
			if lead:
				frappe.db.delete("CRM Lead", {"name": lead.name})


class TestChangedOperatorRejectedOnDeleted(FrappeTestCase):
	"""(4) Planted-bad: `changed to` needs a before-state a Deleted fire never has. The authoring guard
	(CRMAutomationRule._validate_changed_operator) already rejects any `changed…` operator when
	event != Updated - proven for Created in test_criterion_changed_from_to.py; proven here for
	event=Deleted specifically, so a rule that could never legitimately fire can never even be saved."""

	@classmethod
	def setUpClass(cls):
		assert_masters_exist()

	def tearDown(self):
		frappe.db.delete(_DT, {"rule_name": ("like", "DeletedProbe-%")})

	def test_changed_to_on_deleted_event_throws(self):
		doc = frappe.get_doc({
			"doctype": _DT, "rule_name": "DeletedProbe-changed-to", "enabled": 0,
			"on_doctype": "CRM Task", "event": "Deleted",
			"vertical": _GRAIN["vertical"], "group": _GRAIN["group"], "program": _GRAIN["program"],
			"criteria": [{"field": "status", "operator": "changed to", "value": "Done"}],
			"actions": [{"action_type": "Create Note", "comment_mode": "Literal", "comment_text": "unreachable"}],
		})
		with self.assertRaises(frappe.exceptions.ValidationError):
			doc.insert(ignore_permissions=True)


if __name__ == "__main__":
	unittest.main()
