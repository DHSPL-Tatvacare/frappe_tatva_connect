# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Every app's invitation document asks the one platform rule.
Only an allowed domain is invited, never a blocked one, and a blank list allows any."""
import frappe
from frappe.tests import IntegrationTestCase

from tatva_connect.access import invitations

_OUTSIDE = "someone@gmail.com"


def _set_domains(allowed=(), blocked=()):
	settings = frappe.get_single(invitations.SETTINGS)
	settings.set(invitations.FIELD, [{"domain": d, "rule": "Allowed"} for d in allowed]
	             + [{"domain": d, "rule": "Blocked"} for d in blocked])
	settings.save(ignore_permissions=True)


class TestInvitationDomains(IntegrationTestCase):
	def setUp(self):
		self.addCleanup(frappe.db.rollback)
		self.addCleanup(frappe.clear_document_cache, invitations.SETTINGS, invitations.SETTINGS)
		_set_domains(["TatvaCare.in", "@example.org"])

	def test_the_list_is_stored_the_way_the_rule_reads_it(self):
		self.assertEqual(invitations.rules(), (["tatvacare.in", "example.org"], []))

	def test_an_entry_that_is_not_a_domain_is_refused(self):
		with self.assertRaises(frappe.ValidationError):
			_set_domains(["tatvacare.in", "not a domain"])

	def test_a_domain_listed_twice_is_refused(self):
		with self.assertRaises(frappe.ValidationError):
			_set_domains(["tatvacare.in"], blocked=["TatvaCare.in"])

	def test_an_allowed_domain_passes_in_any_case(self):
		invitations.assert_allowed("Rep@TATVACARE.in")

	def test_a_lookalike_domain_is_refused(self):
		with self.assertRaises(frappe.ValidationError):
			invitations.assert_allowed("rep@tatvacare.in.evil.com")

	def test_a_blank_list_allows_any_address(self):
		_set_domains()
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
			self.assertIn("is not an allowed invitation domain", str(caught.exception))

	def test_a_blocked_domain_is_refused_even_with_no_allowed_list(self):
		_set_domains(blocked=["@GMail.com"])
		self.assertEqual(invitations.rules(), ([], ["gmail.com"]))
		invitations.assert_allowed("rep@tatvacare.in")
		documents = [
			{"doctype": "User Invitation", "email": _OUTSIDE, "app_name": "frappe", "redirect_to_path": "/app"},
			{"doctype": "CRM Invitation", "email": _OUTSIDE, "role": "Sales User"},
			{"doctype": "Insights User Invitation", "email": _OUTSIDE},
		]
		for values in documents:
			with self.subTest(doctype=values["doctype"]), self.assertRaises(frappe.ValidationError) as caught:
				frappe.get_doc(values).insert(ignore_permissions=True)
			self.assertIn("gmail.com is a blocked invitation domain", str(caught.exception))
