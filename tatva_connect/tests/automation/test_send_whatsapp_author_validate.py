# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The author-time validator (plan section 2.2 / 3.3): `CRM Automation Rule.validate` blocks the save
of a Send WhatsApp action whose picked template does not belong to the rule's OWN grain-routed account,
whenever that grain is specific enough to pin a single account. A grain too partial to pin one account
is not an authoring error - the rule saves with a warning, and the send-time guard covers it per lead
(see test_send_whatsapp_routing_guard.py). Real Frappe engine as the oracle (S.6): a real rule
insert/save through the real controller, a real routing row, a real (mirrored) template.
"""
import hashlib
import unittest

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.tests.authz.grains import GRAINS, assert_masters_exist

_DT = "CRM Automation Rule"
_GRAIN = GRAINS[0]
_PREFIX = "AuthorValidate-"



def _channel_number(name):
	"""A distinct WABA number per account — the field is unique."""
	return f"9190{int(hashlib.md5(name.encode()).hexdigest(), 16) % 10**8:08d}"

def _make_wati_account(name):
	if frappe.db.exists("WhatsApp Account", name):
		frappe.delete_doc("WhatsApp Account", name, force=True, ignore_permissions=True)
	return frappe.get_doc({
		"doctype": "WhatsApp Account", "account_name": name, "status": "Active",
		"url": "https://live-mt-server.wati.io/000002", "token": "author-validate-test-token",
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

	def test_blank_template_raises_incomplete_action(self):
		"""R2 (post-audit remediation): a Send WhatsApp action with no template must throw at save,
		same as every sibling verb's incomplete-config check - a blank pick must never save cleanly and
		rely on the send-time guard alone to ever notice."""
		rule = frappe.get_doc({
			"doctype": _DT, "rule_name": f"{_PREFIX}blank-template", "enabled": 1,
			"on_doctype": "CRM Lead", "event": "Updated",
			"vertical": _GRAIN["vertical"], "group": _GRAIN["group"], "program": _GRAIN["program"],
			"actions": [{"action_type": "Send WhatsApp", "whatsapp_template": None}],
		})
		with self.assertRaises(frappe.ValidationError) as cm:
			rule.insert(ignore_permissions=True)
		self.assertIn("row 1", str(cm.exception))

	def test_multiple_send_whatsapp_actions_names_the_mismatched_row(self):
		"""Two Send WhatsApp actions in one rule: row 1 picks the matching account, row 2 does not -
		the throw must name row 2, proving the validator checks every Send WhatsApp action instead of
		stopping after the first."""
		rule = frappe.get_doc({
			"doctype": _DT, "rule_name": f"{_PREFIX}multi-mismatch", "enabled": 1,
			"on_doctype": "CRM Lead", "event": "Updated",
			"vertical": _GRAIN["vertical"], "group": _GRAIN["group"], "program": _GRAIN["program"],
			"actions": [
				{"action_type": "Send WhatsApp", "whatsapp_template": self.template_on_a},
				{"action_type": "Send WhatsApp", "whatsapp_template": self.template_on_b},
			],
		})
		with self.assertRaises(frappe.ValidationError) as cm:
			rule.insert(ignore_permissions=True)
		self.assertIn("row 2", str(cm.exception))

	def test_ambiguous_tie_degrades_to_warning_not_throw(self):
		"""R3 (post-audit remediation): an ambiguous-tie routing config must warn-and-allow, same as an
		unpinnable partial grain, instead of hard-blocking the save.

		The composite `::` primary key on CRM WhatsApp Routing (autoname
		`format:{vertical}::{psp_group}::{program}`, A.7) makes a genuine tie structurally unreachable
		through real routing rows for this table: two rows that could tie in specificity for the same
		grain would need the identical (vertical, group, program) triple - the identical primary key,
		which the DB already refuses to duplicate. This proves the degrade path by forcing the shared
		routing engine's own tie exception (`tatva_connect.routing.resolve_account_for_lead` raises
		`frappe.ValidationError` on a genuine tie) at the exact seam `_validate_send_whatsapp` calls
		through, rather than fabricating DB rows that cannot exist."""
		from tatva_connect.whatsapp import routing

		def _raise_ambiguous(vertical, group, program):
			frappe.throw("Ambiguous routing: two equally-specific rules point at different accounts for this lead.")

		orig = routing.resolve_account_for_grain
		routing.resolve_account_for_grain = _raise_ambiguous
		frappe.clear_messages()
		try:
			rule = frappe.get_doc({
				"doctype": _DT, "rule_name": f"{_PREFIX}ambiguous-tie", "enabled": 1,
				"on_doctype": "CRM Lead", "event": "Updated",
				"vertical": _GRAIN["vertical"], "group": _GRAIN["group"], "program": _GRAIN["program"],
				"actions": [{"action_type": "Send WhatsApp", "whatsapp_template": self.template_on_a}],
			}).insert(ignore_permissions=True)
		finally:
			routing.resolve_account_for_grain = orig
		self.assertTrue(rule.name)
		warnings = [m.get("message", "") for m in frappe.get_message_log()]
		self.assertTrue(
			any("cannot be verified" in w.lower() for w in warnings),
			f"expected an ambiguous tie to degrade to a 'cannot verify' warning, got: {warnings}",
		)

	def test_inactive_account_route_degrades_to_cannot_verify_warning(self):
		"""A routing row pointing at an Inactive account must never read as a resolved match:
		`routing._active_account_names()` excludes non-Active accounts (the per-account kill switch), so
		a deactivated tenant's grain no longer pins ANY account and the validator degrades to the same
		'cannot verify' warning as an unpinnable grain - fail-closed, never a false match on a stale
		row, never a hard block while the operator is mid-decommission on a tenant."""
		stale_grain = GRAINS[2]
		account_c = _make_wati_account("AuthorValidate-account-c")
		routing_c = _make_routing(stale_grain, account_c)
		template_on_c = _make_template("AuthorValidate-template-c", account_c)
		frappe.db.set_value("WhatsApp Account", account_c, "status", "Inactive")
		frappe.clear_messages()
		try:
			rule = frappe.get_doc({
				"doctype": _DT, "rule_name": f"{_PREFIX}inactive-account", "enabled": 1,
				"on_doctype": "CRM Lead", "event": "Updated",
				"vertical": stale_grain["vertical"], "group": stale_grain["group"], "program": stale_grain["program"],
				"actions": [{"action_type": "Send WhatsApp", "whatsapp_template": template_on_c}],
			}).insert(ignore_permissions=True)
			self.assertTrue(rule.name)
			warnings = [m.get("message", "") for m in frappe.get_message_log()]
			self.assertTrue(
				any("cannot be verified" in w.lower() for w in warnings),
				f"expected a 'cannot verify' warning for an inactive-account route, got: {warnings}",
			)
		finally:
			frappe.delete_doc("CRM WhatsApp Routing", routing_c, force=True, ignore_permissions=True)
			frappe.delete_doc("WhatsApp Templates", template_on_c, force=True, ignore_permissions=True)
			frappe.delete_doc("WhatsApp Account", account_c, force=True, ignore_permissions=True)


if __name__ == "__main__":
	unittest.main()
