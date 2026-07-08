# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Task 15 sign-off — `automation.simulate.dry_run`: the read-only builder preview. Real Frappe engine
as the oracle throughout — a real rule, a real sample Lead, a real `dry_run()` call.

The crux: dry_run on a MATCHING sample resolves matched=True + a human effects preview (Create Task /
Update Field / Send WhatsApp all correctly resolved) while writing ZERO side effects — proven two ways:
(1) baseline-delta row counts (CRM Task / Comment / the target field / Run Log) before vs. after the
call, and (2) call-count spies on the REAL executor (`dispatcher.run_guards`/`run_effects`) and the REAL
send adapter (`sends.send_whatsapp`) — dry_run must invoke neither, zero times, not "zero net effect".
A non-matching sample previews matched=False with every action explicitly marked "would NOT run",
still zero side effects. The permission gate (no read on the sample doctype) raises, fail-closed.

Every fixture row is created via a real `frappe.get_doc(...).insert()` inside this FrappeTestCase (never
console/raw SQL — the hard safety constraint). Explicit narrow `tearDown{,Class}` cleanup, no
`super().setUpClass()` call — this app's "old" FrappeTestCase runner does not reliably auto-rollback
class-level fixtures (see test_two_lane.py / test_wait_resume.py, the established convention here).
"""
import unittest

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.automation import dispatcher, sends, simulate
from tatva_connect.automation.dispatcher import RUN_LOG
from tatva_connect.tests.authz.grains import GRAINS, assert_masters_exist
from tatva_connect.tests.automation import field_allowlist

_RULE_DT = "CRM Automation Rule"
_FIELD = field_allowlist.DOCTYPE
_GRAIN = GRAINS[0]


def _make_lead(**extra):
	payload = {
		"doctype": "CRM Lead", "first_name": "Simulate", "lead_name": "Simulate Probe", "status": "New",
		"custom_vertical": _GRAIN["vertical"], "custom_group": _GRAIN["group"],
		"custom_current_program": _GRAIN["program"],
	}
	payload.update(extra)
	return frappe.get_doc(payload).insert(ignore_permissions=True)


def _cleanup(prefix):
	frappe.db.delete("CRM Automation Action", {"parent": ("like", f"{prefix}%")})
	frappe.db.delete("CRM Automation Criterion", {"parent": ("like", f"{prefix}%")})
	frappe.db.delete(_RULE_DT, {"rule_name": ("like", f"{prefix}%")})
	frappe.db.delete(RUN_LOG, {"rule": ("like", f"{prefix}%")})


class TestSimulateDryRun(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		assert_masters_exist()
		cls.watch_row = field_allowlist.seed_watchable("CRM Lead", "status")
		cls.set_row = field_allowlist.seed_settable(
			"CRM Lead", "custom_last_report_date", _GRAIN["vertical"], _GRAIN["group"], _GRAIN["program"]
		)
		cls.tt_name = f"{_GRAIN['vertical']}::{_GRAIN['group']}::{_GRAIN['program']}::SimFollowUp"
		if not frappe.db.exists("CRM Task Type", cls.tt_name):
			frappe.get_doc({
				"doctype": "CRM Task Type", "type_name": "SimFollowUp",
				"vertical": _GRAIN["vertical"], "group": _GRAIN["group"], "program": _GRAIN["program"],
			}).insert(ignore_permissions=True)
		cls.rule = frappe.get_doc({
			"doctype": _RULE_DT, "rule_name": "Simulate-rule", "enabled": 1,
			"on_doctype": "CRM Lead", "event": "Updated",
			"vertical": _GRAIN["vertical"], "group": _GRAIN["group"], "program": _GRAIN["program"],
			"criteria": [{"field": "status", "operator": "is", "value": "Contacted"}],
			"actions": [
				{"action_type": "Create Task", "task_type": cls.tt_name, "due_mode": "From Context", "due_from": None},
				{"action_type": "Update Field", "target_doctype": "CRM Lead", "fieldname": "custom_last_report_date",
				 "value_mode": "Literal", "value": "2027-01-01"},
				{"action_type": "Send WhatsApp", "whatsapp_template": "SIM-TEMPLATE"},
			],
		}).insert(ignore_permissions=True, ignore_links=True)  # whatsapp_template is a free-form Link
		# probe here (this suite never sends) — no real WhatsApp Templates fixture needed.

	@classmethod
	def tearDownClass(cls):
		_cleanup("Simulate-")
		frappe.db.delete("CRM Task Type", {"name": cls.tt_name})
		frappe.db.delete(_FIELD, {"name": cls.watch_row})
		frappe.db.delete(_FIELD, {"name": cls.set_row})

	def setUp(self):
		frappe.set_user("Administrator")

	def tearDown(self):
		frappe.set_user("Administrator")

	def _snapshot(self, lead_name):
		return {
			"tasks": frappe.db.count("CRM Task", {"reference_doctype": "CRM Lead", "reference_docname": lead_name}),
			"comments": frappe.db.count("Comment", {"reference_doctype": "CRM Lead", "reference_name": lead_name}),
			"field": frappe.db.get_value("CRM Lead", lead_name, "custom_last_report_date"),
			"run_logs": frappe.db.count(RUN_LOG, {"rule": self.rule.name}),
		}

	# (a) the crux: a MATCHING sample resolves matched=True + correct previews, and touches nothing —
	# proven by both a baseline-delta snapshot and zero calls into the real executor/send adapter.
	def test_matching_sample_previews_true_and_writes_nothing(self):
		lead = _make_lead(status="Contacted", mobile_no="9999999999")
		before = self._snapshot(lead.name)

		orig_guards, orig_effects, orig_send = dispatcher.run_guards, dispatcher.run_effects, sends.send_whatsapp
		calls = {"guards": 0, "effects": 0, "send": 0}

		def _spy_guards(*a, **kw):
			calls["guards"] += 1
			return orig_guards(*a, **kw)

		def _spy_effects(*a, **kw):
			calls["effects"] += 1
			return orig_effects(*a, **kw)

		def _spy_send(*a, **kw):
			calls["send"] += 1
			return orig_send(*a, **kw)

		dispatcher.run_guards, dispatcher.run_effects, sends.send_whatsapp = _spy_guards, _spy_effects, _spy_send
		try:
			out = simulate.dry_run(self.rule.name, "CRM Lead", lead.name)
		finally:
			dispatcher.run_guards, dispatcher.run_effects, sends.send_whatsapp = orig_guards, orig_effects, orig_send

		self.assertTrue(out["matched"])
		self.assertEqual(out["guards"], [])
		wants = " ".join(e["would"] for e in out["effects"])
		self.assertIn("Create Task", wants)
		self.assertIn(self.tt_name, wants)
		self.assertIn("custom_last_report_date", wants)
		self.assertIn("2027-01-01", wants)
		self.assertIn("Send WhatsApp", wants)
		self.assertIn("SIM-TEMPLATE", wants)
		self.assertIn("suppressed unless sends enabled", wants)
		for entry in out["effects"]:
			self.assertNotIn("would NOT run", entry["would"], "a matched rule's actions must not carry the non-fire note")

		after = self._snapshot(lead.name)
		self.assertEqual(after, before, "dry_run on a matching sample wrote a side effect")
		self.assertEqual(calls, {"guards": 0, "effects": 0, "send": 0}, "dry_run invoked the real executor/adapter")

		frappe.db.delete("CRM Task", {"reference_doctype": "CRM Lead", "reference_docname": lead.name})
		frappe.db.delete("Comment", {"reference_doctype": "CRM Lead", "reference_name": lead.name})
		frappe.db.delete("CRM Lead", {"name": lead.name})

	# (b) a NON-matching sample previews matched=False, every action marked "would NOT run", still
	# zero side effects.
	def test_non_matching_sample_previews_false_and_writes_nothing(self):
		lead = _make_lead(status="New")  # rule wants status == Contacted
		before = self._snapshot(lead.name)

		out = simulate.dry_run(self.rule.name, "CRM Lead", lead.name)

		self.assertFalse(out["matched"])
		self.assertTrue(out["effects"], "the non-matching preview must still list the actions")
		for entry in out["effects"]:
			self.assertIn("would NOT run", entry["would"])

		after = self._snapshot(lead.name)
		self.assertEqual(after, before, "dry_run on a non-matching sample wrote a side effect")

		frappe.db.delete("CRM Lead", {"name": lead.name})

	# (c) fail-closed: a user without read on the SAMPLE doctype/record is refused outright.
	def test_permission_gate_denies_unauthorized_user(self):
		lead = _make_lead(status="Contacted")
		try:
			frappe.set_user("Guest")
			with self.assertRaises(frappe.PermissionError):
				simulate.dry_run(self.rule.name, "CRM Lead", lead.name)
		finally:
			frappe.set_user("Administrator")
			frappe.db.delete("CRM Lead", {"name": lead.name})


if __name__ == "__main__":
	unittest.main()
