# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""What every intake form gets from the framework, whatever it creates: the shared scripts and styling, dropdowns that follow their target field's own link_filters, and the mobile-only rule — each driven through the public form a Guest fills."""
import json

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.website.doctype.web_form.web_form import accept

from tatva_connect.helpdesk import TICKET
from tatva_connect.intake import api, builder, intake

_SWITCH = "Lead::Enrolment::intake"
_FORM = "ZZ Framework Form"
_TYPE_A, _TYPE_B = "ZZ Framework Type A", "ZZ Framework Type B"
_SUB_A, _SUB_A_OFF, _SUB_B = "ZZ Framework Sub A", "ZZ Framework Sub A Retired", "ZZ Framework Sub B"
_EMAIL = "zz-framework@example.com"
_OPERATOR_SCRIPT = "// zz operator script"
_OPERATOR_CSS = "/* zz operator css */"


class TestIntakeFramework(FrappeTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		self._switch(1)
		self._made = []
		for name in (_TYPE_A, _TYPE_B):
			self._ensure({"doctype": "HD Ticket Type", "name": name})
		for name, ticket_type, disabled in ((_SUB_A, _TYPE_A, 0), (_SUB_A_OFF, _TYPE_A, 1), (_SUB_B, _TYPE_B, 0)):
			self._ensure({"doctype": "HD Ticket Sub Type", "name": name, "ticket_type": ticket_type, "disabled": disabled})
		if frappe.db.exists("CRM Intake Form", _FORM):
			frappe.delete_doc("CRM Intake Form", _FORM, force=True, ignore_permissions=True)
		frappe.get_doc({
			"doctype": "CRM Intake Form", "form_name": _FORM, "enabled": 1, "anonymous": 1, "target": TICKET,
			"client_script": _OPERATOR_SCRIPT, "custom_css": _OPERATOR_CSS,
			"mappings": [
				{"source_field": "mobile", "fieldtype": "Phone", "mobile_only": 1, "target_table": "Contact", "target_field": "mobile_no"},
				{"source_field": "email", "target_table": "Contact", "target_field": "email_id"},
				{"source_field": "ticket_type", "fieldtype": "Link", "options": "HD Ticket Type", "target_table": TICKET, "target_field": "ticket_type"},
				{"source_field": "sub_type", "fieldtype": "Link", "options": "HD Ticket Sub Type", "target_table": TICKET, "target_field": "custom_ticket_sub_type"},
				{"source_field": "subject", "target_table": TICKET, "target_field": "subject"},
				{"source_field": "sub_pick", "fieldtype": "Link", "options": "HD Ticket Sub Type", "depends_on_question": "ticket_type"},
			],
		}).insert(ignore_permissions=True)
		self.cfg = frappe.get_doc("CRM Intake Form", _FORM)
		builder.publish(self.cfg, True)
		self.dt, self.web_form = builder.doctype_name_for(self.cfg), builder.web_form_name_for(self.cfg)

	def tearDown(self):
		frappe.set_user("Administrator")
		for name in frappe.get_all(TICKET, filters={"raised_by": _EMAIL}, pluck="name"):
			frappe.db.delete("Communication", {"reference_doctype": TICKET, "reference_name": name})
			frappe.delete_doc(TICKET, name, force=True, ignore_permissions=True)
		for name in frappe.get_all("Contact Email", filters={"email_id": _EMAIL}, pluck="parent"):
			frappe.delete_doc("Contact", name, force=True, ignore_permissions=True)
		for wf in frappe.get_all("Web Form", filters={"doc_type": self.dt}, pluck="name"):
			frappe.delete_doc("Web Form", wf, force=True, ignore_permissions=True)
		if frappe.db.exists("DocType", self.dt):
			frappe.delete_doc("DocType", self.dt, force=True, ignore_permissions=True)
		frappe.delete_doc("CRM Intake Form", _FORM, force=True, ignore_permissions=True)
		for doctype, name in reversed(self._made):
			if frappe.db.exists(doctype, name):
				frappe.delete_doc(doctype, name, force=True, ignore_permissions=True)
		intake.bust_intake_doctype_cache()
		self._switch(0)

	def _switch(self, on):
		if frappe.db.exists("CRM Tatva Automation", _SWITCH):
			frappe.db.set_value("CRM Tatva Automation", _SWITCH, "enabled", on)

	def _ensure(self, values):
		if not frappe.db.exists(values["doctype"], values["name"]):
			doc = frappe.get_doc(values).insert(ignore_permissions=True)
			self._made.append((doc.doctype, doc.name))

	def _submit(self, **answers):
		frappe.set_user("Guest")
		try:
			accept(self.web_form, json.dumps({"doctype": self.dt, "email": _EMAIL, "subject": "framework proof", **answers}))
		finally:
			frappe.set_user("Administrator")

	def test_sub_type_follows_ticket_type_on_the_page_and_on_submit(self):
		rule = frappe.get_meta(self.dt).get_field("sub_type").link_filters
		self.assertIn("eval:doc.ticket_type", rule, "the question inherits its target field's rule, rewritten to this form's question")
		self.assertIn("link_options", frappe.db.get_value("Web Form", self.web_form, "client_script"))

		frappe.set_user("Guest")
		offered = api.link_options(self.web_form, "sub_type", {"ticket_type": _TYPE_A})
		frappe.set_user("Administrator")
		self.assertIn(_SUB_A, offered)
		self.assertNotIn(_SUB_B, offered, "a sub type of another type is offered")
		self.assertNotIn(_SUB_A_OFF, offered, "a disabled sub type is offered")

		with self.assertRaises(frappe.ValidationError):
			self._submit(mobile="+91-9000000081", ticket_type=_TYPE_A, sub_type=_SUB_B)
		self._submit(mobile="+91-9000000081", ticket_type=_TYPE_A, sub_type=_SUB_A)
		self.assertEqual(frappe.db.get_value(TICKET, {"raised_by": _EMAIL}, "custom_ticket_sub_type"), _SUB_A)

	def test_a_dependency_admits_its_parent_and_ungated_rows_and_nothing_else(self):
		rules = [["HD Ticket Sub Type", "ticket_type", "=", "eval:doc.ticket_type"]]
		self.assertEqual(intake.resolve_link_filters(rules, {"ticket_type": _TYPE_A}),
		                 [["HD Ticket Sub Type", "ticket_type", "in", [_TYPE_A, ""]]], "a row naming no parent must pass every parent")
		self.assertEqual(intake.resolve_link_filters(rules, {}), [], "an unanswered parent narrows nothing yet")
		offered = frappe.get_all("HD Ticket Sub Type", filters=intake.resolve_link_filters(rules, {"ticket_type": _TYPE_A}), pluck="name")
		self.assertIn(_SUB_A, offered)
		self.assertNotIn(_SUB_B, offered, "a row naming another parent is never offered, whatever it is called")

	def test_a_question_s_own_depends_on_narrows_its_list(self):
		frappe.set_user("Guest")
		offered = api.link_options(self.web_form, "sub_pick", {"ticket_type": _TYPE_B})
		frappe.set_user("Administrator")
		self.assertEqual(offered, [_SUB_B], "Depends On, matched by the list's own link to Ticket Type")

	def test_the_person_is_found_by_email_or_phone_and_their_contact_grows(self):
		known = frappe.get_doc({"doctype": "Contact", "first_name": "Zz Known", "email_ids": [{"email_id": _EMAIL, "is_primary": 1}]}).insert(ignore_permissions=True)
		self._submit(mobile="+91-9000000081", ticket_type=_TYPE_A)
		ticket = frappe.get_doc(TICKET, {"raised_by": _EMAIL})
		self.assertEqual(ticket.contact, known.name, "the email found the existing contact")
		self.assertIn("9000000081", " ".join(p.phone for p in frappe.get_doc("Contact", known.name).phone_nos), "the contact gained the phone")

		frappe.db.delete("Communication", {"reference_doctype": TICKET, "reference_name": ticket.name})
		frappe.delete_doc(TICKET, ticket.name, force=True, ignore_permissions=True)
		frappe.set_user("Guest")
		accept(self.web_form, json.dumps({"doctype": self.dt, "subject": "framework proof", "mobile": "+91-9000000081", "ticket_type": _TYPE_A}))
		frappe.set_user("Administrator")
		ticket = frappe.get_doc(TICKET, {"contact": known.name})
		self.assertEqual(ticket.raised_by, _EMAIL, "no email typed: the contact's own email, never the session user")

	def test_a_mobile_only_question_refuses_a_landline(self):
		with self.assertRaises(frappe.ValidationError):
			self._submit(mobile="+91-1234567890", ticket_type=_TYPE_A)
		self.assertFalse(frappe.db.exists(TICKET, {"raised_by": _EMAIL}), "a refused answer still raised a ticket")
		self._submit(mobile="+91-9000000081", ticket_type=_TYPE_A)
		self.assertTrue(frappe.db.exists(TICKET, {"raised_by": _EMAIL}))

	def test_every_form_carries_the_framework_after_the_operator_s_own(self):
		script, css, intro = frappe.db.get_value("Web Form", self.web_form, ["client_script", "custom_css", "introduction_text"])
		self.assertTrue(script.startswith(_OPERATOR_SCRIPT), "the operator's own script comes first, untouched")
		self.assertIn("relabelAttachments", script)
		self.assertIn("INVISIBLE", script, "the phone cleaner is missing")
		self.assertIn("--input-height", css, "the base styling is missing")
		self.assertTrue(css.rstrip().endswith(_OPERATOR_CSS), "the operator's own CSS comes last, so it wins")
		self.assertNotIn(".web-form-title", css, "frappe's title is hidden on a form that paints no header of its own")

		self.cfg.logo = "/assets/tatva_connect/images/tatva-connect.png"
		self.cfg.save(ignore_permissions=True)
		css, intro = frappe.db.get_value("Web Form", self.web_form, ["custom_css", "introduction_text"])
		self.assertIn(".web-form-title", css, "with a logo, frappe's prefixed title must be hidden")
		self.assertIn("tatva-connect.png", intro)
		self.assertIn(_FORM, intro, "the header names the form")
