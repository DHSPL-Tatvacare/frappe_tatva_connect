# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A task's id is an int and every column referencing it is varchar: each door that deletes a task, and the task's own on_trash, hand frappe the id as text, so each DELETE keyed on it uses an index instead of scanning.

Run:
    bench --site dev.localhost run-tests --app tatva_connect --module tatva_connect.tests.tasks.test_a_task_delete_uses_the_parent_index
"""
import random
from unittest.mock import patch

import frappe
from crm.api.doc import remove_linked_doc_reference
from frappe.tests import IntegrationTestCase
from frappe.utils import cstr

from tatva_connect import bulk_actions_run
from tatva_connect.utils import delete_in_pace


class TestATaskDeleteUsesTheParentIndex(IntegrationTestCase):
	def _lead(self):
		lead = frappe.get_doc({
			"doctype": "CRM Lead", "first_name": "Test", "mobile_no": f"+9190{random.randint(10_000_000, 99_999_999)}",
		}).insert(ignore_permissions=True)
		self.addCleanup(lambda: frappe.db.exists("CRM Lead", lead.name) and frappe.delete_doc("CRM Lead", lead.name, force=True, ignore_permissions=True))
		return lead.name

	def _task(self, lead=None):
		task = frappe.get_doc({
			"doctype": "CRM Task", "title": "ZZ delete-index probe", "status": "Todo",
			"reference_doctype": "CRM Lead" if lead else None, "reference_docname": lead,
		}).insert(ignore_permissions=True)
		self.assertIsInstance(task.name, int)
		frappe.db.commit()  # the doors under test commit per record
		return task.name

	def _assert_text(self, name, delete):
		"""Every DELETE keyed on this task — child tables and derived pointers alike — was bound with text."""
		with patch.object(frappe.db, "delete", wraps=frappe.db.delete) as spy:
			delete()
		bound = [v for c in spy.call_args_list if isinstance(c.args[1], dict)
		         for v in c.args[1].values() if cstr(v) == cstr(name)]
		self.assertTrue(bound)
		self.assertTrue(all(isinstance(v, str) for v in bound), bound)
		self.assertFalse(frappe.db.exists("CRM Task", name))

	def test_a_bulk_task_delete_binds_text(self):
		name = self._task()
		self._assert_text(name, lambda: delete_in_pace("CRM Task", [name], lambda n: frappe.delete_doc("CRM Task", n)))

	def test_deleting_a_lead_with_its_tasks_binds_text(self):
		lead = self._lead()
		name = self._task(lead)
		self._assert_text(name, lambda: bulk_actions_run.run_bulk_delete("CRM Lead", [lead], {"delete_linked": 1}))

	def test_deleting_a_selected_linked_task_binds_text(self):
		lead = self._lead()
		name = self._task(lead)
		self._assert_text(name, lambda: remove_linked_doc_reference([{"doctype": "CRM Task", "docname": name}], delete=True))
