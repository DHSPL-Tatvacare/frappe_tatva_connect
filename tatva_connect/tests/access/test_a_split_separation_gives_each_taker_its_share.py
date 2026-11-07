# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A work row split across two takers hands each taker its own share, not the whole group to the last one."""
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from tatva_connect.access import separation
from tatva_connect.workflow_engine.tests.fixtures import GRAIN, make_lead

LEAVER, FIRST, SECOND = (f"zz-split-{who}@example.test" for who in ("leaver", "first", "second"))
PHONES = ("+916100093001", "+916100093002", "+916100093003", "+916100093004")


class TestASplitSeparationGivesEachTakerItsShare(IntegrationTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		for email in (LEAVER, FIRST, SECOND):
			frappe.get_doc({"doctype": "User", "email": email, "first_name": "Split", "send_welcome_email": 0,
			                "roles": [{"role": "Sales User"}]}).insert(ignore_permissions=True)
		for email in (FIRST, SECOND):
			frappe.get_doc({"doctype": "User Permission", "user": email, "allow": "CRM Vertical", "for_value": GRAIN["vertical"],
			                "applicable_for": "CRM Lead", "apply_to_all_doctypes": 0}).insert(ignore_permissions=True)
		self.leads = [make_lead(mobile_no=phone, lead_owner=LEAVER).name for phone in PHONES]

	def tearDown(self):
		frappe.db.rollback()

	def test_two_takers_each_get_half(self):
		doc = frappe.get_doc({"doctype": "Tatva User Separation", "user": LEAVER, "reassign_to": FIRST}).insert()
		row = next(r for r in doc.work if r.ref_doctype == "CRM Lead")
		doc.append("work", {"ref_doctype": "CRM Lead", "grain": row.grain, "vertical": row.vertical, "crm_group": row.crm_group,
		                    "program": row.program, "assign_to": SECOND})  # what the form's Split adds
		doc.save()
		with patch("frappe.enqueue"), patch("frappe.db.commit"):
			separation.run(doc.name)
			separation.work(doc.name)
		owners = frappe.get_all("CRM Lead", filters={"name": ["in", self.leads]}, pluck="lead_owner")
		self.assertEqual(sorted(owners), sorted([FIRST, FIRST, SECOND, SECOND]))
