# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The More button opens the history of ANY multi-row section, not only screening.

Three defects are locked here, all of them proven RED before the fix:

  1. `section_history` split the key on `#` only. A catalogued key is `section:fieldname`, so
     `acq:utm_campaign` was read as a section named "acq:utm_campaign" and `get_cached_doc` raised —
     the endpoint 500ed instead of answering or refusing. There is now ONE parse, and the SECTION's own
     shape (`is_key_value` vs `is_multi_row`) decides which history it is; the separator decides nothing.
  2. `has_more` was set only for a key-value section, so a lab/drug/acquisition field never offered its
     history at all. It is now set in the main loop, off the section brain.
  3. `hideEmpty` is ON by default and drops a field the server calls empty, taking its More button with
     it — so a field blank on the LATEST row but filled on an earlier one had its history made
     unreachable. `empty` now means empty in EVERY row the field is kept in.

The multi-row branch is gated by `_select` — the panel's own field gate — so the modal can never answer
a field the panel declined to show, and it does NOT invent an ordering: `multirow.sorted_child_rows` is
the same rule whose head the panel already displays.

The `acq` section is the vehicle: it is multi-row on `touch_at` and `acq:utm_campaign` is a catalog row
`lead_sync/catalog_seed.py` guarantees, so nothing here asserts an operator's seed.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \
        --module tatva_connect.tests.lead.test_section_history
