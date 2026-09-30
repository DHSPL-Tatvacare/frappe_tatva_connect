# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A file a Guest uploads on an intake form lands where that record's screen lists it — on the lead, on the ticket's first message — through the one shared attach step."""
import json

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.website.doctype.web_form.web_form import accept

from tatva_connect.automation import seed
from tatva_connect.helpdesk import TICKET
from tatva_connect.intake import builder, intake
from tatva_connect.partner_api import section_seed
from tatva_connect.whatsapp.phone import to_e164

_SWITCHES = ("Lead::Enrolment::intake", "Lead::CRM Lead::dedup")
_VERTICAL, _GROUP, _PROGRAM, _LEAD_SOURCE = "Goodflip-Care", "Anaya", "Nivolumab", "Enrolment Form"
_TYPE = "ZZ Attach Ticket Type"
_LEAD_FORM, _TICKET_FORM = "ZZ Attach Lead Form", "ZZ Attach Ticket Form"
_LEAD_PHONE, _TICKET_PHONE = "+91 9000000061", "+91 9000000062"
_EMAIL = "zz-attach-ticket@example.com"


class TestIntakeAttachments(FrappeTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		seed.sync_catalog()
		section_seed.ensure_rows()
		self._made = []
		for key in _SWITCHES:
			self._switch(key, 1)
		self._ensure("CRM Vertical", _VERTICAL, {"vertical_name": _VERTICAL})
		self._ensure("CRM Group", _GROUP, {"group_name": _GROUP})
		self._ensure("CRM Program", _PROGRAM, {"program_name": _PROGRAM})
		self._ensure("CRM Lead Source", _LEAD_SOURCE, {"source_name": _LEAD_SOURCE})
		self._ensure("HD Ticket Type", _TYPE, {"name": _TYPE})
		# The visitor uploads the file before submitting, so the Guest owns it — exactly as on the live form.
		frappe.set_user("Guest")
		upload = frappe.get_doc({
			"doctype": "File", "file_name": "zz-intake-proof.txt", "content": b"intake attach proof", "is_private": 1,
		}).insert(ignore_permissions=True)
		frappe.set_user("Administrator")
		self.file_url = upload.file_url

	def tearDown(self):
		frappe.set_user("Administrator")
		# Files first: the upload is bonded to its submission row, whose table goes with the form below.
		for file in frappe.get_all("File", filters={"file_url": self.file_url}, pluck="name"):
			frappe.delete_doc("File", file, force=True, ignore_permissions=True)
		for name in frappe.get_all("CRM Lead", filters={"mobile_no": to_e164(_LEAD_PHONE)}, pluck="name"):
			self._purge("CRM Lead", name)
		for name in frappe.get_all(TICKET, filters={"raised_by": _EMAIL}, pluck="name"):
			frappe.db.delete("Communication", {"reference_doctype": TICKET, "reference_name": name})
			self._purge(TICKET, name)
		for name in frappe.get_all("Contact Email", filters={"email_id": _EMAIL}, pluck="parent"):
			frappe.delete_doc("Contact", name, force=True, ignore_permissions=True)
		for form in (_LEAD_FORM, _TICKET_FORM):
			if not frappe.db.exists("CRM Intake Form", form):
				continue
			dt = builder.doctype_name_for(frappe.get_doc("CRM Intake Form", form))
			for wf in frappe.get_all("Web Form", filters={"doc_type": dt}, pluck="name"):
				frappe.delete_doc("Web Form", wf, force=True, ignore_permissions=True)
			if frappe.db.exists("DocType", dt):
				frappe.delete_doc("DocType", dt, force=True, ignore_permissions=True)
			frappe.delete_doc("CRM Intake Form", form, force=True, ignore_permissions=True)
		for doctype, name in reversed(self._made):
			if frappe.db.exists(doctype, name):
				frappe.delete_doc(doctype, name, force=True, ignore_permissions=True)
		intake.bust_intake_doctype_cache()
		for key in _SWITCHES:
			self._switch(key, 0)

	def _switch(self, key, on):
		if frappe.db.exists("CRM Tatva Automation", key):
			frappe.db.set_value("CRM Tatva Automation", key, "enabled", on)

	def _made_doc(self, doc):
		self._made.append((doc.doctype, doc.name))
		return doc

	def _ensure(self, doctype, name, values):
		if not frappe.db.exists(doctype, name):
			self._made_doc(frappe.get_doc({"doctype": doctype, **values}).insert(ignore_permissions=True))

	def _purge(self, doctype, name):
		frappe.delete_doc(doctype, name, force=True, ignore_permissions=True)

	def _publish(self, values, mappings):
		if frappe.db.exists("CRM Intake Form", values["form_name"]):
			frappe.delete_doc("CRM Intake Form", values["form_name"], force=True, ignore_permissions=True)
		frappe.get_doc({"doctype": "CRM Intake Form", "enabled": 1, "anonymous": 1, "mappings": mappings, **values}).insert(ignore_permissions=True)
		cfg = frappe.get_doc("CRM Intake Form", values["form_name"])
		builder.publish(cfg, True)
		return builder.doctype_name_for(cfg), builder.web_form_name_for(cfg)

	def _submit(self, web_form, doctype, answers):
		frappe.set_user("Guest")
		accept(web_form, json.dumps({"doctype": doctype, **answers}))
		frappe.set_user("Administrator")

	def _attached(self, doctype, name):
		return frappe.get_all("File", filters={"attached_to_doctype": doctype, "attached_to_name": name}, pluck="file_url")

	def test_a_lead_form_upload_lands_on_the_lead(self):
		dt, web_form = self._publish(
			{"form_name": _LEAD_FORM, "source": _LEAD_SOURCE, "custom_vertical": _VERTICAL,
			 "custom_group": _GROUP, "custom_current_program": _PROGRAM},
			[{"source_field": "phone", "fieldtype": "Phone", "target_table": "lead", "target_field": "mobile_no"},
			 {"source_field": "patient_name", "target_table": "lead", "target_field": "first_name"},
			 {"source_field": "prescription", "fieldtype": "Attach"}],
		)
		self._submit(web_form, dt, {"phone": _LEAD_PHONE, "patient_name": "Zz Attach Lead", "prescription": self.file_url})

		lead = frappe.db.get_value("CRM Lead", {"mobile_no": to_e164(_LEAD_PHONE)}, "name")
		self.assertTrue(lead, "the lead form created no lead")
		self.assertIn(self.file_url, self._attached("CRM Lead", lead), "the upload did not land on the lead")

	def test_a_ticket_form_upload_lands_on_the_ticket_s_first_message(self):
		dt, web_form = self._publish(
			{"form_name": _TICKET_FORM, "target": TICKET},
			[{"source_field": "customer_mobile", "fieldtype": "Phone", "target_table": "Contact", "target_field": "mobile_no"},
			 {"source_field": "customer_email", "target_table": "Contact", "target_field": "email_id"},
			 {"source_field": "ticket_type", "fieldtype": "Link", "options": "HD Ticket Type",
			  "target_table": TICKET, "target_field": "ticket_type"},
			 {"source_field": "subject", "target_table": TICKET, "target_field": "subject"},
			 {"source_field": "details", "fieldtype": "Text", "target_table": TICKET, "target_field": "description"},
			 {"source_field": "attachment", "fieldtype": "Attach"}],
		)
		self._submit(web_form, dt, {"customer_mobile": _TICKET_PHONE, "customer_email": _EMAIL,
		                            "ticket_type": _TYPE, "subject": "attach proof", "details": "see the file",
		                            "attachment": self.file_url})

		ticket = frappe.db.get_value(TICKET, {"raised_by": _EMAIL}, "name")
		self.assertTrue(ticket, "the ticket form created no ticket")
		message = frappe.get_all("Communication", filters={"reference_doctype": TICKET, "reference_name": ticket},
		                         fields=["name", "sender"], order_by="creation asc", limit=1)[0]
		self.assertIn(self.file_url, self._attached("Communication", message.name),
		              "the upload is not on the ticket's first message, where Helpdesk lists attachments")
		self.assertEqual(message.sender, _EMAIL, "the first message is authored by the customer, not a blank Guest")
