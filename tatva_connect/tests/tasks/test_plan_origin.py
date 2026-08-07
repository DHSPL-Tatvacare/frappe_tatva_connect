"""A task row is born an APPOINTMENT or a RECORD, and it never changes its mind.

The modal shows the scheduling half of a row iff someone actually made an appointment. That question is
answered ONCE, at insert, from the only evidence that is always true at that moment — whether the row
arrived carrying a due date. Deriving it at READ time from the current `due_date` is what these tests
exist to forbid: a rep clearing the date would silently turn a kept appointment into a bare record, and
the task they were sent to do would lose the reason it existed.

The stamp is an invariant, not an operator toggle, so it lives on the controller override
(`list_engine.columns.TatvaCRMTask`) beside the auth bindings, never in `doc_events` — there is no state
of this system in which a row may be born without knowing which half it is.
"""

import unittest

import frappe
from frappe.utils import add_days, now_datetime


class TestPlanOrigin(unittest.TestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		self.sp = f"plan_origin_{frappe.generate_hash(length=6)}"
		frappe.db.savepoint(self.sp)
		self.lead = frappe.get_doc({
			"doctype": "CRM Lead", "first_name": "Plan Origin Probe",
			"mobile_no": f"+9198124{int(frappe.generate_hash(length=8), 16) % 100000:05d}",
		}).insert(ignore_permissions=True)  # authz-ok: tier-c — test fixture, no gate under test

	def tearDown(self):
		frappe.db.rollback(save_point=self.sp)

	def _task(self, **kwargs):
		doc = frappe.get_doc({
			"doctype": "CRM Task",
			"title": "origin probe",
			"reference_doctype": "CRM Lead",
			"reference_docname": self.lead.name,
			**kwargs,
		})
		doc.insert(ignore_permissions=True)  # authz-ok: tier-c — test fixture, no gate under test
		return doc

	def test_a_row_born_with_a_due_date_is_an_appointment(self):
		"""New Task and the automation follow-up both schedule; both are appointments."""
		task = self._task(due_date=add_days(now_datetime(), 1))
		self.assertEqual(task.custom_is_planned, 1)

	def test_a_row_born_without_one_is_a_record(self):
		"""Log Activity records something already done — nothing scheduled it."""
		task = self._task()
		self.assertEqual(task.custom_is_planned, 0)

	def test_clearing_the_due_date_does_not_erase_the_appointment(self):
		"""THE point of storing it. Derived at read time, this row would forget it was ever scheduled."""
		task = self._task(due_date=add_days(now_datetime(), 1))
		task.due_date = None
		task.save(ignore_permissions=True)  # authz-ok: tier-c — test fixture, no gate under test
		self.assertEqual(
			frappe.db.get_value("CRM Task", task.name, "custom_is_planned"), 1,
			"a kept appointment lost its origin when its date was cleared",
		)

	def test_adding_a_due_date_later_does_not_invent_an_appointment(self):
		"""A logged record is finished. A date added afterwards is a correction, not a promise."""
		task = self._task()
		task.due_date = add_days(now_datetime(), 1)
		task.save(ignore_permissions=True)  # authz-ok: tier-c — test fixture, no gate under test
		self.assertEqual(
			frappe.db.get_value("CRM Task", task.name, "custom_is_planned"), 0,
			"a record grew an appointment it never had",
		)

	def test_the_stamp_is_not_the_callers_to_assert(self):
		"""A client could otherwise declare itself planned and get scheduling controls it never filled."""
		task = self._task(custom_is_planned=1)
		self.assertEqual(
			task.custom_is_planned, 0,
			"the caller's claim survived — the stamp must be derived from the due date, never accepted",
		)
