# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A lead's open tasks move to its new owner the moment it's reassigned — silently, no per-task notification."""
import random
from unittest.mock import patch

import frappe
from frappe.desk.form import assign_to
from frappe.tests import IntegrationTestCase

SECOND_USER = "zz-lead-handover@example.com"


class TestLeadReassignmentHandover(IntegrationTestCase):
	def setUp(self):
		self._tasks = []
		if not frappe.db.exists("User", SECOND_USER):
			frappe.get_doc({
				"doctype": "User", "email": SECOND_USER, "first_name": "ZZ Handover",
				"send_welcome_email": 0, "user_type": "System User",
			}).insert(ignore_permissions=True)
		mobile_no = f"+9190{random.randint(10_000_000, 99_999_999)}"
		self.lead = frappe.get_doc({
			"doctype": "CRM Lead", "first_name": "Test", "mobile_no": mobile_no,
		}).insert(ignore_permissions=True)
		assign_to.add({"doctype": "CRM Lead", "name": self.lead.name, "assign_to": ["Administrator"]})
		self.addCleanup(self._cleanup)

	def _cleanup(self):
		frappe.db.delete("ToDo", {"reference_type": "CRM Lead", "reference_name": self.lead.name})
		if self._tasks:
			frappe.db.delete("ToDo", {"reference_type": "CRM Task", "reference_name": ["in", self._tasks]})
		frappe.db.delete("CRM Task", {"reference_docname": self.lead.name})
		frappe.delete_doc("CRM Lead", self.lead.name, force=True, ignore_permissions=True)
		frappe.delete_doc("User", SECOND_USER, force=True, ignore_permissions=True)
		frappe.db.commit()

	def _task(self, status="Todo", assigned_to="Administrator"):
		task = frappe.get_doc({
			"doctype": "CRM Task", "title": "ZZ handover probe", "status": status,
			"assigned_to": assigned_to, "reference_doctype": "CRM Lead",
			"reference_docname": self.lead.name,
		}).insert(ignore_permissions=True)
		self._tasks.append(task.name)
		return task.name

	def test_the_holder_column_moves_and_no_todo_is_written(self):
		"""A task is held by its `assigned_to` column, so handover moves the column and writes no ToDo."""
		task = self._task(status="Todo", assigned_to="Administrator")

		assign_to.add({"doctype": "CRM Lead", "name": self.lead.name, "assign_to": [SECOND_USER]})

		self.assertEqual(frappe.db.get_value("CRM Task", task, "assigned_to"), SECOND_USER)
		self.assertFalse(frappe.db.exists("ToDo", {"reference_type": "CRM Task", "reference_name": task}))

	def test_closed_task_is_left_alone(self):
		task = self._task(status="Done", assigned_to="Administrator")

		assign_to.add({"doctype": "CRM Lead", "name": self.lead.name, "assign_to": [SECOND_USER]})

		self.assertEqual(frappe.db.get_value("CRM Task", task, "assigned_to"), "Administrator")

	def test_task_already_on_the_new_owner_is_untouched(self):
		task = self._task(status="Todo", assigned_to=SECOND_USER)
		before = frappe.db.count("ToDo", {"reference_type": "CRM Task", "reference_name": task})

		assign_to.add({"doctype": "CRM Lead", "name": self.lead.name, "assign_to": [SECOND_USER]})

		self.assertEqual(frappe.db.count("ToDo", {"reference_type": "CRM Task", "reference_name": task}), before)

	def test_handover_never_notifies(self):
		self._task(status="Todo", assigned_to="Administrator")
		self._task(status="Todo", assigned_to="Administrator")
		before = frappe.db.count("Notification Log", {"for_user": SECOND_USER})

		assign_to.add({"doctype": "CRM Lead", "name": self.lead.name, "assign_to": [SECOND_USER]})

		# Exactly one notification — the lead's own — even though two tasks moved.
		after = frappe.db.count("Notification Log", {"for_user": SECOND_USER})
		self.assertEqual(after - before, 1)

	def test_a_failing_task_does_not_block_the_others_or_the_real_assignment(self):
		good = self._task(status="Todo", assigned_to="Administrator")
		bad = self._task(status="Todo", assigned_to="Administrator")
		real_set_value = frappe.db.set_value

		def _flaky(doctype, name, *args, **kwargs):
			if doctype == "CRM Task" and name == bad:
				raise frappe.db.InternalError("simulated deadlock")
			return real_set_value(doctype, name, *args, **kwargs)

		with patch.object(frappe.db, "set_value", side_effect=_flaky):
			assign_to.add({"doctype": "CRM Lead", "name": self.lead.name, "assign_to": [SECOND_USER]})

		self.assertTrue(frappe.db.exists("ToDo", {
			"reference_type": "CRM Lead", "reference_name": self.lead.name,
			"allocated_to": SECOND_USER, "status": "Open",
		}))
		self.assertEqual(frappe.db.get_value("CRM Task", good, "assigned_to"), SECOND_USER)
