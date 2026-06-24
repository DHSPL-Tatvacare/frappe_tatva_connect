# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Fail-closed file-privacy gate (invariant 15).

`storage.file_events.apply_privacy_policy` runs on File.validate and must keep every
attachment PRIVATE unless its doctype is on the operator allowlist (CRM Azure Storage
Settings -> Public Attachment Doctypes) AND the Storage::File::privacy toggle is ON.
It must NEVER infer "public" from a doctype name (the removed *Settings suffix shortcut),
so a doctype the operator never listed always defaults private.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \
        --module tatva_connect.tests.test_file_privacy
"""
import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.storage import file_events

_TOGGLE = "Storage::File::privacy"          # CRM Tatva Automation row that gates public exceptions
_SETTINGS = "CRM Azure Storage Settings"
_LIST_FIELD = "public_attachment_doctypes"


class TestFilePrivacy(FrappeTestCase):
	def _decide(self, attached_to_doctype, toggle_on, listed_doctypes=""):
		frappe.db.set_value("CRM Tatva Automation", _TOGGLE, "enabled", 1 if toggle_on else 0)
		frappe.db.set_single_value(_SETTINGS, _LIST_FIELD, listed_doctypes)
		doc = frappe._dict(attached_to_doctype=attached_to_doctype, is_private=0)
		file_events.apply_privacy_policy(doc)
		return doc.is_private

	def test_unlisted_doctype_is_private_even_with_toggle_on(self):
		# A patient/lead doctype is not on the allowlist -> private, regardless of the toggle.
		self.assertEqual(self._decide("CRM Lead", toggle_on=True, listed_doctypes=""), 1)

	def test_settings_named_doctype_no_longer_auto_publics(self):
		# Regression guard for the removed `endswith("Settings")` shortcut: a *Settings doctype
		# that the operator did NOT list must stay private even with the toggle ON.
		self.assertEqual(self._decide("CRM WhatsApp Settings", toggle_on=True, listed_doctypes=""), 1)

	def test_listed_doctype_is_public_when_toggle_on(self):
		self.assertEqual(self._decide("CRM WhatsApp Settings", toggle_on=True,
									   listed_doctypes="CRM WhatsApp Settings"), 0)

	def test_listed_doctype_is_private_when_toggle_off(self):
		# Toggle OFF can only make a file MORE private — an allowlisted doctype is still private.
		self.assertEqual(self._decide("CRM WhatsApp Settings", toggle_on=False,
									   listed_doctypes="CRM WhatsApp Settings"), 1)

	def test_unattached_file_keeps_uploader_choice(self):
		# No attached_to_doctype -> the policy does not touch is_private (stays as uploaded).
		doc = frappe._dict(attached_to_doctype=None, is_private=0)
		file_events.apply_privacy_policy(doc)
		self.assertEqual(doc.is_private, 0)
