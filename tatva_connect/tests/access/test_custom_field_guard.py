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


class TestCustomFieldGuard(FrappeTestCase):
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
			"doctype": "Custom Field", "dt": "ToDo", "fieldname": "custom_guard_probe",
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
