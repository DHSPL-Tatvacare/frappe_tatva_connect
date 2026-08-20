# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A Custom Field fieldname must be a legal SQL identifier, refused on every ORM write — including the
ignore_validate insert that planted the poisoned CRM Dashboard field on UAT. Driven through a real
insert with flags.ignore_validate set, so a refactor back to a plain `validate` hook (which
ignore_validate skips) returns red.
"""
import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.access.custom_field_guard import guard_custom_field

_PAYLOAD = 'charts<img src="0" onerror=alert(document.cookie)>'


_PROBE = "custom_guard_probe"


class TestCustomFieldGuard(FrappeTestCase):
	def setUp(self):
		"""Clear a probe a previous run left behind, so the suite survives being interrupted.

		The probe below is inserted for real and removed by the test transaction's rollback — but a run
		killed between the insert and the rollback leaves the Custom Field standing, and every run after
		it then errors on "already exists" rather than testing the guard. That is the test failing to
		describe the code, which is the one thing a test may never do. The probe is a Section Break, so
		it owns no DB column and dropping the row drops nothing with it.
		"""
		super().setUp()
		stale = frappe.db.exists("Custom Field", {"dt": "ToDo", "fieldname": _PROBE})
		if stale:
			frappe.delete_doc("Custom Field", stale, ignore_permissions=True, force=True)
			frappe.db.commit()  # the leftover survived a rollback; removing it has to outlive one too

	def test_illegal_fieldname_rejected(self):
		with self.assertRaises(frappe.ValidationError):
			guard_custom_field(frappe._dict(fieldname=_PAYLOAD))

	def test_legal_fieldname_accepted(self):
		guard_custom_field(frappe._dict(fieldname="custom_patient_age"))  # no raise

	def test_ignore_validate_mutation_is_blocked(self):
		"""The exact UAT vector: a field is created legally, then its fieldname is MUTATED to the payload
		and saved with validate ignored — autoname does not re-run on update, so only before_validate
		stands between the poison and the DB. A plain `validate` hook (which ignore_validate skips) misses
		this. Section Break => no DB column, so the probe leaves no schema behind."""
		cf = frappe.get_doc({
			"doctype": "Custom Field", "dt": "ToDo", "fieldname": _PROBE,
			"label": "Probe", "fieldtype": "Section Break",
		}).insert(ignore_permissions=True)
		cf.fieldname = _PAYLOAD
		cf.flags.ignore_validate = True
		with self.assertRaises(frappe.ValidationError):
			cf.save(ignore_permissions=True)

	def test_guard_is_registered_always_on(self):
		from tatva_connect.automation.registry import AUTOMATIONS

		paths = {p for a in AUTOMATIONS for p in a.backs}
		self.assertIn("tatva_connect.access.custom_field_guard.guard_custom_field", paths)
