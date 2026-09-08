# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The informed Send WhatsApp template picker (plan section 2.1 / 3.2): `templates.template_picker_query`
must describe each template with its account and the grains that route to it, so an author picks a
template with the account and grain in front of them. Real Frappe engine as the oracle (S.6): a real
account, real routing rows, real (mirrored) templates, and the real permission layer for the gate.
"""
import hashlib
import unittest

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.tests.authz.grains import GRAINS, assert_masters_exist
from tatva_connect.whatsapp import templates

_GRAIN_0 = GRAINS[0]
_GRAIN_1 = GRAINS[1]
_PROBE_USER = "template-picker-probe@example.com"



def _channel_number(name):
	"""A distinct WABA number per account — the field is unique."""
	return f"9190{int(hashlib.md5(name.encode()).hexdigest(), 16) % 10**8:08d}"

def _make_wati_account(name):
	if frappe.db.exists("WhatsApp Account", name):
		frappe.delete_doc("WhatsApp Account", name, force=True, ignore_permissions=True)
	return frappe.get_doc({
		"doctype": "WhatsApp Account", "account_name": name, "status": "Active",
		"url": "https://live-mt-server.wati.io/000003", "token": "template-picker-test-token",
		"custom_provider": "WATI", "custom_wati_channel_number": _channel_number(name),
	}).insert(ignore_permissions=True).name


def _make_template(template_name, account):
	"""`WhatsApp Templates` is WATI-mirrored and read-only - rows only ever land via `templates_sync`'s
	`db_insert`. Same seam as test_sends_gated.py."""
	full_name = f"{template_name}-en"
	if frappe.db.exists("WhatsApp Templates", full_name):
		frappe.delete_doc("WhatsApp Templates", full_name, force=True, ignore_permissions=True)
	doc = frappe.new_doc("WhatsApp Templates")
	doc.update({
		"template_name": template_name, "template": "<p>Hi</p>", "language_code": "en",
		"category": "UTILITY", "whatsapp_account": account, "actual_name": template_name,
		"status": "APPROVED",
	})
	doc.name = full_name
	doc.db_insert()
	return doc.name


def _make_routing(grain, account):
	key = f"{grain['vertical']}::{grain['group']}::{grain['program']}"
	if frappe.db.exists("CRM WhatsApp Routing", key):
		frappe.delete_doc("CRM WhatsApp Routing", key, force=True, ignore_permissions=True)
	return frappe.get_doc({
		"doctype": "CRM WhatsApp Routing", "vertical": grain["vertical"],
		"psp_group": grain["group"], "program": grain["program"], "whatsapp_account": account,
	}).insert(ignore_permissions=True).name


def _row_for(rows, template_name):
	"""The fixture's own row, or a failure that says what the picker really returned.

	`next(...)` alone raised StopIteration with no message once a real catalogue was synced onto the
	bench — 591 templates, and a page of 20 that no fixture appears on. The picker is a SEARCH, so the
	tests ask for their own row by name rather than hunting an unfiltered page.
	"""
	for row in rows:
		if row[0] == template_name:
			return row
	raise AssertionError(f"{template_name} is not in the picker's answer: {[r[0] for r in rows][:10]}")


class TestTemplatePickerQuery(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		assert_masters_exist()
		cls.account = _make_wati_account("Picker-routed-account")
		cls.unrouted_account = _make_wati_account("Picker-unrouted-account")
		cls.routing_0 = _make_routing(_GRAIN_0, cls.account)
		cls.routing_1 = _make_routing(_GRAIN_1, cls.account)
		cls.template_routed = _make_template("Picker-template-routed", cls.account)
		cls.template_unrouted = _make_template("Picker-template-unrouted", cls.unrouted_account)

	@classmethod
	def tearDownClass(cls):
		# test_recall_grain_summary_derives_from_routing_not_hardcoded deletes and re-seeds both rows
		# (same deterministic names, the grain key itself) in its own try/finally, so both exist here.
		frappe.delete_doc("CRM WhatsApp Routing", cls.routing_0, force=True, ignore_permissions=True)
		frappe.delete_doc("CRM WhatsApp Routing", cls.routing_1, force=True, ignore_permissions=True)
		frappe.delete_doc("WhatsApp Templates", cls.template_routed, force=True, ignore_permissions=True)
		frappe.delete_doc("WhatsApp Templates", cls.template_unrouted, force=True, ignore_permissions=True)
		frappe.delete_doc("WhatsApp Account", cls.account, force=True, ignore_permissions=True)
		frappe.delete_doc("WhatsApp Account", cls.unrouted_account, force=True, ignore_permissions=True)
		frappe.delete_doc("User", _PROBE_USER, force=True, ignore_permissions=True)

	def tearDown(self):
		frappe.set_user("Administrator")

	def _query(self, txt=""):
		return templates.template_picker_query("WhatsApp Templates", txt, "name", 0, 20, {})

	def test_one_line_per_template_with_account_and_joined_grains(self):
		rows = self._query("Picker-template-routed")
		row = _row_for(rows, self.template_routed)
		self.assertEqual(row[1], self.account)
		grains_in_row = {g.strip() for g in row[2].split(",")}
		self.assertEqual(
			grains_in_row,
			{
				f"{_GRAIN_0['vertical']}::{_GRAIN_0['group']}::{_GRAIN_0['program']}",
				f"{_GRAIN_1['vertical']}::{_GRAIN_1['group']}::{_GRAIN_1['program']}",
			},
		)

	def test_txt_narrows_to_matching_actual_name(self):
		rows = self._query("template-routed")
		names = {r[0] for r in rows}
		self.assertIn(self.template_routed, names)
		self.assertNotIn(self.template_unrouted, names)

	def test_template_on_unrouted_account_reads_no_grain_routed(self):
		rows = self._query("Picker-template-unrouted")
		row = _row_for(rows, self.template_unrouted)
		self.assertEqual(row[1], self.unrouted_account)
		self.assertEqual(row[2], "(no grain routed)")

	def test_permission_gate_blocks_a_reader_without_access(self):
		if not frappe.db.exists("User", _PROBE_USER):
			frappe.get_doc({
				"doctype": "User", "email": _PROBE_USER, "first_name": "Template Picker Probe",
				"send_welcome_email": 0, "roles": [{"role": "Sales User"}],
			}).insert(ignore_permissions=True)
		frappe.set_user(_PROBE_USER)
		with self.assertRaises(frappe.PermissionError):
			self._query()

	def test_recall_grain_summary_derives_from_routing_not_hardcoded(self):
		"""Planted-bad (S.6 recall): delete both routing rows for the account and confirm the grain
		summary for its template goes empty - proves the description is DERIVED from live routing
		rows at query time, not a cached or hardcoded string."""
		frappe.delete_doc("CRM WhatsApp Routing", self.routing_0, force=True, ignore_permissions=True)
		frappe.delete_doc("CRM WhatsApp Routing", self.routing_1, force=True, ignore_permissions=True)
		try:
			rows = self._query("Picker-template-routed")
			row = _row_for(rows, self.template_routed)
			self.assertEqual(row[2], "(no grain routed)")
		finally:
			# Autoname is the grain key itself, so re-seeding restores the SAME names tearDownClass
			# expects - no need to rebind cls.routing_0/routing_1.
			_make_routing(_GRAIN_0, self.account)
			_make_routing(_GRAIN_1, self.account)


if __name__ == "__main__":
	unittest.main()
