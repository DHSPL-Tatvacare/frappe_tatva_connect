# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Every app's invitation document asks the one platform rule: only an allowed domain is invited, and a blank list allows any."""
import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.access import invitations

_OUTSIDE = "someone@gmail.com"


def _set_domains(text):
	settings = frappe.get_single(invitations.SETTINGS)
	settings.invitation_domains = text
	settings.save(ignore_permissions=True)


class TestInvitationDomains(FrappeTestCase):
	def setUp(self):
		self.addCleanup(frappe.db.rollback)
		_set_domains("TatvaCare.in\n@example.org\n")

	def test_the_list_is_stored_the_way_the_rule_reads_it(self):
		self.assertEqual(frappe.db.get_single_value(invitations.SETTINGS, "invitation_domains"), "tatvacare.in\nexample.org")

	def test_an_entry_that_is_not_a_domain_is_refused(self):
		with self.assertRaises(frappe.ValidationError):
			_set_domains("tatvacare.in\nnot a domain")

	def test_an_allowed_domain_passes_in_any_case(self):
		invitations.assert_allowed("Rep@TATVACARE.in")

	def test_a_lookalike_domain_is_refused(self):
		with self.assertRaises(frappe.ValidationError):
			invitations.assert_allowed("rep@tatvacare.in.evil.com")

	def test_a_blank_list_allows_any_address(self):
		_set_domains("")
		invitations.assert_allowed(_OUTSIDE)

	def test_every_invitation_document_refuses_an_outside_address(self):
		documents = [
			{"doctype": "User Invitation", "email": _OUTSIDE, "app_name": "frappe", "redirect_to_path": "/app"},
			{"doctype": "CRM Invitation", "email": _OUTSIDE, "role": "Sales User"},
			{"doctype": "Insights User Invitation", "email": _OUTSIDE},
		]
		for values in documents:
			with self.subTest(doctype=values["doctype"]), self.assertRaises(frappe.ValidationError) as caught:
				frappe.get_doc(values).insert(ignore_permissions=True)
			self.assertIn("can only be sent to addresses on", str(caught.exception))
