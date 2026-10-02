# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A real non-admin user resolves their own grain's field and never a foreign grain's, through both entitlement
sources: an Assignment Rule with the registry flag off, and native User Permission with it on."""

import frappe
from frappe.tests import IntegrationTestCase

from tatva_connect.access import entitlement
from tatva_connect.tests.api import partner_fixture

VERTICAL = "ZZ Real User Vertical"
HELD = "ZZ Real User Group Held"
FOREIGN = "ZZ Real User Group Foreign"
USER = "fieldgate.probe@example.test"
FLAG = "Access::Grain::registry"
_CACHES = ("tatva_connect:entitled_grains", "tatva_connect:internal_contract_ticks", "tatva_connect:internal_universal_fields",
           "tatva_connect:grain_registry_flag", "tatva_connect:grain_registry_rows")


class _FieldGate(IntegrationTestCase):
	flag = 0

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.get_doc({"doctype": "CRM Vertical", "vertical_name": VERTICAL}).insert(ignore_permissions=True)
		for group in (HELD, FOREIGN):
			frappe.get_doc({"doctype": "CRM Group", "group_name": group}).insert(ignore_permissions=True)
		cls.held, cls.foreign = partner_fixture.stock_catalog_rows(2)
		for group, field in ((HELD, cls.held), (FOREIGN, cls.foreign)):
			frappe.get_doc({"doctype": "CRM Lead API Mapping", "contract_name": f"ZZ Field Gate {group}", "enabled": 1,
			                "is_internal": 1, "vertical": VERTICAL, "crm_group": group,
			                "allowed_fields": [{"field": field}]}).insert(ignore_permissions=True)
		frappe.get_doc({"doctype": "User", "email": USER, "first_name": "Field Gate", "send_welcome_email": 0,
		                "roles": [{"role": "Sales User"}]}).insert(ignore_permissions=True)
		cls.grant()
		cls._flag_was = frappe.db.get_value("CRM Tatva Automation", FLAG, "enabled")
		cls._set_flag(cls.flag)

	@classmethod
	def tearDownClass(cls):
		cls._set_flag(cls._flag_was)
		frappe.cache.hdel("user_permissions", USER)
		super().tearDownClass()

	@classmethod
	def grant(cls):
		raise NotImplementedError

	@staticmethod
	def _set_flag(enabled):
		row = frappe.get_doc("CRM Tatva Automation", FLAG)
		row.enabled = enabled
		row.save(ignore_permissions=True)

	def setUp(self):
		for bucket in _CACHES:
			if hasattr(frappe.local, bucket):
				delattr(frappe.local, bucket)

	def resolved(self):
		with self.set_user(USER):
			return set(entitlement.resolve_fields({k: {"field_key": k} for k in (self.held, self.foreign)},
			                                      entitlement.entitled_grains()))

	def test_the_user_holds_exactly_their_grain(self):
		self.assertEqual(entitlement.entitled_grains(USER), {(VERTICAL, HELD, "")})

	def test_the_user_sees_their_grains_field(self):
		self.assertIn(self.held, self.resolved())

	def test_the_user_never_sees_a_foreign_grains_field(self):
		self.assertNotIn(self.foreign, self.resolved())


class TestThroughAnAssignmentRule(_FieldGate):
	flag = 0

	@classmethod
	def grant(cls):
		frappe.get_doc({"doctype": "Assignment Rule", "name": "zz-field-gate-rule", "document_type": "CRM Lead",
		                "assign_condition": "1", "rule": "Round Robin", "priority": 0, "disabled": 0,
		                "grain_vertical": VERTICAL, "grain_group": HELD, "users": [{"user": USER}],
		                "assignment_days": [{"day": "Monday"}]}).insert(ignore_permissions=True)


class TestThroughUserPermission(_FieldGate):
	flag = 1

	@classmethod
	def grant(cls):
		for allow, value in (("CRM Vertical", VERTICAL), ("CRM Group", HELD)):
			frappe.get_doc({"doctype": "User Permission", "user": USER, "allow": allow, "for_value": value,
			                "applicable_for": "CRM Lead", "apply_to_all_doctypes": 0}).insert(ignore_permissions=True)


del _FieldGate
