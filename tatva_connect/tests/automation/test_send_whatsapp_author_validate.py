# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The author-time validator (plan section 2.2 / 3.3): `CRM Automation Rule.validate` blocks the save
of a Send WhatsApp action whose picked template does not belong to the rule's OWN grain-routed account,
whenever that grain is specific enough to pin a single account. A grain too partial to pin one account
is not an authoring error - the rule saves with a warning, and the send-time guard covers it per lead
(see test_send_whatsapp_routing_guard.py). Real Frappe engine as the oracle (S.6): a real rule
insert/save through the real controller, a real routing row, a real (mirrored) template.
"""
import unittest

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.tests.authz.grains import GRAINS, assert_masters_exist

_DT = "CRM Automation Rule"
_GRAIN = GRAINS[0]
_PREFIX = "AuthorValidate-"


def _make_wati_account(name):
	if frappe.db.exists("WhatsApp Account", name):
		frappe.delete_doc("WhatsApp Account", name, force=True, ignore_permissions=True)
	return frappe.get_doc({
		"doctype": "WhatsApp Account", "account_name": name, "status": "Active",
		"url": "https://live-mt-server.wati.io/000002", "token": "author-validate-test-token",
		"custom_provider": "WATI",
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


def _cleanup(prefix):
	frappe.db.delete("CRM Automation Action", {"parent": ("like", f"{prefix}%")})
	frappe.db.delete(_DT, {"rule_name": ("like", f"{prefix}%")})


class TestSendWhatsappAuthorValidate(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		assert_masters_exist()
		cls.account_a = _make_wati_account("AuthorValidate-account-a")
		cls.account_b = _make_wati_account("AuthorValidate-account-b")
		cls.routing_a = _make_routing(_GRAIN, cls.account_a)
		cls.template_on_a = _make_template("AuthorValidate-template-a", cls.account_a)
		cls.template_on_b = _make_template("AuthorValidate-template-b", cls.account_b)

	@classmethod
	def tearDownClass(cls):
		frappe.delete_doc("CRM WhatsApp Routing", cls.routing_a, force=True, ignore_permissions=True)
		frappe.delete_doc("WhatsApp Templates", cls.template_on_a, force=True, ignore_permissions=True)
		frappe.delete_doc("WhatsApp Templates", cls.template_on_b, force=True, ignore_permissions=True)
		frappe.delete_doc("WhatsApp Account", cls.account_a, force=True, ignore_permissions=True)
		frappe.delete_doc("WhatsApp Account", cls.account_b, force=True, ignore_permissions=True)

	def tearDown(self):
		_cleanup(_PREFIX)

	def test_full_grain_mismatch_blocks_save(self):
		"""The rule's grain (full triple) resolves to account A; the picked template belongs to
		account B - save must be blocked at once, in the desk form."""
		rule = frappe.get_doc({
			"doctype": _DT, "rule_name": f"{_PREFIX}mismatch", "enabled": 1,
			"on_doctype": "CRM Lead", "event": "Updated",
			"vertical": _GRAIN["vertical"], "group": _GRAIN["group"], "program": _GRAIN["program"],
			"actions": [{"action_type": "Send WhatsApp", "whatsapp_template": self.template_on_b}],
		})
		with self.assertRaises(frappe.ValidationError):
			rule.insert(ignore_permissions=True)

	def test_full_grain_match_saves_cleanly(self):
		"""The rule's grain resolves to account A; the picked template also belongs to account A -
		no mismatch, the save proceeds."""
		rule = frappe.get_doc({
			"doctype": _DT, "rule_name": f"{_PREFIX}match", "enabled": 1,
			"on_doctype": "CRM Lead", "event": "Updated",
			"vertical": _GRAIN["vertical"], "group": _GRAIN["group"], "program": _GRAIN["program"],
			"actions": [{"action_type": "Send WhatsApp", "whatsapp_template": self.template_on_a}],
		}).insert(ignore_permissions=True)
		self.assertTrue(rule.name)

	def test_partial_grain_cannot_pin_account_saves_with_warning(self):
		"""A grain too partial to pin a single account (Product Line only - the routing row also
		carries a Group, so no rule can match) is not an authoring error: the save proceeds, and a
		warning is recorded instead of a throw."""
		frappe.clear_messages()
		rule = frappe.get_doc({
			"doctype": _DT, "rule_name": f"{_PREFIX}partial", "enabled": 1,
			"on_doctype": "CRM Lead", "event": "Updated", "vertical": _GRAIN["vertical"],
			"actions": [{"action_type": "Send WhatsApp", "whatsapp_template": self.template_on_b}],
		}).insert(ignore_permissions=True)
		self.assertTrue(rule.name)
		warnings = [m.get("message", "") for m in frappe.get_message_log()]
		self.assertTrue(
			any("cannot be verified" in w.lower() for w in warnings),
			f"expected a 'cannot verify' warning in the message log, got: {warnings}",
		)


if __name__ == "__main__":
	unittest.main()
