# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""`lead_get_bulk` filter mode, checked against `lead_list` as the oracle."""
import unittest
from unittest.mock import patch

import frappe

from tatva_connect.api import partner

EPOCH = "2000-01-01"
PAGE = 20


class TestLeadGetBulkFilters(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		frappe.set_user("Administrator")

	def setUp(self):
		self._form = frappe.form_dict
		if frappe.db.count("CRM Lead") <= PAGE * 2:
			self.skipTest("this bench holds fewer than two pages of leads")

	def tearDown(self):
		frappe.form_dict = self._form

	def _call(self, endpoint, **form):
		frappe.local.response = frappe._dict()
		frappe.form_dict = frappe._dict(form)
		endpoint()
		return frappe.local.response

	def _bulk_names(self, body):
		return [r["data"]["name"] for r in body["results"]]

	def test_filter_mode_names_the_same_leads_as_lead_list(self):
		"""Same filter and page give the same leads in the same order."""
		for offset in (0, PAGE):
			with self.subTest(offset=offset):
				listed = self._call(partner.lead_list, updated_after=EPOCH, limit=PAGE, offset=offset)
				bulk = self._call(partner.lead_get_bulk, updated_after=EPOCH, limit=PAGE, offset=offset)
				self.assertEqual(bulk["status"], "success", bulk.get("error"))
				self.assertEqual(self._bulk_names(bulk), [r["name"] for r in listed["data"]["leads"]])
				self.assertEqual(bulk["paging"]["total"], listed["data"]["total"])
				self.assertEqual(bulk["paging"]["has_more"], listed["data"]["has_more"])

	def test_every_published_filter_works_on_both_calls(self):
		"""Each key `lead_schema` publishes in `list_filters` narrows both calls to the same leads."""
		lead = frappe.get_all("CRM Lead", fields=["mobile_no", "status"], limit=1,
		                      filters={"mobile_no": ["is", "set"], "status": ["is", "set"]})[0]
		values = {"status": lead.status, "created_after": EPOCH, "created_before": "2999-01-01 00:00:00",
		          "updated_after": EPOCH, "updated_before": "2999-01-01 00:00:00", "mobile_no": lead.mobile_no}
		self.assertEqual(set(values), set(partner.LEAD_FILTER_KEYS), "a published filter has no case here")
		for key, value in values.items():
			with self.subTest(filter=key):
				listed = self._call(partner.lead_list, **{key: value}, limit=PAGE)
				bulk = self._call(partner.lead_get_bulk, **{key: value}, limit=PAGE)
				self.assertEqual(self._bulk_names(bulk), [r["name"] for r in listed["data"]["leads"]])
				self.assertEqual(bulk["paging"]["total"], listed["data"]["total"])

	def test_filter_mode_returns_the_full_record(self):
		"""Each result is the full `lead_get` record."""
		bulk = self._call(partner.lead_get_bulk, updated_after=EPOCH, limit=1)
		name = bulk["results"][0]["data"]["name"]
		single = self._call(partner.lead_get, name=name)
		self.assertEqual(bulk["results"][0]["data"], single["data"])

	def test_filter_mode_pages_are_disjoint(self):
		bulk = [self._call(partner.lead_get_bulk, updated_after=EPOCH, limit=PAGE, offset=o) for o in (0, PAGE)]
		first, second = (set(self._bulk_names(b)) for b in bulk)
		self.assertFalse(first & second, "a lead was read on two pages")

	def test_a_filter_narrows_the_result(self):
		"""A filter matching nothing returns an empty page, not an error."""
		body = self._call(partner.lead_get_bulk, updated_after="2999-01-01")
		self.assertEqual(body["status"], "success", body.get("error"))
		self.assertEqual(body["results"], [])
		self.assertEqual(body["paging"]["total"], 0)
		self.assertFalse(body["paging"]["has_more"])

	def test_ids_win_over_filters_as_before(self):
		"""Ids plus a filter key reads the ids and ignores the filter — what the committed code always did."""
		name = frappe.get_all("CRM Lead", pluck="name", limit=1)[0]
		body = self._call(partner.lead_get_bulk, names=[name], updated_after="2999-01-01")
		self.assertEqual([r["data"]["name"] for r in body["results"]], [name])
		self.assertNotIn("paging", body)

	def test_a_partner_key_reads_only_its_own_line(self):
		"""actual ⊆ fence: every lead filter mode returns is on the key's vertical+group, and none is missed."""
		lead = frappe.get_all("CRM Lead", fields=["custom_vertical", "custom_group"], limit=1,
		                      filters={"custom_vertical": ["is", "set"], "custom_group": ["is", "set"]})[0]
		mp = frappe._dict(vertical=lead.custom_vertical, crm_group=lead.custom_group, program=None, source=None)
		fence = {"custom_vertical": mp.vertical, "custom_group": mp.crm_group, "modified": [">=", EPOCH]}
		self.assertLess(frappe.db.count("CRM Lead", fence), frappe.db.count("CRM Lead"),
		                "precondition: leads outside the fence exist, or the test proves nothing")
		fields = partner._caller_fields()
		with patch.object(partner, "_caller_fields", return_value=(fields[0], mp, False, *fields[3:])):
			body = self._call(partner.lead_get_bulk, updated_after=EPOCH, limit=200)
		self.assertEqual(body["paging"]["total"], frappe.db.count("CRM Lead", fence))
		grains = {(r["data"]["custom_vertical"], r["data"]["custom_group"]) for r in body["results"]}
		self.assertEqual(grains, {(mp.vertical, mp.crm_group)}, "a lead outside the key's line was returned")

	def test_neither_ids_nor_filters_is_refused(self):
		body = self._call(partner.lead_get_bulk)
		self.assertEqual(body["error"]["code"], "validation_error")

	def test_id_mode_is_unchanged(self):
		"""Id mode is input-ordered, reports unknown ids in place, and gains no key."""
		name = frappe.get_all("CRM Lead", pluck="name", limit=1)[0]
		body = self._call(partner.lead_get_bulk, names=[name, "no-such-lead"])
		self.assertEqual(body["results"][0]["data"]["name"], name)
		self.assertEqual(body["results"][1]["status"], "error")
		self.assertEqual(body["summary"], {"total": 2, "succeeded": 1, "failed": 1})
		self.assertNotIn("paging", body, "id mode gains no key")
