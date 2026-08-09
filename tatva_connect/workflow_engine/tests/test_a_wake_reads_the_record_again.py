# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Every segment reads the subject as it is NOW.

The same lead is loaded six times in one activity save, so caching it is tempting and was measured: it
returned two queries on the read path and cost ninety-four on the write path, where sharing a mutated
document across saves makes Frappe re-diff its children. The cache was reverted; this lock is what the
attempt was worth, because the invariant it protects is real either way.

`interpreter._doc_loader`'s own docstring records the defect: "wait 30 days, then if the lead is still
New" tested a 30-day-old value. A journey that parks for a month and wakes must read the document today,
so state is rebuilt per SEGMENT — `_refreshed_state` on every wake — and never carried across a park.
RED against any cache with a longer life: a module global, `frappe.local`, or one hung off the journey.
"""
import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.workflow_engine.tests import fixtures as fx


class TestAWakeReadsTheRecordAgain(FrappeTestCase):
	"""THE guard. RED on any cache that outlives a segment — a module global, `frappe.local`, or a
	registry hung off the journey instead of off the segment's state."""

	@classmethod
	def setUpClass(cls):
		cls.lead = fx.make_lead(status="New")
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		frappe.delete_doc("CRM Lead", cls.lead.name, force=True, ignore_permissions=True)
		frappe.db.commit()

	def test_a_second_segment_sees_the_lead_as_it_is_now(self):
		"""A journey parks, the lead changes underneath it, the journey wakes. The value it reads must be
		the one on the document TODAY, never the one captured when it parked."""
		from tatva_connect.workflow_engine import interpreter

		journey = frappe._dict(subject_doctype="CRM Lead", subject_name=self.lead.name, state_json="{}")

		parked = interpreter._refreshed_state(journey)
		self.assertEqual(parked.get("crm_lead.status"), "New", "premise: the lead starts New")

		frappe.db.set_value("CRM Lead", self.lead.name, "status", "Qualified")
		frappe.db.commit()

		woken = interpreter._refreshed_state(journey)
		self.assertEqual(woken.get("crm_lead.status"), "Qualified",
		                 "the wake read a stale document — the cache outlived its segment")
