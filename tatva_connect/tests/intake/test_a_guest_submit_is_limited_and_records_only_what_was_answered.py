# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A guest's submit is limited where frappe limits a call, and a dropdown starts unanswered.

Both run through frappe's own dispatcher (`frappe.handler.execute_cmd`, the call `api.handle` makes for
/api/method/<path>) on a real werkzeug POST — the path the web form page takes. The submit limits used to
sit in before_request, where /api/method/<path> has no cmd yet, so they never ran for a real visitor.
"""
import json

import frappe
from frappe.app import make_form_dict
from frappe.handler import execute_cmd
from frappe.tests.utils import FrappeTestCase
from werkzeug.test import EnvironBuilder
from werkzeug.wrappers import Request

from tatva_connect.automation import seed
from tatva_connect.intake import builder, guards, intake
from tatva_connect.partner_api import section_seed
from tatva_connect.whatsapp.phone import to_e164

_ACCEPT = guards._ACCEPT_CMD
_SWITCHES = ("Lead::Enrolment::intake", "Lead::CRM Lead::dedup", guards._RATE_SWITCH)
_VERTICAL, _GROUP, _PROGRAM, _SOURCE = "Goodflip-Care", "Anaya", "Nivolumab", "Enrolment Form"
_FORM, _PHONE, _IP = "ZZ Guest Submit Form", "+91 9000000071", "203.0.113.71"
_CAPS = ("ip_per_hour", "phone_per_day")


class TestAGuestSubmit(FrappeTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		seed.sync_catalog()
		section_seed.ensure_rows()
		self._made = []
		self._switches = {k: frappe.db.get_value("CRM Tatva Automation", k, "enabled") for k in _SWITCHES}
		self._caps = {k: frappe.db.get_single_value("CRM Intake Settings", k) for k in _CAPS}
		for key in _SWITCHES:
			frappe.db.set_value("CRM Tatva Automation", key, "enabled", 1)
		for doctype, name, values in (
			("CRM Vertical", _VERTICAL, {"vertical_name": _VERTICAL}), ("CRM Group", _GROUP, {"group_name": _GROUP}),
			("CRM Program", _PROGRAM, {"program_name": _PROGRAM}), ("CRM Lead Source", _SOURCE, {"source_name": _SOURCE}),
		):
			if not frappe.db.exists(doctype, name):
				self._made.append((doctype, frappe.get_doc({"doctype": doctype, **values}).insert(ignore_permissions=True).name))
		self._clear_counters()
		self.sink, self.web_form = self._publish()

	def tearDown(self):
		frappe.set_user("Administrator")
		for name in frappe.get_all("CRM Lead", filters={"mobile_no": to_e164(_PHONE)}, pluck="name"):
			frappe.delete_doc("CRM Lead", name, force=True, ignore_permissions=True)
		cfg = frappe.get_doc("CRM Intake Form", _FORM)
		for wf in frappe.get_all("Web Form", filters={"doc_type": self.sink}, pluck="name"):
			frappe.delete_doc("Web Form", wf, force=True, ignore_permissions=True)
		frappe.delete_doc("DocType", builder.doctype_name_for(cfg), force=True, ignore_permissions=True)
		frappe.delete_doc("CRM Intake Form", _FORM, force=True, ignore_permissions=True)
		for doctype, name in reversed(self._made):
			frappe.delete_doc(doctype, name, force=True, ignore_permissions=True)
		for key, on in self._switches.items():
			frappe.db.set_value("CRM Tatva Automation", key, "enabled", on or 0)
		for key, value in self._caps.items():
			frappe.db.set_single_value("CRM Intake Settings", key, value)
		self._clear_counters()
		intake.bust_intake_doctype_cache()

	def _clear_counters(self):
		for pattern in (f"*intake-rl:*{_IP}*", f"*intake-rl:phone:{to_e164(_PHONE)}*", f"*rl:{_ACCEPT}:{_IP}*"):
			for key in frappe.cache.scan_iter(match=pattern):
				frappe.cache.delete(key)

	def _publish(self):
		if frappe.db.exists("CRM Intake Form", _FORM):
			frappe.delete_doc("CRM Intake Form", _FORM, force=True, ignore_permissions=True)
		frappe.get_doc({
			"doctype": "CRM Intake Form", "form_name": _FORM, "enabled": 1, "anonymous": 1, "source": _SOURCE,
			"custom_vertical": _VERTICAL, "custom_group": _GROUP, "custom_current_program": _PROGRAM,
			"mappings": [
				{"source_field": "phone", "fieldtype": "Phone", "target_table": "lead", "target_field": "mobile_no"},
				{"source_field": "patient_name", "target_table": "lead", "target_field": "first_name"},
				{"source_field": "dosage", "fieldtype": "Select", "options": "\n40 mg\n100 mg"},
				{"source_field": "prescription", "fieldtype": "Attach"},
			],
		}).insert(ignore_permissions=True)
		cfg = frappe.get_doc("CRM Intake Form", _FORM)
		builder.publish(cfg, True)
		intake.bust_intake_doctype_cache()
		return builder.doctype_name_for(cfg), builder.web_form_name_for(cfg)

	def _submit(self, **answers):
		"""One guest POST to /api/method/<accept>, dispatched the way `api.handle` dispatches it."""
		data = json.dumps({"doctype": self.sink, "patient_name": "Zz Guest Submit", "phone": _PHONE, **answers})
		request = Request(EnvironBuilder(path=f"/api/method/{_ACCEPT}", method="POST",
		                                 data={"web_form": self.web_form, "data": data}).get_environ())
		frappe.set_user("Guest")  # first: set_user resets form_dict, as the session is settled before the body is read
		frappe.local.request, frappe.local.request_ip = request, _IP
		make_form_dict(request)
		frappe.form_dict.cmd = _ACCEPT  # what api/v1.handle sets, AFTER before_request has run
		try:
			return execute_cmd(_ACCEPT)
		finally:
			frappe.set_user("Administrator")

	def test_the_submit_limits_run_on_the_call_a_visitor_makes(self):
		frappe.db.set_single_value("CRM Intake Settings", "phone_per_day", 1)
		self._submit()
		with self.assertRaises(frappe.ValidationError, msg="a second submit on a phone capped at one went through"):
			self._submit()
		with self.assertRaises(frappe.ValidationError) as refused:
			self._submit(prescription="/private/files/zz-never-uploaded.pdf")
		self.assertIn("attachment expired", str(refused.exception))

	def test_a_dropdown_starts_unanswered(self):
		self.assertEqual(frappe.db.get_value("Web Form Field", {"parent": self.web_form, "fieldname": "dosage"}, "options"), "\n40 mg\n100 mg")
		self.assertEqual(frappe.get_meta(self.sink).get_field("dosage").options, "\n40 mg\n100 mg")
		self.assertFalse(frappe.new_doc(self.sink).dosage, "a fresh submission pre-picked the dropdown's first choice")
