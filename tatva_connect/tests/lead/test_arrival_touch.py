# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A lead that arrives again leaves a mark, whatever door it came through.

The create brain merges a returning patient onto the lead they already have, and recorded nothing about
it: the return and a rep's edit were the same event on the record, so nothing could tell them apart. An
arrival now times itself in the Acquisition section — the section that exists to say a patient was
acquired more than once — the way the door already stamps `source` and the Facebook ids.

A source that times its own arrival keeps that time. Facebook sends the moment the patient submitted the
form, which is what makes a re-crawl of one submission land on the row it already wrote.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \
        --module tatva_connect.tests.lead.test_arrival_touch
"""
import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.api import partner
from tatva_connect.tests.api import partner_fixture

PHONE = "+916100050001"
SENT_TOUCH = "2026-07-20 10:00:00"


class TestArrivalTouch(FrappeTestCase):
	PARTNER_USER = "zz-arrival-partner@example.com"

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		cls._purge_leads()
		partner_fixture.mint_partner(cls.PARTNER_USER)
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		cls._purge_leads()
		partner_fixture.teardown()
		frappe.db.commit()
		super().tearDownClass()

	@classmethod
	def _purge_leads(cls):
		for name in frappe.get_all("CRM Lead", filters={"mobile_no": ["like", "+91610005%"]}, pluck="name"):
			frappe.delete_doc("CRM Lead", name, force=True, ignore_permissions=True)

	def setUp(self):
		frappe.set_user("Administrator")
		self.table, self.column = partner_fixture.touch_address()
		self.sp = f"arrival_{frappe.generate_hash(length=6)}"
		frappe.db.savepoint(self.sp)

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback(save_point=self.sp)

	def _arrive(self, extra=None):
		"""One arrival through the partner door — the same `_upsert_one` every other door enters by."""
		frappe.set_user(self.PARTNER_USER)
		try:
			payload = frappe._dict({"mobile_no": PHONE, "first_name": "Asha Arrival"})
			payload.update(extra or {})
			_user, mp, is_sysmgr, parent_fields, child_allow = partner._caller_fields()
			doc, action = partner._upsert_one(payload, mp, is_sysmgr, parent_fields, child_allow, [])
		finally:
			frappe.set_user("Administrator")
		return frappe.get_doc("CRM Lead", doc.name), action

	def _touches(self, lead):
		return [row.get(self.column) for row in (lead.get(self.table) or [])]

	def test_a_return_is_a_second_touch(self):
		"""The whole point: the lead is the same lead, and the section says they came twice."""
		first, action = self._arrive()
		self.assertEqual(action, "created")
		self.assertEqual(len(self._touches(first)), 1, "the first arrival left no touch")

		second, action = self._arrive()
		self.assertEqual(action, "updated", "a return must merge onto the lead, never mint a second one")
		self.assertEqual(second.name, first.name)
		touches = self._touches(second)
		self.assertEqual(len(touches), 2, "the return left no touch of its own")
		self.assertGreater(max(touches), max(self._touches(first)), "the newest touch did not move")

	def test_a_source_that_times_itself_keeps_its_time(self):
		"""Facebook sends Meta's submission time; ours must never overwrite it, or a re-crawl re-arrives."""
		lead, _action = self._arrive({self.table: [{self.column: SENT_TOUCH}]})
		self.assertEqual(
			[frappe.utils.get_datetime(touch) for touch in self._touches(lead)],
			[frappe.utils.get_datetime(SENT_TOUCH)],
			"the caller's own arrival time was not kept",
		)

	def test_an_update_by_id_is_not_an_arrival(self):
		"""`lead_update` addresses a lead that is already here — an edit of it, never a patient coming back."""
		lead, _action = self._arrive()
		frappe.set_user(self.PARTNER_USER)
		try:
			_user, mp, is_sysmgr, parent_fields, child_allow = partner._caller_fields()
			partner._update_one(
				lead.name, {"first_name": "Asha Corrected"}, mp, is_sysmgr, parent_fields, child_allow
			)
		finally:
			frappe.set_user("Administrator")
		self.assertEqual(
			len(self._touches(frappe.get_doc("CRM Lead", lead.name))), 1, "an edit was recorded as an arrival"
		)
