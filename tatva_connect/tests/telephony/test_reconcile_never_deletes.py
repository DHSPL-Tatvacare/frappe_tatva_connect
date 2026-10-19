# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The records pull is an upsert: it never removes or writes over a call a rep logged by hand.

Run:
    bench --site dev.localhost run-tests --app tatva_connect --module tatva_connect.tests.telephony.test_reconcile_never_deletes
"""
import ast
import collections
import inspect

import frappe
from frappe.tests import IntegrationTestCase

from tatva_connect.telephony import reconcile
from tatva_connect.tests.telephony.fixtures import config

DID = "9000007179"


class TestReconcileNeverDeletes(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		config.ensure_account()
		cls.addClassCleanup(config.set_rules, config.current_rules())

	def setUp(self):
		config.set_rules([config.rule("Outbound", "Dialer")])
		config.clear_dids()
		config.map_did(DID)

	def tearDown(self):
		config.clear_dids()

	def test_a_pull_never_removes_or_overwrites_a_hand_logged_call(self):
		manual = frappe.get_doc({
			"doctype": "CRM Call Log", "id": frappe.generate_hash(length=16), "telephony_medium": "Manual", "type": "Outgoing",
			"status": "Completed", "from": DID, "to": "9000012345", "duration": 120,
		}).insert(ignore_permissions=True)  # authz-ok: tier-a — test fixture, runs as Administrator
		self.addCleanup(frappe.delete_doc, "CRM Call Log", manual.name, force=True, ignore_permissions=True)
		before = frappe.db.count("CRM Call Log")

		record = {**config.record("answered_click_to_call_with_seat"), "did_number": f"+91{DID}", "client_number": "+919000012345"}
		for _ in range(2):
			reconcile._reconcile_one(record, config.ACCOUNT, False, collections.defaultdict(int))

		self.assertEqual(frappe.db.get_value("CRM Call Log", manual.name, ["telephony_medium", "duration"]), ("Manual", 120))
		self.assertEqual(frappe.db.count("CRM Call Log"), before + 1)

	def test_reconcile_calls_nothing_destructive(self):
		"""Read as an AST, so the module's own prose about deleting cannot pass for a call."""
		banned = {"delete", "delete_doc", "truncate", "sql", "multisql"}
		for node in ast.walk(ast.parse(inspect.getsource(reconcile))):
			if isinstance(node, ast.Call):
				called = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
				self.assertNotIn(called, banned, f"reconcile must never call {called}()")
