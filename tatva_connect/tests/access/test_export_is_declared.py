# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Export is exactly what the ledger declares: a right given with its roles rides only those readers.
The rebuild runs inside the test and is rolled back with it, so a migrate a previous run left behind proves nothing."""
import frappe
from frappe.tests import IntegrationTestCase

from tatva_connect.access import ledger, lockdown
from tatva_connect.tests.authz.base import mk_user


def scoped():
	"""The doctypes whose export the ledger names roles for, read from the declaration, never typed here."""
	return [dt for dt, extras in ledger.EXTRA_PTYPES.items() if isinstance(extras, dict) and frappe.db.exists("DocType", dt)]


def declared_export(doctype, role, read):
	"""THE answer the ledger gives: a config sheet, a bare right on every reader, or a right named with its roles."""
	extras = ledger.extra_ptypes_for(doctype)
	only = extras.get("export", ()) if isinstance(extras, dict) else (None if "export" in extras else ())
	return int(bool(read) and (doctype in lockdown.IMPORT_ON or only is None or role in only))


class TestExportIsDeclared(IntegrationTestCase):
	def setUp(self):
		for doctype in scoped():
			frappe.db.set_default(f"ledger_hash:{doctype}", "")  # force the rebuild these declarations get on migrate
		lockdown.apply()

	def test_every_role_exports_exactly_what_the_ledger_declares(self):
		for doctype in scoped():
			for role, perms in ledger.rows_for(doctype).items():
				live = frappe.db.get_value("Custom DocPerm", {"parent": doctype, "role": role, "permlevel": 0}, "export")
				self.assertEqual(live, declared_export(doctype, role, perms[0]), f"{role} export on {doctype}")

	def test_an_agent_manager_exports_a_ticket_and_an_agent_or_customer_does_not(self):
		cases = {ledger.AGENT_MANAGER: True, ledger.AGENT: False, ledger.HD_CUSTOMER: False}
		for role, allowed in cases.items():
			user = mk_user(f"zz.export.{role.lower().replace(' ', '')}@example.com", [role])
			self.assertEqual(frappe.has_permission("HD Ticket", "export", user=user), allowed, role)
