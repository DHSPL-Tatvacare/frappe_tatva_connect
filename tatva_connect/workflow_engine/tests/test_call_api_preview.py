# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The Call API preview answers about the request the author is really about to ship.

`test_call` exists so an author can map capture paths off a REAL response. That only works if two things
are true of the answer, and neither was:

  * the record behind the request is the one the AUTHOR chose. It fell back to the most recently modified
    lead on the site, so an author mapped a tree built from a record they had never seen — and the paths
    they learned there do not have to exist for the next one;
  * the panel can show what was SENT. It reported the authored body, which is None in every mode but
    Custom, so the one mode where the payload most needs showing — the whole lead document goes out —
    reported nothing at all.

Nothing here is mocked. The endpoint is `example.invalid`, so no DNS resolves and no packet leaves; the
payload is logged BEFORE the request is attempted, which is exactly why a preview can still say what it
tried to send when the network refuses it. Asserting on the returned answer rather than on a spy is the
point: a spy would have been green on both defects.
"""
import json

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.tests.authz.grains import assert_masters_exist
from tatva_connect.workflow_engine import context as node_context
from tatva_connect.workflow_engine.tests import fixtures as fx

_ENDPOINT = "Call API Preview Probe Endpoint"
_SERVICE = "Workflow Call API"

_BODY = json.dumps({
	"model": "probe",
	"metadata": {"lead": "$ctx.crm_lead.name"},
	"messages": [{"role": "user", "content": "$ctx.crm_lead.first_name"}],
})


class TestCallApiPreview(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		assert_masters_exist()
		fx.arm_engine(True, cls)
		# Two leads, inserted in order: B is the newest, so a preview that ignores the argument answers about B.
		cls.lead_a = fx.make_lead(first_name="Preview A", lead_name="Preview A")
		cls.lead_b = fx.make_lead(first_name="Preview B", lead_name="Preview B")
		_ensure_endpoint()
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		# The logs go first: `reference_docname` is a Dynamic Link, so a lead with one still pointing at it cannot be deleted.
		for lead in (cls.lead_a, cls.lead_b):
			frappe.db.delete("Integration Request", {
				"integration_request_service": _SERVICE,
				"reference_doctype": "CRM Lead", "reference_docname": lead.name,
			})
			frappe.delete_doc("CRM Lead", lead.name, force=True, ignore_permissions=True)
		if frappe.db.exists("Webhook", _ENDPOINT):
			frappe.delete_doc("Webhook", _ENDPOINT, force=True, ignore_permissions=True)
		frappe.db.commit()

	# --- what was sent ------------------------------------------------------------------------------------

	def test_the_preview_shows_the_whole_record_when_the_author_wrote_no_body(self):
		"""LEAD MODE SENDS THE DOCUMENT, and the panel must say so.

		With no authored body `_call_endpoint` puts `payload_doc.as_dict()` on the wire — every field of the
		lead. The preview reported the authored body instead, which is None here, so the author was shown an
		empty payload for the mode that sends the most. A `capture` path can only be mapped against something
		the author can see.

		`status` is 0 because `example.invalid` cannot answer. That is deliberate: the payload is recorded
		before the request is attempted, so an unreachable endpoint must not cost the author the one half of
		the answer that does not depend on the network.
		"""
		answer = node_context.test_call(_ENDPOINT, lead=self.lead_a.name)

		self.assertEqual(answer["status"], 0, "an unroutable host cannot answer, which is the point")
		self.assertIsInstance(answer["sent"], dict, "the record itself went out — the preview must show it")
		self.assertEqual(answer["sent"].get("name"), self.lead_a.name)
		self.assertEqual(answer["sent"].get("first_name"), self._name_of(self.lead_a))

	def _name_of(self, lead):
		"""The lead's first name AS IT STANDS. Asserted rather than spelled: these probes carry the shared
		fixture grain, so any workflow armed on this bench may legitimately have rewritten the name, and
		what this suite is about is WHICH record went on the wire — not what it is called.
		"""
		return frappe.db.get_value("CRM Lead", lead.name, "first_name")

	def test_the_preview_shows_the_resolved_body_when_the_author_wrote_one(self):
		"""CUSTOM MODE, unchanged in what it reports and rewired in how it gets there.

		The authored body is no longer echoed back from memory — it is read off the same record of the
		request the other mode is read from, so ONE source answers both. This asserts the rewire did not
		change the answer: references resolved at every depth, literals left alone.
		"""
		answer = node_context.test_call(_ENDPOINT, request_body=_BODY, lead=self.lead_a.name)

		self.assertEqual(answer["sent"]["model"], "probe", "a literal is left alone")
		self.assertEqual(answer["sent"]["metadata"]["lead"], self.lead_a.name, "a nested reference resolves")
		self.assertEqual(
			answer["sent"]["messages"][0]["content"], self._name_of(self.lead_a),
			"a reference inside a LIST of objects resolves — where every real API body puts it",
		)

	def test_the_reported_payload_is_the_one_the_request_log_recorded(self):
		"""ONE SOURCE, not two. The decision about what to send lives in `_call_endpoint` and is written to
		the Integration Request by the same line that sends it. A preview that recomputed the payload would
		be a second copy of that decision, and a copy drifts silently — the author would be shown a request
		that is merely plausible. This asserts the two are the same value, so a future change to what is sent
		cannot leave the panel describing the old shape.
		"""
		answer = node_context.test_call(_ENDPOINT, request_body=_BODY, lead=self.lead_b.name)

		logged = frappe.db.get_value(
			"Integration Request",
			{
				"integration_request_service": _SERVICE,
				"reference_doctype": "CRM Lead", "reference_docname": self.lead_b.name,
			},
			"data", order_by="creation desc",
		)
		self.assertEqual(answer["sent"], frappe.parse_json(logged))

	# --- which record ------------------------------------------------------------------------------------

	def test_the_lead_the_author_chose_is_the_lead_that_goes_on_the_wire(self):
		"""The argument must reach the REQUEST, not just the answer's label.

		Reporting the chosen lead while building the request from another one would be the worst version of
		this bug — a preview that names a record and then contradicts itself. Two leads, two previews, and
		each payload has to carry its own lead's data.
		"""
		first = node_context.test_call(_ENDPOINT, lead=self.lead_a.name)
		second = node_context.test_call(_ENDPOINT, lead=self.lead_b.name)

		self.assertEqual(first["lead"], self.lead_a.name)
		self.assertEqual(second["lead"], self.lead_b.name)
		self.assertEqual(first["sent"]["first_name"], self._name_of(self.lead_a))
		self.assertEqual(
			second["sent"]["first_name"], self._name_of(self.lead_b),
			"B is also the newest lead here, so only A's payload proves the argument was honoured",
		)

	def test_a_lead_that_is_gone_is_an_answer_the_panel_can_show(self):
		"""A picked lead can be deleted between the picking and the pressing. Now that the author chooses,
		that is an ordinary outcome of choosing — and a preview control has nothing to render when its own
		call 500s, which is the same reason a dormant engine answers `{"armed": False}` instead of throwing.
		"""
		answer = node_context.test_call(_ENDPOINT, lead="CRM-LEAD-no-such-record")

		self.assertTrue(answer["armed"])
		self.assertTrue(answer.get("error"), "the author is told why, not left with a traceback")
		self.assertNotIn("status", answer, "nothing was called, so there is no status to report")


def _ensure_endpoint():
	"""A curated Webhook row — the endpoint an author PICKS. Its URL is never typed on the node."""
	if frappe.db.exists("Webhook", _ENDPOINT):
		return
	frappe.get_doc({
		"doctype": "Webhook",
		"name": _ENDPOINT,
		"webhook_doctype": "CRM Lead",
		"request_url": "https://example.invalid/preview",
		"request_method": "POST",
		"webhook_docevent": "on_update",
	}).insert(ignore_permissions=True)  # authz-ok: tier-c — test fixture, no user input
	frappe.db.commit()
