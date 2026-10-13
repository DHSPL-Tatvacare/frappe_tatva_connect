# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Meta conversions: a stage change saved on a Facebook lead reaches Meta once, in Meta's Conversion Leads
shape, read off the change log with nothing added to the lead's save; and an unreachable Meta loses nothing.

Only the HTTP call is stubbed, at `make_post_request`. The reader, the mapping, the payload, the
Integration Request row and the `read_upto` move are the real code.
Run:
    bench --site dev.localhost run-tests --app tatva_connect \\
        --module tatva_connect.tests.lead_sync.test_meta_conversions
"""
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.lead_sync import conversions
from tatva_connect.taxonomy import grain

VERTICAL = "ZZ Meta Line"
GROUP = "ZZ Meta Group"
PROGRAM = "ZZ Meta Program"
BOUGHT = "ZZ Meta Bought"
BROWSING = "ZZ Meta Browsing"
DATASET_ID = "990000000000001"
FB_LEAD_ID = "120000000000000001"


class TestMetaConversions(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		for doctype, field, value in (("CRM Vertical", "vertical_name", VERTICAL), ("CRM Group", "group_name", GROUP),
		                              ("CRM Program", "program_name", PROGRAM)):
			if not frappe.db.exists(doctype, value):
				frappe.get_doc({"doctype": doctype, field: value}).insert(ignore_permissions=True)
		cls.bought = cls._stage(BOUGHT)
		cls.browsing = cls._stage(BROWSING)
		frappe.db.commit()

	@classmethod
	def _stage(cls, stage):
		existing = frappe.db.get_value("CRM Lead Stage", {"program": PROGRAM, "stage": stage}, "name")
		return existing or frappe.get_doc({
			"doctype": "CRM Lead Stage", "program": PROGRAM, "stage": stage, "selectable": 1,
		}).insert(ignore_permissions=True).name

	def setUp(self):
		frappe.set_user("Administrator")
		self._clean()
		self.dataset = frappe.get_doc({
			"doctype": "CRM Meta Dataset", "dataset_name": "ZZ Meta Probe", "dataset_id": DATASET_ID,
			"enabled": 1, "send_frequency": "Hourly", "access_token": "zz-dataset-token",
			"graph_api_version": "v23.0", "lead_event_source": "ZZ CRM",
			"stages": [{"crm_stage": self.bought, "meta_event": "Converted"}],
		}).insert(ignore_permissions=True)
		frappe.db.commit()

	def tearDown(self):
		self._clean()

	@classmethod
	def _clean(cls):
		frappe.db.delete("Integration Request", {"reference_doctype": "CRM Meta Dataset", "reference_docname": DATASET_ID})
		if frappe.db.exists("CRM Meta Dataset", DATASET_ID):
			frappe.delete_doc("CRM Meta Dataset", DATASET_ID, force=True, ignore_permissions=True)
		for name in frappe.get_all("CRM Lead", filters={"first_name": "ZZ Meta Probe"}, pluck="name"):
			frappe.delete_doc("CRM Lead", name, force=True, ignore_permissions=True)
		frappe.db.commit()

	def _lead(self, facebook_lead_id=None):
		values = dict(zip(grain.columns("CRM Lead"), (VERTICAL, GROUP, PROGRAM), strict=True))
		return frappe.get_doc({
			"doctype": "CRM Lead", "first_name": "ZZ Meta Probe", "facebook_lead_id": facebook_lead_id, **values,
		}).insert(ignore_permissions=True)

	def _move(self, lead, stage):
		lead.reload()
		lead.custom_substage = stage
		# Frappe writes no Version under tests unless asked (`ignore_version` defaults to `in_test`).
		lead.save(ignore_permissions=True, ignore_version=False)  # authz-ok: tier-a — test fixture, runs as Administrator

	def _pass(self, post):
		with patch("tatva_connect.lead_sync.graph.make_post_request", post):
			conversions.send_pass(frappe.get_doc("CRM Meta Dataset", DATASET_ID))

	@staticmethod
	def _meta_refuses(code, subcode=None):
		"""A Graph 400 with Meta's error code, left where `make_request` leaves a response, then raised."""
		def refuse(*args, **kwargs):
			frappe.flags.integration_request = MagicMock(
				status_code=400, text="zz refused", json=lambda: {"error": {"code": code, "error_subcode": subcode, "message": "zz refused"}})
			raise Exception("400 Client Error")
		return refuse

	def test_a_mapped_stage_change_on_a_facebook_lead_is_sent_once_in_metas_shape(self):
		fb_lead = self._lead(FB_LEAD_ID)
		other = self._lead()
		self._move(fb_lead, self.browsing)  # not in the grid
		self._move(fb_lead, self.bought)
		self._move(other, self.bought)  # no Meta lead ID
		frappe.db.commit()

		post = MagicMock(return_value={"events_received": 1, "fbtrace_id": "zz-trace"})
		self._pass(post)
		self._pass(post)  # a second pass finds nothing new

		self.assertEqual(post.call_count, 1, "the change was not sent exactly once")
		sent = frappe.parse_json(post.call_args.kwargs["data"]["data"])
		self.assertEqual(len(sent), 1, "an unmapped or non-Facebook change was sent")
		event = sent[0]
		self.assertEqual(event["event_name"], "Converted")
		self.assertEqual(event["action_source"], "system_generated")
		newest = frappe.get_all("Version", filters={"ref_doctype": "CRM Lead", "docname": fb_lead.name}, pluck="name", order_by="creation desc")[0]
		self.assertEqual(event["event_id"], newest, "the event id is not its change-log row, so a resent batch counts twice")
		self.assertEqual(event["user_data"], {"lead_id": int(FB_LEAD_ID)})
		self.assertEqual(event["custom_data"], {"lead_event_source": "ZZ CRM", "event_source": "crm"})
		self.assertEqual(frappe.get_all("Integration Request", pluck="status", filters={
			"integration_request_service": conversions.SERVICE, "reference_docname": DATASET_ID}), ["Completed"])

	def test_an_outage_or_a_dead_token_keeps_the_batch_for_the_next_pass(self):
		self._move(self._lead(FB_LEAD_ID), self.bought)
		frappe.db.commit()
		start = frappe.db.get_value("CRM Meta Dataset", DATASET_ID, "read_upto")

		self._pass(MagicMock(side_effect=ConnectionError("zz network down")))
		self.assertEqual(frappe.db.get_value("CRM Meta Dataset", DATASET_ID, "read_upto"), start,
		                 "read_upto moved past a batch Meta never received")

		for code, subcode in ((190, None), (100, 33)):  # an expired token, and a dataset the token cannot reach
			self._pass(MagicMock(side_effect=self._meta_refuses(code, subcode)))
			self.assertEqual(frappe.db.get_value("CRM Meta Dataset", DATASET_ID, "read_upto"), start,
			                 f"read_upto moved past a batch refused with code {code}, which a resend can fix")
		self.assertFalse(conversions.has_sent(DATASET_ID), "a failed batch counted as sent and would block Send Past Days")
		refusal = self._meta_refuses(100)
		with self.assertRaises(Exception):
			refusal()
		self.assertTrue(conversions._refused_for_content(frappe.flags.integration_request),
		                "a batch Meta refuses on its content would be resent forever")

		post = MagicMock(return_value={"events_received": 1})
		self._pass(post)
		self.assertEqual(post.call_count, 1, "the unsent batch was not sent on the next pass")
		self.assertEqual(sorted(frappe.get_all("Integration Request", pluck="status", filters={
			"integration_request_service": conversions.SERVICE, "reference_docname": DATASET_ID})), ["Completed", "Failed", "Failed", "Failed"])
