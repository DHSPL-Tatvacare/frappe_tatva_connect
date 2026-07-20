# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The dry run is the live write path, rolled back — this pins the mechanism that makes that safe.

Validation must answer "what will happen when this file is imported", and the only honest way to answer
it is to do the write and undo it. `_process_bulk` already wraps each record in its own savepoint; the
dry-run closure opens a SECOND savepoint inside that one and rolls back to it before returning normally,
so the record counts as a success and its writes are gone.

That is only sound if an inner rollback leaves later writes intact — if it also discarded the result rows
`_write_results` inserts afterwards, a dry run would report nothing. Nothing in the app depends on that
being true until this closure exists, so it is proven here rather than assumed.
"""
import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.api._base import _process_bulk


class TestNestedSavepointRollback(FrappeTestCase):
	def test_an_inner_rollback_discards_only_its_own_writes(self):
		written = []

		def one(index, _item):
			savepoint = f"tc_dry_{index}"
			frappe.db.savepoint(savepoint)
			doc = frappe.get_doc({"doctype": "ToDo", "description": f"dry-run-probe-{index}"}).insert(
				ignore_permissions=True)  # authz-ok: tier-a — test fixture
			written.append(doc.name)
			frappe.db.rollback(save_point=savepoint)
			return {"index": index, "status": "success", "action": "validated", "data": {}}

		results, summary = _process_bulk([{}, {}], one)

		self.assertEqual(summary["succeeded"], 2, "a closure that rolls back its own savepoint must still succeed")
		self.assertEqual([r["action"] for r in results], ["validated", "validated"])
		for name in written:
			self.assertFalse(frappe.db.exists("ToDo", name), "the inner rollback did not discard its write")

	def test_a_write_after_the_inner_rollback_survives(self):
		"""_write_results runs after the closure; a dry run that reported nothing would be useless."""
		def one(index, _item):
			savepoint = f"tc_dry_{index}"
			frappe.db.savepoint(savepoint)
			frappe.get_doc({"doctype": "ToDo", "description": "dry-run-discarded"}).insert(
				ignore_permissions=True)  # authz-ok: tier-a — test fixture
			frappe.db.rollback(save_point=savepoint)
			return {"index": index, "status": "success", "action": "validated", "data": {}}

		_process_bulk([{}], one)
		marker = frappe.get_doc({"doctype": "ToDo", "description": "dry-run-result-row"}).insert(
			ignore_permissions=True)  # authz-ok: tier-a — test fixture
		self.assertTrue(frappe.db.exists("ToDo", marker.name), "a write after the inner rollback was lost")

	def test_a_failing_closure_is_still_reported_per_record(self):
		"""A refusal inside the savepoint must reach the caller as that record's failure, not a crash."""
		def one(index, _item):
			if index == 0:
				frappe.throw("refused on purpose")
			return {"index": index, "status": "success", "action": "validated", "data": {}}

		results, summary = _process_bulk([{}, {}], one)
		self.assertEqual((summary["succeeded"], summary["failed"]), (1, 1))
		self.assertEqual(results[0]["status"], "error")
