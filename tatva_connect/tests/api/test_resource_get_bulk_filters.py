# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Every resource's `*_get_bulk` filter mode, checked against its own `*_list` as the oracle."""
import unittest

import frappe

from tatva_connect.api import partner_activity, partner_call, partner_file, partner_note, partner_ticket

EPOCH = "2000-01-01 00:00:00"
FAR = "2999-01-01 00:00:00"

# (label, list endpoint, bulk endpoint, list key, (scope param, parent doctype, doctype, parent column, filters))
RESOURCES = (
	("activity", partner_activity.activity_list, partner_activity.activity_get_bulk, "activities",
	 ("lead", "CRM Lead", "CRM Task", "reference_docname", {"reference_doctype": "CRM Lead"})),
	("call", partner_call.call_list, partner_call.call_get_bulk, "calls",
	 ("lead", "CRM Lead", "CRM Call Log", "reference_docname", {"reference_doctype": "CRM Lead"})),
	("file", partner_file.file_list, partner_file.file_get_bulk, "files",
	 ("lead", "CRM Lead", "File", "attached_to_name", {"attached_to_doctype": "CRM Lead"})),
	("ticket", partner_ticket.ticket_list, partner_ticket.ticket_get_bulk, "tickets",
	 ("lead", "CRM Lead", "HD Ticket", "custom_lead", {"custom_lead": ["is", "set"]})),
	("note", partner_note.note_list, partner_note.note_get_bulk, "notes",
	 ("lead", "CRM Lead", "FCRM Note", "reference_docname", {"reference_doctype": "CRM Lead"})),
	("comment", partner_ticket.comment_list, partner_ticket.comment_get_bulk, "comments",
	 ("ticket", "HD Ticket", "HD Ticket Comment", "reference_ticket", {})),
)


class TestResourceGetBulkFilters(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		frappe.set_user("Administrator")

	def setUp(self):
		self._form = frappe.form_dict

	def tearDown(self):
		frappe.form_dict = self._form

	def _call(self, endpoint, **form):
		frappe.local.response = frappe._dict()
		frappe.form_dict = frappe._dict(form)
		endpoint()
		return frappe.local.response

	def _scope(self, list_ep, key, source):
		"""{param: parent} for the first parent whose own list returns records, asked of the list itself."""
		param, parent_doctype, doctype, column, filters = source
		for parent in frappe.get_all(doctype, filters=filters, pluck=column, distinct=True, order_by=f"{column} desc", limit=200):
			if frappe.db.exists(parent_doctype, parent) and self._call(list_ep, **{param: parent}, limit=1)["data"][key]:
				return {param: parent}
		return None

	def _with_data(self):
		"""The resources this bench holds records for; the parity checks need at least four of the six to mean anything."""
		have = [r for r in RESOURCES if self._scope(r[1], r[3], r[4])]
		print(f"\n  resources with local data: {[r[0] for r in have]}")
		self.assertGreaterEqual(len(have), 4, "too few resources have local records to prove parity")
		return have

	def test_every_date_filter_matches_the_list(self):
		"""Same filter on list and bulk: the same records in the same order, and the same total."""
		for label, list_ep, bulk_ep, key, source in self._with_data():
			scope = self._scope(list_ep, key, source)
			for date_filter in ({"created_after": EPOCH}, {"created_before": FAR}, {"updated_after": EPOCH},
			                    {"updated_before": FAR}, {"created_after": FAR}):
				with self.subTest(resource=label, filter=date_filter):
					form = {**scope, **date_filter, "limit": 50}
					listed = self._call(list_ep, **form)
					bulk = self._call(bulk_ep, **form)
					self.assertEqual(bulk.get("status"), "success", bulk.get("error"))
					self.assertEqual([r["data"]["name"] for r in bulk["results"]], [r["name"] for r in listed["data"][key]])
					self.assertEqual(bulk["paging"]["total"], listed["data"]["total"])

	def test_records_carry_creation_and_modified(self):
		for label, list_ep, _bulk_ep, key, source in self._with_data():
			with self.subTest(resource=label):
				rows = self._call(list_ep, **self._scope(list_ep, key, source), limit=1)["data"][key]
				self.assertTrue(rows[0].get("creation") and rows[0].get("modified"))

	def test_id_mode_is_unchanged(self):
		"""Names still read exactly those records, ignore filters, and add no `paging`."""
		for label, list_ep, bulk_ep, key, source in self._with_data():
			with self.subTest(resource=label):
				name = self._call(list_ep, **self._scope(list_ep, key, source), limit=1)["data"][key][0]["name"]
				body = self._call(bulk_ep, names=[name], created_after=FAR)
				self.assertEqual([r["data"]["name"] for r in body["results"]], [name])
				self.assertNotIn("paging", body)

	def test_an_empty_body_is_still_refused(self):
		for label, _list_ep, bulk_ep, *_rest in RESOURCES:
			with self.subTest(resource=label):
				self.assertEqual(self._call(bulk_ep)["error"]["code"], "validation_error")
