# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The bulk lane answers exactly what the single lane answers, record for record, and its query count does not grow with the page."""
import json
import unittest
from unittest.mock import patch

import frappe

from tatva_connect.api import (
	bulk_lane,
	partner,
	partner_activity,
	partner_call,
	partner_file,
	partner_note,
	partner_ticket,
)
from tatva_connect.api._base import bulk_keys

# (label, bulk endpoint, doctype the names come from, filters picking real records, the column naming each record's lead)
RESOURCES = (
	("lead", partner.lead_get_bulk, "CRM Lead", {}, None),
	("activity", partner_activity.activity_get_bulk, "CRM Task", {"reference_doctype": "CRM Lead"}, "reference_docname"),
	("call", partner_call.call_get_bulk, "CRM Call Log", {"reference_doctype": "CRM Lead"}, "reference_docname"),
	("note", partner_note.note_get_bulk, "FCRM Note", {"reference_doctype": "CRM Lead"}, "reference_docname"),
	("file", partner_file.file_get_bulk, "File", {"attached_to_doctype": "CRM Lead"}, "attached_to_name"),
	("ticket", partner_ticket.ticket_get_bulk, "HD Ticket", {}, None),
	("comment", partner_ticket.comment_get_bulk, partner_ticket.COMMENT, partner_ticket.ON_TICKET, None),
)
PAGE = 40
# Fewer records than this on the bench and a resource proves nothing.
LEAST = 5
# A page's own overhead between one record and forty: one query per table and the distinct configs, never a per-record cost.
FLAT = 30


def single_lane_only():
	"""The bulk lane switched off, so every record is answered by the single lane: the path this lane must equal."""
	return patch.multiple(bulk_lane, docs=lambda *a, **k: {}, names_within=lambda *a, **k: set())


class TestBulkReadLoadsAPage(unittest.TestCase):
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
		return json.loads(frappe.as_json(frappe.local.response))

	def _names(self, doctype, filters, lead_column, live_leads=False):
		"""Up to PAGE real names, newest first; with `live_leads`, only records whose lead still exists."""
		rows = frappe.get_all(doctype, filters=filters, fields=["name", *([lead_column] if lead_column else [])],
		                      order_by="modified desc", limit=PAGE * 5)
		if live_leads and lead_column:
			live = set(frappe.get_all("CRM Lead", filters={"name": ["in", [r[lead_column] for r in rows]]}, pluck="name"))
			rows = [r for r in rows if r[lead_column] in live]
		names = [str(r.name) for r in rows][:PAGE]
		if len(names) < LEAST:
			self.skipTest(f"this bench holds fewer than {LEAST} {doctype} records")
		return names

	def _queries(self, endpoint, **form):
		real, count = frappe.db.sql, [0]

		def counting(*args, **kwargs):
			count[0] += 1
			return real(*args, **kwargs)

		with patch.object(frappe.db, "sql", side_effect=counting):
			self._call(endpoint, **form)
		return count[0]

	def test_the_bulk_lane_answers_what_the_single_lane_answers(self):
		for label, endpoint, doctype, filters, lead_column in RESOURCES:
			with self.subTest(label):
				names, key = self._names(doctype, filters, lead_column), bulk_keys(label)["get"]
				asked = [*names, "zz-no-such-record", names[0]]
				paged = self._call(endpoint, **{key: asked})
				with single_lane_only():
					one_by_one = self._call(endpoint, **{key: asked})
				self.assertEqual(paged, one_by_one)

	def test_leads_by_phone_answer_what_the_single_lane_answers(self):
		phones = frappe.get_all("CRM Lead", filters={"mobile_no": ["is", "set"]}, pluck="mobile_no", order_by="modified desc", limit=PAGE)
		asked = [*phones, "+910000000000", "not a phone"]
		paged = self._call(partner.lead_get_bulk, mobile_nos=asked)
		with single_lane_only():
			one_by_one = self._call(partner.lead_get_bulk, mobile_nos=asked)
		self.assertEqual(paged, one_by_one)

	def test_a_page_costs_the_same_queries_at_one_record_and_at_forty(self):
		for label, endpoint, doctype, filters, lead_column in RESOURCES:
			with self.subTest(label):
				names, key = self._names(doctype, filters, lead_column, live_leads=True), bulk_keys(label)["get"]
				one = self._queries(endpoint, **{key: names[:1]})
				many = self._queries(endpoint, **{key: names})
				self.assertLess(many - one, FLAT, f"{label}: {one} queries for 1 record, {many} for {len(names)}")

	def test_a_page_that_throws_is_answered_record_by_record_with_a_clean_envelope(self):
		names = self._names("CRM Lead", {}, None)[:5]

		def throws(*args, **kwargs):
			frappe.throw("page failed")

		with patch.object(bulk_lane, "docs", side_effect=throws), patch.object(frappe, "log_error"):
			paged = self._call(partner.lead_get_bulk, names=names)
		self.assertEqual(frappe.local.message_log, [], "the page's throw must not reach the envelope as _server_messages")
		with single_lane_only():
			one_by_one = self._call(partner.lead_get_bulk, names=names)
		self.assertEqual(paged, one_by_one)
