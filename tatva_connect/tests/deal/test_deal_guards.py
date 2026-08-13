# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A Deal is the customer a Lead became — it carries that lead's grain, and only where deals are armed.

What is asserted:

  * a deal takes its (product line, group, program) from the lead it names, read through `grain.columns`
    and never by typing a fieldname here;
  * the grain is RE-derived on every save, so a lead that moves program takes its deal with it;
  * a product line that has not been armed for deals refuses to carry one;
  * a second deal on the same lead is refused — a renewal is a row inside the deal, not another deal;
  * a phone number on the deal is stored in the one canonical +E.164 form;
  * a deal is opened ONLY from the stage its programme declares to be the moment the lead bought, and
    that judgement is made at birth, so an old deal whose lead has since moved on stays saveable;
  * a sale line's renewal date is the SKU's duration added to the line's start date — computed, and
    recomputed over anything typed, because a typed renewal date drifts from the plan it belongs to;
  * with the switch off, every one of the above is dormant and stock crm behaviour stands.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \\
        --module tatva_connect.tests.deal.test_deal_guards
"""
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_days, getdate

from tatva_connect.deal.deals import normalize_deal_phones
from tatva_connect.taxonomy import grain

GUARDS_SWITCH = "Deal::CRM Deal::guards"

ARMED = "ZZ Deal Armed Line"
UNARMED = "ZZ Deal Unarmed Line"
GROUP = "ZZ Deal Group"
PROGRAM_ONE = "ZZ Deal Program One"
PROGRAM_TWO = "ZZ Deal Program Two"

BOUGHT = "ZZ Deal Bought"
BROWSING = "ZZ Deal Browsing"
SKU_TERM = "ZZ Deal SKU Term"
SKU_OPEN = "ZZ Deal SKU Open"
TERM_DAYS = 90
START = "2026-01-01"
# A date nobody could arrive at from START + a duration — so a survivor proves the value was typed, not derived.
TYPED = "2030-12-31"


class TestDealGuards(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		for vertical, deals_enabled in ((ARMED, 1), (UNARMED, 0)):
			if not frappe.db.exists("CRM Vertical", vertical):
				frappe.get_doc({
					"doctype": "CRM Vertical", "vertical_name": vertical, "deals_enabled": deals_enabled,
				}).insert(ignore_permissions=True)
		if not frappe.db.exists("CRM Group", GROUP):
			frappe.get_doc({"doctype": "CRM Group", "group_name": GROUP}).insert(ignore_permissions=True)
		for program in (PROGRAM_ONE, PROGRAM_TWO):
			if not frappe.db.exists("CRM Program", program):
				frappe.get_doc({"doctype": "CRM Program", "program_name": program}).insert(ignore_permissions=True)
		# One stage the programme calls "they bought" and one it does not — the whole conversion gate.
		cls.bought = cls._stage(BOUGHT, 1)
		cls.browsing = cls._stage(BROWSING, 0)
		# One SKU that runs for a term and one sold open-ended, so the arithmetic has both a numerator and a blank.
		cls.term_sku = cls._sku(SKU_TERM, TERM_DAYS)
		cls.open_sku = cls._sku(SKU_OPEN, 0)
		frappe.db.commit()

	@classmethod
	def _stage(cls, stage, is_conversion_point):
		"""The PK is `format:{program}::{stage}`, so the row is asked for its own name rather than built here."""
		existing = frappe.db.get_value("CRM Lead Stage", {"program": PROGRAM_ONE, "stage": stage}, "name")
		if existing:
			frappe.db.set_value("CRM Lead Stage", existing, "is_conversion_point", is_conversion_point)
			return existing
		return frappe.get_doc({
			"doctype": "CRM Lead Stage", "program": PROGRAM_ONE, "stage": stage,
			"selectable": 1, "is_conversion_point": is_conversion_point,
		}).insert(ignore_permissions=True).name

	@classmethod
	def _sku(cls, product_code, duration_days):
		if frappe.db.exists("CRM Product", product_code):
			frappe.db.set_value("CRM Product", product_code, "custom_duration_days", duration_days)
			return product_code
		return frappe.get_doc({
			"doctype": "CRM Product", "product_code": product_code, "product_name": product_code,
			"standard_rate": 1000, "custom_duration_days": duration_days,
		}).insert(ignore_permissions=True).name

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		for name in frappe.get_all("CRM Deal", filters={"custom_vertical": ["in", [ARMED, UNARMED]]}, pluck="name"):
			frappe.delete_doc("CRM Deal", name, force=True, ignore_permissions=True)
		for name in frappe.get_all("CRM Lead", filters={"custom_vertical": ["in", [ARMED, UNARMED]]}, pluck="name"):
			frappe.delete_doc("CRM Lead", name, force=True, ignore_permissions=True)
		# Stages and SKUs go after the rows that link to them, and before the programme that names them.
		for dt, name in (("CRM Lead Stage", cls.bought), ("CRM Lead Stage", cls.browsing),
						 ("CRM Product", cls.term_sku), ("CRM Product", cls.open_sku),
						 ("CRM Program", PROGRAM_ONE), ("CRM Program", PROGRAM_TWO), ("CRM Group", GROUP),
						 ("CRM Vertical", ARMED), ("CRM Vertical", UNARMED)):
			if frappe.db.exists(dt, name):
				frappe.delete_doc(dt, name, force=True, ignore_permissions=True)
		frappe.db.commit()
		super().tearDownClass()

	def setUp(self):
		self.addCleanup(frappe.db.rollback)
		frappe.set_user("Administrator")
		self._arm(GUARDS_SWITCH)

	def _arm(self, live):
		"""In process, never written to the bench: a written switch survives rollback and leaks into every later suite."""
		enabled = patch("tatva_connect.automation.is_enabled", side_effect=lambda key: key == live)
		enabled.start()
		self.addCleanup(enabled.stop)

	# ---- fixtures ----------------------------------------------------------------------------------

	@staticmethod
	def _column(doctype, axis):
		"""The column an axis lives in — asked of the grain brain, never typed."""
		return grain.columns(doctype)[grain.AXES.index(axis)]

	def _lead(self, vertical=ARMED, program=PROGRAM_ONE, stage=None):
		"""A probe lead sits on the buying stage by default — every other rule here is about what happens next."""
		axes = dict(zip(grain.AXES, (vertical, GROUP, program), strict=True))
		values = {self._column("CRM Lead", axis): value for axis, value in axes.items()}
		return frappe.get_doc({
			"doctype": "CRM Lead", "first_name": "ZZ Deal Probe",
			"custom_substage": self.bought if stage is None else stage, **values,
		}).insert(ignore_permissions=True)

	def _deal(self, lead=None, **kw):
		return frappe.get_doc({
			"doctype": "CRM Deal", "lead": lead, **kw,
		}).insert(ignore_permissions=True)

	@staticmethod
	def _line(sku, start=START, renewal=None):
		"""One sale line; product_name and rate are crm's own required columns on the child row."""
		row = {"product_code": sku, "product_name": sku, "rate": 1000, "qty": 1, "custom_start_date": start}
		if renewal:
			row["custom_renewal_date"] = renewal
		return row

	def _grain_of(self, doc):
		return tuple(doc.get(column) for column in grain.columns("CRM Deal"))

	# ---- the grain ---------------------------------------------------------------------------------

	def test_deal_takes_its_grain_from_its_lead(self):
		"""Before the deal carried grain columns, convert dropped every one of them on the floor."""
		lead = self._lead()
		deal = self._deal(lead.name)
		self.assertEqual(self._grain_of(deal), grain.of("CRM Lead", lead.name),
						 "the deal is not filed under the same product line as the lead it came from")

	def test_grain_is_restamped_when_the_lead_moves(self):
		lead = self._lead(program=PROGRAM_ONE)
		deal = self._deal(lead.name)
		frappe.db.set_value("CRM Lead", lead.name, self._column("CRM Lead", "program"), PROGRAM_TWO)
		deal.save(ignore_permissions=True)
		self.assertEqual(deal.get(self._column("CRM Deal", "program")), PROGRAM_TWO,
						 "the deal kept a program its lead has left")

	def test_a_line_without_deals_refuses_one(self):
		lead = self._lead(vertical=UNARMED)
		with self.assertRaises(frappe.ValidationError):
			self._deal(lead.name)

	def test_a_deal_with_no_lead_is_refused(self):
		with self.assertRaises(frappe.ValidationError):
			self._deal(None)

	# ---- one deal per customer ---------------------------------------------------------------------

	def test_a_second_deal_on_one_lead_is_refused(self):
		lead = self._lead()
		self._deal(lead.name)
		with self.assertRaises(frappe.ValidationError):
			self._deal(lead.name)

	# ---- the moment they bought --------------------------------------------------------------------

	def test_a_lead_not_at_the_conversion_stage_cannot_convert(self):
		"""Convert is gated on the SERVER, so every Convert button — list, desktop and mobile — is covered."""
		lead = self._lead(stage=self.browsing)
		with self.assertRaises(frappe.ValidationError):
			self._deal(lead.name)

	def test_a_lead_at_the_conversion_stage_converts(self):
		lead = self._lead(stage=self.bought)
		self.assertTrue(self._deal(lead.name).name, "a lead at the buying stage was refused a deal")

	def test_an_existing_deal_survives_its_lead_moving_off_the_conversion_stage(self):
		"""The gate is a BIRTH event. Re-judging it on every save would freeze every deal already in the book."""
		lead = self._lead(stage=self.bought)
		deal = self._deal(lead.name)
		frappe.db.set_value("CRM Lead", lead.name, "custom_substage", self.browsing)
		deal.save(ignore_permissions=True)
		self.assertTrue(deal.name, "an existing deal became unsaveable because its lead moved stage")

	# ---- renewal is arithmetic ---------------------------------------------------------------------

	def test_renewal_date_is_the_start_plus_the_sku_duration(self):
		lead = self._lead()
		deal = self._deal(lead.name, products=[self._line(self.term_sku)])
		self.assertEqual(
			getdate(deal.products[0].custom_renewal_date), getdate(add_days(START, TERM_DAYS)),
			"the renewal date is not the plan's duration applied to the line's start date")

	def test_a_sku_with_no_duration_leaves_the_renewal_blank(self):
		"""An open-ended sale has no renewal date, and asking for one must not invent a day or raise."""
		lead = self._lead()
		deal = self._deal(lead.name, products=[self._line(self.open_sku)])
		self.assertIsNone(deal.products[0].custom_renewal_date,
						  "an open-ended sale was given a renewal date it does not have")

	def test_a_typed_renewal_date_is_overwritten(self):
		"""The proof the field is DERIVED: a date typed on the row loses to the one the SKU implies."""
		lead = self._lead()
		deal = self._deal(lead.name, products=[self._line(self.term_sku, renewal=TYPED)])
		self.assertEqual(
			getdate(deal.products[0].custom_renewal_date), getdate(add_days(START, TERM_DAYS)),
			"a hand-typed renewal date survived, so the deal renews on a day the plan never bought")

	# ---- the phone ---------------------------------------------------------------------------------

	def test_a_deal_phone_is_stored_canonical(self):
		"""The hook is called directly: stock crm's own validate re-reads these two fields off the primary
		contact (and blanks them when there is none), so an inserted deal proves the fork, not this rule."""
		deal = frappe.new_doc("CRM Deal")
		deal.mobile_no = "09876543210"
		normalize_deal_phones(deal)
		self.assertEqual(deal.mobile_no, "+919876543210",
						 "a number was stored in a form no inbound message or call can match")

	# ---- dormant -----------------------------------------------------------------------------------

	def test_every_guard_is_dormant_when_the_switch_is_off(self):
		"""The switch ships off, and off it is stock crm: no lead, no armed line, no duplicate check, no
		conversion stage to reach, and a renewal date that is whatever the row was given."""
		self._arm("Nothing::Is::On")
		lead = self._lead(vertical=UNARMED, stage=self.browsing)
		self._deal(None)
		first = self._deal(lead.name, products=[self._line(self.term_sku, renewal=TYPED)])
		second = self._deal(lead.name)
		self.assertEqual(self._grain_of(first), (None, None, None),
						 "the grain was stamped while the switch was off")
		self.assertTrue(second.name, "a second deal was refused while the switch was off")
		self.assertEqual(getdate(first.products[0].custom_renewal_date), getdate(TYPED),
						 "the renewal date was recomputed while the switch was off")
