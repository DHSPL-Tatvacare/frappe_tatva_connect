import random
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from tatva_connect import bulk_actions_run


class TestExecutors(IntegrationTestCase):
	def _lead(self, **kwargs):
		# A real, valid, unique mobile number: frappe's phone validation rejects an opaque hash.
		mobile_no = kwargs.pop("mobile_no", None) or f"+9190{random.randint(10_000_000, 99_999_999)}"
		doc = frappe.get_doc({
			"doctype": "CRM Lead", "first_name": "Test", "mobile_no": mobile_no,
			**kwargs,
		}).insert(ignore_permissions=True)
		return doc.name

	def _cleanup_leads(self, names):
		# run_assign/run_clear_assignment now commit per successful row (see bulk_actions_run.py), so the
		# test framework's own rollback can't undo these inserts; delete for real, same convention as
		# test_run_bulk_edit_updates_the_field below.
		def _cleanup():
			for n in names:
				frappe.db.delete("ToDo", {"reference_type": "CRM Lead", "reference_name": n})
				if frappe.db.exists("CRM Lead", n):
					frappe.delete_doc("CRM Lead", n, force=True, ignore_permissions=True)
			frappe.db.commit()
		self.addCleanup(_cleanup)

	def test_run_assign_assigns_every_lead(self):
		names = [self._lead() for _ in range(3)]
		self._cleanup_leads(names)
		result = bulk_actions_run.run_assign("CRM Lead", names, {"assign_to": "Administrator"})
		self.assertEqual(result, {"total": 3, "succeeded": 3, "failed": 0, "failed_names": []})
		for name in names:
			self.assertTrue(frappe.db.exists("ToDo", {
				"reference_type": "CRM Lead", "reference_name": name,
				"allocated_to": "Administrator", "status": "Open",
			}))

	def test_run_assign_assigns_multiple_users_to_one_lead(self):
		second_user = "zz-bulk-assign@example.com"
		if not frappe.db.exists("User", second_user):
			frappe.get_doc({
				"doctype": "User", "email": second_user, "first_name": "ZZ Bulk Assign",
				"send_welcome_email": 0, "user_type": "System User",
			}).insert(ignore_permissions=True)
			def _cleanup_user():
				frappe.delete_doc("User", second_user, force=True, ignore_permissions=True)
				frappe.db.commit()
			self.addCleanup(_cleanup_user)

		name = self._lead()
		self._cleanup_leads([name])
		result = bulk_actions_run.run_assign("CRM Lead", [name], {"assign_to": ["Administrator", second_user]})
		self.assertEqual(result["succeeded"], 1)
		assignees = {a["owner"] for a in frappe.desk.form.assign_to.get({"doctype": "CRM Lead", "name": name})}
		self.assertEqual(assignees, {"Administrator", second_user})

	def test_run_assign_isolates_one_bad_row(self):
		names = [self._lead(), "CRM-LEAD-DOES-NOT-EXIST", self._lead()]
		self._cleanup_leads(names)
		result = bulk_actions_run.run_assign("CRM Lead", names, {"assign_to": "Administrator"})
		self.assertEqual(result["succeeded"], 2)
		self.assertEqual(result["failed_names"], ["CRM-LEAD-DOES-NOT-EXIST"])

	def test_run_assign_rolls_back_a_partially_assigned_row_on_failure(self):
		# assign_to.add() loops over its own assignee list with no savepoint: "Administrator" is
		# processed first and its ToDo insert succeeds, but the second, nonexistent assignee then
		# blows up on ToDo's own link validation (frappe.exceptions.LinkValidationError), so the
		# whole call raises with Administrator's insert still pending in the open transaction. Without
		# the frappe.db.rollback() in run_assign's except block, that pending ToDo would sit there
		# uncommitted and get swept into the NEXT successful docname's commit — landing this docname
		# in `failed_names` while it is, in the database, actually half-assigned. Asserting ZERO ToDo
		# rows survive (not one, not two) is what actually exercises the rollback rather than just
		# reading the code.
		name = self._lead()
		self._cleanup_leads([name])
		result = bulk_actions_run.run_assign(
			"CRM Lead", [name], {"assign_to": ["Administrator", "definitely-bogus-user@example.com"]}
		)
		self.assertEqual(result["failed_names"], [name])
		self.assertEqual(
			frappe.get_all("ToDo", filters={"reference_type": "CRM Lead", "reference_name": name}), []
		)

	def test_run_assign_sends_one_batch_notification_not_one_per_lead(self):
		second_user = "zz-bulk-notify@example.com"
		if not frappe.db.exists("User", second_user):
			frappe.get_doc({
				"doctype": "User", "email": second_user, "first_name": "ZZ Bulk Notify",
				"send_welcome_email": 0, "user_type": "System User",
			}).insert(ignore_permissions=True)
			def _cleanup_user():
				frappe.delete_doc("User", second_user, force=True, ignore_permissions=True)
				frappe.db.commit()
			self.addCleanup(_cleanup_user)

		names = [self._lead() for _ in range(3)]
		self._cleanup_leads(names)
		before = frappe.db.count("Notification Log", {"for_user": second_user})

		result = bulk_actions_run.run_assign("CRM Lead", names, {"assign_to": second_user})

		self.assertEqual(result["succeeded"], 3)
		after = frappe.db.count("Notification Log", {"for_user": second_user})
		self.assertEqual(after - before, 1)

	def test_run_clear_assignment_never_notifies(self):
		name = self._lead()
		self._cleanup_leads([name])
		frappe.desk.form.assign_to.add({"doctype": "CRM Lead", "name": name, "assign_to": ["Administrator"]})
		before = frappe.db.count("Notification Log", {"for_user": "Administrator"})

		bulk_actions_run.run_clear_assignment("CRM Lead", [name], {})

		after = frappe.db.count("Notification Log", {"for_user": "Administrator"})
		self.assertEqual(after, before)

	def test_run_clear_assignment_removes_open_todos(self):
		name = self._lead()
		self._cleanup_leads([name])
		frappe.desk.form.assign_to.add({"doctype": "CRM Lead", "name": name, "assign_to": ["Administrator"]})
		result = bulk_actions_run.run_clear_assignment("CRM Lead", [name], {})
		self.assertEqual(result["succeeded"], 1)
		self.assertFalse(frappe.db.exists("ToDo", {
			"reference_type": "CRM Lead", "reference_name": name, "status": "Open",
		}))

	def test_run_bulk_edit_updates_the_field(self):
		names = [self._lead() for _ in range(2)]
		# _bulk_action commits per row, so the test framework's rollback can't undo these inserts; delete for real.
		def _cleanup():
			for n in names:
				frappe.delete_doc("CRM Lead", n, force=True, ignore_permissions=True)
			frappe.db.commit()
		self.addCleanup(_cleanup)
		result = bulk_actions_run.run_bulk_edit("CRM Lead", names, {"field": "job_title", "value": "Updated"})
		self.assertEqual(result["succeeded"], 2)
		for name in names:
			self.assertEqual(frappe.db.get_value("CRM Lead", name, "job_title"), "Updated")

	def test_run_bulk_delete_removes_the_records(self):
		names = [self._lead() for _ in range(2)]
		result = bulk_actions_run.run_bulk_delete("CRM Lead", names, {})
		self.assertEqual(result["succeeded"], 2)
		for name in names:
			self.assertFalse(frappe.db.exists("CRM Lead", name))

	def test_run_bulk_delete_with_delete_linked_removes_the_linked_task(self):
		# CRM Task's reference_doctype/reference_docname dynamic-link pair is exactly what
		# get_dynamic_linked_docs (called inside get_linked_docs_of_document) walks to find
		# documents linked to a CRM Lead.
		name = self._lead()
		task = frappe.get_doc({
			"doctype": "CRM Task", "title": "Follow up", "status": "Backlog",
			"reference_doctype": "CRM Lead", "reference_docname": name,
		}).insert(ignore_permissions=True)
		result = bulk_actions_run.run_bulk_delete("CRM Lead", [name], {"delete_linked": True})
		self.assertEqual(result["succeeded"], 1)
		self.assertFalse(frappe.db.exists("CRM Lead", name))
		self.assertFalse(frappe.db.exists("CRM Task", task.name))

	def test_run_bulk_delete_isolates_a_cascade_failure(self):
		# SYNTHETIC failure, not a natural one. A genuine `frappe.LinkExistsError` raised by
		# `frappe.delete_doc` inside `remove_linked_doc_reference` can never actually reach this
		# executor's loop: `remove_linked_doc_reference`'s own try/except already catches
		# `frappe.ValidationError` (which `LinkExistsError` subclasses, confirmed in
		# frappe/exceptions.py) and continues silently. So there's no reachable real-world input
		# that makes the cascade raise out to us; a targeted mock is the only way to exercise the
		# isolation mechanism itself. This does not re-test `delete_bulk_docs`'s own cascade
		# behavior (already TatvaCare's tested code) — only that ONE bad docname's cascade failure
		# here doesn't abort the batch for the others.
		good_name = self._lead()
		bad_name = self._lead()
		task = frappe.get_doc({
			"doctype": "CRM Task", "title": "Follow up", "status": "Backlog",
			"reference_doctype": "CRM Lead", "reference_docname": bad_name,
		}).insert(ignore_permissions=True)

		def _cleanup():
			frappe.delete_doc("CRM Task", task.name, force=True, ignore_permissions=True)
			if frappe.db.exists("CRM Lead", bad_name):
				frappe.delete_doc("CRM Lead", bad_name, force=True, ignore_permissions=True)
			frappe.db.commit()
		self.addCleanup(_cleanup)

		def _boom(items, **kwargs):
			items = frappe.parse_json(items) if isinstance(items, str) else items
			if any(i.get("docname") == task.name for i in items):
				raise RuntimeError("synthetic cascade failure for isolation test")
			return "success"

		# good_name has no linked docs at all, so its cascade loop never calls
		# remove_linked_doc_reference in the first place — it's unaffected by the mock by
		# construction, exactly like a genuinely healthy row would be.
		with patch("crm.api.doc.remove_linked_doc_reference", side_effect=_boom):
			result = bulk_actions_run.run_bulk_delete(
				"CRM Lead", [good_name, bad_name], {"delete_linked": True}
			)

		self.assertEqual(result["succeeded"], 1)
		self.assertEqual(result["failed"], 1)
		self.assertEqual(result["failed_names"], [bad_name])
		self.assertFalse(frappe.db.exists("CRM Lead", good_name))
		self.assertTrue(frappe.db.exists("CRM Lead", bad_name))
