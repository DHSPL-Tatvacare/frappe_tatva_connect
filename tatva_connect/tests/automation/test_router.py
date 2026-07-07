# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The unified wildcard event router (Task 4) - the ONE trigger entry seam replacing the old
dispatcher.fire_rules / watch.fire_field_change_rules split.

Real Frappe engine as the oracle; frappe.flags.in_test makes `frappe.enqueue(..., now=True)` run
synchronously so on_created/on_updated -> live_doctypes() gating is observable directly."""
import unittest

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.automation import router
from tatva_connect.tests.authz.grains import GRAINS, assert_masters_exist

_DT = "CRM Automation Rule"
_GRAIN = GRAINS[0]


def _make_lead(**extra):
	payload = {
		"doctype": "CRM Lead", "first_name": "RouterProbe", "lead_name": "Router Probe", "status": "New",
		"custom_vertical": _GRAIN["vertical"], "custom_group": _GRAIN["group"],
		"custom_current_program": _GRAIN["program"],
	}
	payload.update(extra)
	return frappe.get_doc(payload).insert(ignore_permissions=True)


def _make_updated_rule(name, grain=None, enabled=1):
	g = grain or _GRAIN
	return frappe.get_doc({
		"doctype": _DT, "rule_name": name, "enabled": enabled,
		"on_doctype": "CRM Lead", "event": "Updated",
		"vertical": g["vertical"], "group": g["group"], "program": g["program"],
		"actions": [{
			"action_type": "Create Note", "comment_mode": "Literal", "comment_text": "router probe fired",
		}],
	}).insert(ignore_permissions=True)


class TestLiveDoctypes(FrappeTestCase):
	"""live_doctypes() - the cheap, self-healing guard set the wildcard router checks first."""

	@classmethod
	def setUpClass(cls):
		assert_masters_exist()

	def tearDown(self):
		# Child tables first (frappe.db.delete on the parent doesn't cascade) - a failed test that
		# skips the explicit rule.delete() must not leave an orphaned action row behind for the next run.
		frappe.db.delete("CRM Automation Action", {"parent": ("like", "RouterProbe-%")})
		frappe.db.delete(_DT, {"rule_name": ("like", "RouterProbe-%")})
		router.clear_live_doctypes_cache()

	# (c) live_doctypes() self-heals when a rule is enabled then disabled (cache cleared).
	def test_live_doctypes_self_heals_on_enable_disable(self):
		router.clear_live_doctypes_cache()
		self.assertNotIn("CRM Lead", router.live_doctypes())
		rule = _make_updated_rule("RouterProbe-live", enabled=1)
		self.assertIn("CRM Lead", router.live_doctypes())  # on_change busted the cache
		rule.enabled = 0
		rule.save(ignore_permissions=True)
		self.assertNotIn("CRM Lead", router.live_doctypes())  # on_change busted it again
		frappe.delete_doc("CRM Automation Rule", rule.name, force=True, ignore_permissions=True)


class TestRouterEntry(FrappeTestCase):
	"""on_created/on_updated - the wildcard entry guards, enqueue-after-commit."""

	@classmethod
	def setUpClass(cls):
		assert_masters_exist()
		frappe.db.set_value("CRM Tatva Automation", "Task::Automation::rules", "enabled", 1)

	@classmethod
	def tearDownClass(cls):
		frappe.db.delete("CRM Automation Action", {"parent": ("like", "RouterProbe-%")})
		frappe.db.delete(_DT, {"rule_name": ("like", "RouterProbe-%")})
		frappe.db.delete("CRM Lead", {"lead_name": "Router Probe"})

	def tearDown(self):
		router.clear_live_doctypes_cache()

	def _spy_enqueue(self):
		called = {"n": 0}
		orig = frappe.enqueue

		def _spy(method, **kw):
			if method == "tatva_connect.automation.router.run_for_event":
				called["n"] += 1
			return orig(method, **kw)

		return called, _spy, orig

	# (a) an enabled `On CRM Lead Updated` rule -> saving a matching Lead enqueues one dispatch.
	def test_updated_rule_enqueues_one_dispatch(self):
		rule = _make_updated_rule("RouterProbe-updated")
		called, spy, orig = self._spy_enqueue()
		frappe.flags.in_test = True
		frappe.enqueue = spy
		try:
			ld = _make_lead()
			ld.status = "Contacted"
			ld.save(ignore_permissions=True)
		finally:
			frappe.enqueue = orig
			frappe.flags.in_test = False
		self.assertEqual(called["n"], 1)
		frappe.delete_doc("CRM Automation Rule", rule.name, force=True, ignore_permissions=True)

	# (b) a doctype with no enabled rule enqueues nothing (the early-return via live_doctypes()).
	def test_no_rule_enqueues_nothing(self):
		router.clear_live_doctypes_cache()
		called, spy, orig = self._spy_enqueue()
		frappe.flags.in_test = True
		frappe.enqueue = spy
		try:
			ld = _make_lead()
			ld.status = "Contacted"
			ld.save(ignore_permissions=True)
		finally:
			frappe.enqueue = orig
			frappe.flags.in_test = False
		self.assertEqual(called["n"], 0)


if __name__ == "__main__":
	unittest.main()
