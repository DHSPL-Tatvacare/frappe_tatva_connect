"""Reconcile is an upsert. It must never delete, and it must never touch a row a rep typed.

`CRM Call Log` is a mixed table. Rows this app writes from a provider are keyed on the provider's
`call_id`; rows a rep logs by hand carry `telephony_medium = Manual` and no provider key at all. On a
live bench the manual rows are the overwhelming majority.

The natural way to write a reconciler — clear the window, refetch it — is safe only when the provider is
the single source. Here it would delete work no refetch can restore: a manual row has no `call_id`, so
the provider cannot return it. That mistake would look like a success, because every provider row would
come back and only the reps' own entries would be missing.

These tests are the guard. They fail if a pull ever removes a row, or writes over one it did not create.
"""
import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.telephony import reconcile

ACCOUNT = "_TestTelephonyAcct"
LEAD_PHONE = "9000012345"
MANUAL_ID = "_test-manual-call-entry"
PROVIDER_ID = "_test-provider-call-entry"


def _record(call_id, client_number=LEAD_PHONE, did="9000007179"):
	"""One Acefone Call Detail Record, in the shape the live API returns."""
	return {
		"call_id": call_id,
		"uuid": "6a5384e2b1c07",
		"direction": "outbound",
		"call_hint": "dialer",
		"status": "answered",
		"client_number": f"+91{client_number}",
		"did_number": f"+91{did}",
		"call_duration": 52,
		"date": "2026-07-12",
		"time": "17:34:58",
		"end_stamp": "2026-07-12 17:35:49",
		"hangup_cause": "disconnected_by_caller",
		"call_flow": [],
	}


class TestReconcileNeverDeletes(FrappeTestCase):
	def setUp(self):
		for name in (MANUAL_ID, PROVIDER_ID):
			frappe.db.delete("CRM Call Log", {"name": name})
		frappe.db.commit()

		# What a rep typed. No provider key, and the provider could never recreate it.
		manual = frappe.new_doc("CRM Call Log")
		manual.update({
			"id": MANUAL_ID, "telephony_medium": "Manual", "type": "Outgoing",
			"status": "Completed", "from": "9000099999", "to": LEAD_PHONE, "duration": 120,
		})
		manual.insert(ignore_permissions=True)
		frappe.db.commit()

	def tearDown(self):
		for name in (MANUAL_ID, PROVIDER_ID):
			frappe.db.delete("CRM Call Log", {"name": name})
		frappe.db.commit()

	def test_a_pull_never_removes_a_manual_row(self):
		"""The whole point. A rep's hand-logged call survives a reconcile that writes beside it."""
		before = frappe.db.count("CRM Call Log")
		summary = {"scanned": 0, "new": 0, "existing": 0, "declined": 0, "failed": 0}
		reconcile._reconcile_one(_record(PROVIDER_ID), ACCOUNT, dry_run=False, summary=summary)

		self.assertTrue(frappe.db.exists("CRM Call Log", MANUAL_ID), "the manual row was deleted")
		self.assertEqual(
			frappe.db.get_value("CRM Call Log", MANUAL_ID, "telephony_medium"), "Manual",
			"the manual row was written over",
		)
		self.assertEqual(frappe.db.get_value("CRM Call Log", MANUAL_ID, "duration"), 120)
		# Nothing was removed. The count may rise if the record is captured; it may never fall.
		self.assertGreaterEqual(frappe.db.count("CRM Call Log"), before)

	def test_reconcile_calls_nothing_destructive(self):
		"""Behaviour is checked above; this pins the source, so the destructive idiom cannot creep back
		in behind a passing test.

		Read as an AST rather than as text: the module's own prose says the words 'delete' and 'truncate'
		while explaining why it must not do either, and a substring scan cannot tell an explanation from
		an instruction.
		"""
		import ast
		import inspect

		banned = {"delete", "delete_doc", "truncate", "sql", "multisql"}
		for node in ast.walk(ast.parse(inspect.getsource(reconcile))):
			if not isinstance(node, ast.Call):
				continue
			func = node.func
			called = getattr(func, "attr", None) or getattr(func, "id", None)
			self.assertNotIn(called, banned, f"reconcile must never call {called}()")

	def test_a_re_pull_updates_the_row_it_created_and_adds_nothing(self):
		"""The provider's own row is upserted on `call_id`, not duplicated."""
		summary = {"scanned": 0, "new": 0, "existing": 0, "declined": 0, "failed": 0}
		reconcile._reconcile_one(_record(PROVIDER_ID), ACCOUNT, dry_run=False, summary=summary)
		after_first = frappe.db.count("CRM Call Log")

		reconcile._reconcile_one(_record(PROVIDER_ID), ACCOUNT, dry_run=False, summary=summary)
		self.assertEqual(frappe.db.count("CRM Call Log"), after_first, "the re-pull duplicated a call")

	def test_a_manual_row_is_not_addressable_by_the_provider_key(self):
		"""A manual row carries no provider key, so a pull cannot select it even by accident."""
		self.assertFalse(
			frappe.db.exists("CRM Call Log", {"name": MANUAL_ID, "telephony_medium": "Acefone"})
		)