"""
import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.lead import detail, multirow
from tatva_connect.lead_sync import catalog_seed
from tatva_connect.tests.api import partner_fixture

SECTION = "acq"
FIELD_KEY = "acq:utm_campaign"
FIELDNAME = "utm_campaign"
MOBILE = "+916100030001"


def _row(**kw):
	return frappe._dict(kw)


class TestSortedChildRows(FrappeTestCase):
	"""One ordering, two readers: the flattened value and the history cannot disagree."""

	def test_rows_come_back_newest_first(self):
		old = _row(report_date="2026-01-10", creation="2026-01-10 09:00:00", name="aaa")
		mid = _row(report_date="2026-03-01", creation="2026-03-01 09:00:00", name="bbb")
		new = _row(report_date="2026-06-01", creation="2026-06-01 09:00:00", name="ccc")
		ordered = multirow.sorted_child_rows([mid, old, new], "report_date")
		self.assertEqual([r.name for r in ordered], ["ccc", "bbb", "aaa"])

	def test_a_tie_on_the_key_falls_to_creation_then_name(self):
		a = _row(report_date="2026-01-10", creation="2026-01-10 09:00:00", name="aaa")
		b = _row(report_date="2026-01-10", creation="2026-01-10 09:00:00", name="zzz")
		c = _row(report_date="2026-01-10", creation="2026-01-10 18:00:00", name="mmm")
		self.assertEqual([r.name for r in multirow.sorted_child_rows([a, b, c], "report_date")],
		                 ["mmm", "zzz", "aaa"])

	def test_latest_child_row_is_exactly_the_head_of_the_sorted_list(self):
		"""B7's divergence lock: `latest_child_row` is DEFINED as the head, so a change to either rule
		cannot leave the panel showing one row while the modal calls another one current."""
		rows = [
			_row(report_date="2026-01-10", creation="2026-01-10 09:00:00", name="aaa"),
			_row(report_date="2026-06-01", creation="2026-01-01 00:00:00", name="zzz"),
			_row(report_date="2026-06-01", creation="2026-05-01 00:00:00", name="bbb"),
		]
		for order in ([0, 1, 2], [2, 1, 0], [1, 0, 2]):
			shuffled = [rows[i] for i in order]
			with self.subTest(order=order):
				self.assertIs(multirow.latest_child_row(shuffled, "report_date"),
				              multirow.sorted_child_rows(shuffled, "report_date")[0])

	def test_no_rows_is_an_empty_list_and_no_latest(self):
		self.assertEqual(multirow.sorted_child_rows([], "report_date"), [])
		self.assertEqual(multirow.sorted_child_rows(None, "report_date"), [])
		self.assertIsNone(multirow.latest_child_row([], "report_date"))


class TestParseFieldKey(FrappeTestCase):
	"""ONE parse for both shapes — the separator names nothing, the section does."""

	def test_a_catalogued_key_yields_its_section_not_the_whole_key(self):
		self.assertEqual(detail.parse_field_key(FIELD_KEY), (SECTION, FIELDNAME))

	def test_a_key_value_key_yields_its_section_and_identity(self):
		self.assertEqual(detail.parse_field_key("screening#abc123"), ("screening", "abc123"))

	def test_a_bare_section_yields_no_member(self):
		self.assertEqual(detail.parse_field_key("lead"), ("lead", ""))


class TestEmptyEverywhere(FrappeTestCase):
	def test_blank_on_the_latest_row_but_filled_earlier_is_not_empty(self):
		self.assertFalse(detail.empty_everywhere([None, "spring-campaign"]))

	def test_blank_in_every_row_is_empty(self):
		self.assertTrue(detail.empty_everywhere([None, "", "   "]))

	def test_zero_is_a_real_value(self):
		self.assertFalse(detail.empty_everywhere([0]))


class TestMultiRowHistoryEndpoint(FrappeTestCase):
	"""The endpoint, driven exactly as the More button drives it: (lead, the key the panel served)."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		if not frappe.db.exists("CRM Lead API Field", FIELD_KEY):
			catalog_seed.ensure_rows()  # the app's own after_migrate seed; a no-op on any migrated site
		cls.section = frappe.get_cached_doc("CRM Lead Section", SECTION)
		assert cls.section.is_multi_row, "this suite needs `acq` to be the multi-row section it is seeded as"
		# The panel serves a field only where the LEAD's grain is covered by a contract ticking it. A lead
		# with no grain is covered by none, so the fixture mints both rather than asserting an empty panel.
		partner_fixture.mint_grain()
		cls.contract = frappe.get_doc({
			"doctype": "CRM Lead API Mapping", "contract_name": "ZZ Section History Contract", "enabled": 1,
			"is_internal": 1, "vertical": partner_fixture.VERTICAL, "crm_group": partner_fixture.GROUP,
			"allowed_fields": [{"field": FIELD_KEY}],
		}).insert(ignore_permissions=True).name
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		if frappe.db.exists("CRM Lead API Mapping", cls.contract):
			frappe.delete_doc("CRM Lead API Mapping", cls.contract, force=True, ignore_permissions=True)
		partner_fixture.teardown()
		frappe.db.commit()
		super().tearDownClass()

	def setUp(self):
		frappe.set_user("Administrator")
		self.lead = frappe.get_doc({
			"doctype": "CRM Lead",
			"first_name": "History",
			"last_name": "Probe",
			"mobile_no": MOBILE,
			"custom_vertical": partner_fixture.VERTICAL,
			"custom_group": partner_fixture.GROUP,
			self.section.child_table_field: [
				{"touch_at": "2026-01-10 09:00:00", FIELDNAME: "winter-push"},
				{"touch_at": "2026-03-01 09:00:00", FIELDNAME: "spring-push"},
				{"touch_at": "2026-06-01 09:00:00", FIELDNAME: "summer-push"},
			],
		}).insert(ignore_permissions=True)

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.delete_doc("CRM Lead", self.lead.name, force=True, ignore_permissions=True)

	def _panel_field(self, field_key):
		out = detail.lead_detail(self.lead.name)
		flat = {f["field_key"]: f for sec in out["sections"] for f in sec["fields"]}
		return flat.get(field_key)

	# -- the 500 ---------------------------------------------------------------

	def test_a_catalogued_multi_row_field_answers_its_history(self):
		"""RED before the fix: `partition('#')` made the section key the WHOLE `acq:utm_campaign`, so
		`get_cached_doc` raised DoesNotExistError and the More button 500ed."""
		out = detail.section_history(self.lead.name, FIELD_KEY)
		self.assertEqual({"label", "entries"}, set(out))
		self.assertEqual(len(out["entries"]), 3)

	def test_history_is_newest_first_and_opens_on_the_value_the_panel_shows(self):
		out = detail.section_history(self.lead.name, FIELD_KEY)
		self.assertEqual([e["value"] for e in out["entries"]],
		                 ["summer-push", "spring-push", "winter-push"])
		self.assertEqual(out["entries"][0]["value"], self._panel_field(FIELD_KEY)["value"])

	def test_every_entry_carries_the_one_shape(self):
		"""Both branches answer in the same five keys, so the modal renders one thing either way."""
		for entry in detail.section_history(self.lead.name, FIELD_KEY)["entries"]:
			self.assertEqual({"value", "display", "empty", "on", "source"}, set(entry))
		first = detail.section_history(self.lead.name, FIELD_KEY)["entries"][0]
		self.assertIsNone(first["source"], "a multi-row row is our own record and names no form")
		self.assertTrue(first["on"], "`on` is the row key — what dates this entry")

	def test_on_is_the_sections_row_key_not_the_row_creation(self):
		"""Rows written in one save share a `creation` to the second; the report/cycle/touch date is the
		fact the reader is dating the entry by, and it is read off the section brain."""
		ons = [str(e["on"]) for e in detail.section_history(self.lead.name, FIELD_KEY)["entries"]]
		self.assertTrue(ons[0].startswith("2026-06-01"))
		self.assertTrue(ons[-1].startswith("2026-01-10"))

	# -- has_more --------------------------------------------------------------

	def test_the_panel_offers_more_on_a_multi_row_field_with_a_second_row(self):
		"""RED before the fix: `has_more` was set ONLY inside `_screening_answers`, so the key was absent
		from every catalogued field and the More button never rendered for lab/drug/acq."""
		field = self._panel_field(FIELD_KEY)
		self.assertIsNotNone(field, "acq:utm_campaign must be in the panel for an entitled viewer")
		self.assertIn("has_more", field)
		self.assertTrue(field["has_more"])

	def test_a_parent_section_field_never_offers_more(self):
		field = self._panel_field("lead:status")
		self.assertIsNotNone(field)
		self.assertFalse(field["has_more"], "the lead row holds one value; More would open on itself")

	def test_a_single_row_of_a_multi_row_section_offers_no_more(self):
		lead = frappe.get_doc("CRM Lead", self.lead.name)
		lead.set(self.section.child_table_field, lead.get(self.section.child_table_field)[:1])
		lead.save(ignore_permissions=True)
		self.assertFalse(self._panel_field(FIELD_KEY)["has_more"])

	# -- hideEmpty -------------------------------------------------------------

	def test_a_field_blank_on_the_latest_row_survives_hide_empty(self):
		"""RED before the fix: `empty` was `_is_empty(value)` on the LATEST row alone, so the panel (with
		hideEmpty ON by default) dropped the field and the only door to its history with it."""
		lead = frappe.get_doc("CRM Lead", self.lead.name)
		latest = multirow.latest_child_row(lead.get(self.section.child_table_field), self.section.row_key_field)
		latest.set(FIELDNAME, "")
		lead.save(ignore_permissions=True)
		field = self._panel_field(FIELD_KEY)
		self.assertTrue(detail._is_empty(field["value"]), "the displayed value really is blank")
		self.assertFalse(field["empty"], "blank on the latest row but filled earlier is not 'nothing to show'")
		self.assertTrue(field["has_more"])

	def test_a_field_blank_in_every_row_stays_empty(self):
		"""The tidiness half: widening `empty` must not drag every never-filled field onto the panel."""
		lead = frappe.get_doc("CRM Lead", self.lead.name)
		for child in lead.get(self.section.child_table_field):
			child.set(FIELDNAME, "")
		lead.save(ignore_permissions=True)
		self.assertTrue(self._panel_field(FIELD_KEY)["empty"])

	# -- the gate --------------------------------------------------------------

	def test_history_refuses_a_field_the_panel_never_showed(self):
		"""Gated by `_select`, the panel's own field gate — an undeclared column of a real multi-row
		section is not a field this viewer was shown, so its history is refused, not answered."""
		with self.assertRaises(frappe.exceptions.PermissionError):
			detail.section_history(self.lead.name, f"{SECTION}:utm_term_not_catalogued")

	def test_history_is_refused_on_a_lead_the_caller_cannot_read(self):
		victim = _ensure_plain_user()
		frappe.set_user(victim)
		try:
			with self.assertRaises(frappe.exceptions.PermissionError):
				detail.section_history(self.lead.name, FIELD_KEY)
		finally:
			frappe.set_user("Administrator")

	# -- the sections that keep none ------------------------------------------

	def test_a_single_row_section_says_it_keeps_no_history(self):
		"""RED before the fix: `plan:plan_name` partitioned to a section named "plan:plan_name" and the
		lookup raised DoesNotExistError — a 500 dressed as a validation error, with no readable message.
		The refusal must be a DELIBERATE one, which is what excluding DoesNotExistError asserts."""
		with self.assertRaises(frappe.exceptions.ValidationError) as caught:
			detail.section_history(self.lead.name, "plan:plan_name")
		self.assertNotIsInstance(caught.exception, frappe.DoesNotExistError)

	def test_an_unknown_section_refuses_instead_of_blowing_up(self):
		with self.assertRaises(frappe.exceptions.ValidationError) as caught:
			detail.section_history(self.lead.name, "no_such_section:whatever")
		self.assertNotIsInstance(caught.exception, frappe.DoesNotExistError)


def _ensure_plain_user():
	email = "history.plainuser@example.com"
	if not frappe.db.exists("User", email):
		frappe.get_doc({
			"doctype": "User", "email": email, "first_name": "Plain",
			"send_welcome_email": 0, "roles": [],
		}).insert(ignore_permissions=True)
	return email
