# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""An intake form whose target is HD Ticket: a Guest submits the published form and a ticket is raised, its Contact found or created by mobile and linked."""
import json

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.website.doctype.web_form.web_form import accept

from tatva_connect.helpdesk import TICKET
from tatva_connect.intake import builder, intake, layers
from tatva_connect.taxonomy import grain

_INTAKE_SWITCH = "Lead::Enrolment::intake"
_FORM = "ZZ Ticket Intake Test"
_TYPE = "ZZ Intake Ticket Type"
_SOURCE = "ZZ Intake Support Form"
_PHONE = "+91 9000000071"
_EMAIL = "zz-intake-ticket@example.com"


class TestIntakeTicketTarget(FrappeTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		self._switch(1)
		if not frappe.db.exists("HD Ticket Type", _TYPE):
			frappe.get_doc({"doctype": "HD Ticket Type", "name": _TYPE}).insert(ignore_permissions=True)
		if not frappe.db.exists("HD Ticket Source", _SOURCE):
			frappe.get_doc({"doctype": "HD Ticket Source", "name": _SOURCE}).insert(ignore_permissions=True)
		if frappe.db.exists("CRM Intake Form", _FORM):
			frappe.delete_doc("CRM Intake Form", _FORM, force=True, ignore_permissions=True)
		frappe.get_doc({
			"doctype": "CRM Intake Form",
			"form_name": _FORM,
			"enabled": 1,
			"target": TICKET,
			"source": _SOURCE,
			"mappings": [
				{"source_field": "customer_name", "target_table": "Contact", "target_field": "first_name"},
				{"source_field": "mobile", "fieldtype": "Phone", "target_table": "Contact", "target_field": "mobile_no"},
				{"source_field": "email", "target_table": "Contact", "target_field": "email_id"},
				{"source_field": "ticket_type", "fieldtype": "Link", "options": "HD Ticket Type",
				 "target_table": TICKET, "target_field": "ticket_type"},
				{"source_field": "summary", "target_table": TICKET, "target_field": "subject"},
				{"source_field": "details", "fieldtype": "Text", "target_table": TICKET, "target_field": "description"},
			],
		}).insert(ignore_permissions=True)
		self.cfg = frappe.get_doc("CRM Intake Form", _FORM)
		builder.publish(self.cfg, True)
		self.dt = builder.doctype_name_for(self.cfg)

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
		for doctype, name in (("HD Ticket Type", _TYPE), ("HD Ticket Source", _SOURCE)):
			if frappe.db.exists(doctype, name):
				frappe.delete_doc(doctype, name, force=True, ignore_permissions=True)
		intake.bust_intake_doctype_cache()
		self._switch(0)

	def _switch(self, on):
		if frappe.db.exists("CRM Tatva Automation", _INTAKE_SWITCH):
			frappe.db.set_value("CRM Tatva Automation", _INTAKE_SWITCH, "enabled", on)

	def test_a_guest_submission_raises_a_ticket_linked_to_its_contact(self):
		web_form = builder.web_form_name_for(self.cfg)
		frappe.set_user("Guest")
		accept(web_form, json.dumps({
			"customer_name": "Asha Ticket", "mobile": _PHONE, "email": _EMAIL,
			"ticket_type": _TYPE, "summary": "Kit not delivered", "details": "Waiting a week.",
		}))
		self.assertEqual(frappe.session.user, "Guest", "the fold must hand the request back to its visitor")
		frappe.set_user("Administrator")

		name = frappe.db.get_value(TICKET, {"raised_by": _EMAIL}, "name")
		self.assertTrue(name, "the submission raised no ticket")
		ticket = frappe.get_doc(TICKET, name)
		self.assertEqual((ticket.subject, ticket.ticket_type), ("Kit not delivered", _TYPE))
		self.assertEqual(self.cfg.source_doctype, "HD Ticket Source", "Source must pick from the ticket's own source list")
		self.assertEqual(ticket.custom_ticket_source, _SOURCE, "the form's Source is stamped on the ticket")
		self.assertTrue(ticket.contact, "the ticket is not linked to a contact")
		contact = frappe.get_doc("Contact", ticket.contact)
		self.assertEqual(contact.first_name, "Asha Ticket")
		self.assertTrue(any(p.phone.endswith("9000000071") for p in contact.phone_nos))
		self.assertFalse(any(ticket.get(c) for c in grain.columns(TICKET) if c), "a ticket form stamps no grain")

		row = frappe.get_all(self.dt, fields=["ticket", "processed"])[0]
		self.assertEqual((row.ticket, row.processed), (name, 1))

	def test_every_target_the_form_offers_has_a_layer(self):
		offered = frappe.get_meta("CRM Intake Form").get_field("target").options.split("\n")
		self.assertEqual({o for o in offered if o}, {layers.LEAD, *layers.LAYERS})
		for target, layer in layers.LAYERS.items():
			for related, (link, fields) in layer.related.items():
				df = frappe.get_meta(target).get_field(link)
				self.assertEqual((df.fieldtype, df.options), ("Link", related), f"{target}.{link}")
				for field in fields:
					self.assertTrue(frappe.get_meta(related).has_field(field), f"{related}.{field}")
