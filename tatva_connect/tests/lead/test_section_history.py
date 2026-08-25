# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A multi-row section opens its ROWS; a key-value question opens its answers.

Four defects are locked here, all of them proven RED before the fix:

  1. `section_history` split the key on `#` only. A catalogued key is `section:fieldname`, so
     `acq:utm_campaign` was read as a section named "acq:utm_campaign" and `get_cached_doc` raised —
     the endpoint 500ed instead of answering or refusing. There is now ONE parse, and the SECTION's own
     shape decides what its detail is; the separator decides nothing.
  2. `has_more` — a fact about the SECTION ("this child table keeps three rows") — was attached to every
     FIELD, so all 17 lab measurements grew a More button and each opened one column of the same three
     rows. A section is a child table, so `multi_row`/`row_count` are served once per section and the
     detail behind them is the TABLE (`lead_detail_rows`): every column, one line per row.
  3. `hideEmpty` is ON by default and drops a field the server calls empty. `empty` now means empty in
     EVERY row the field is kept in, so a field blank on the latest row but filled earlier still shows.
  4. The rows reader must not grow a second column brain: columns are derived from the child doctype's
     own meta, so a section that grows a field grows a column and `in_list_view` decides nothing.

Both readers are gated by the panel's own gates — `_select` for a field, the section set it yields for a
table — so neither can answer something the panel declined to show, and neither invents an ordering:
`multirow.sorted_child_rows` is the same rule whose head the panel already displays.

The `acq` section is the vehicle: it is multi-row on `touch_at` and `acq:utm_campaign` is a catalog row
`lead_sync/catalog_seed.py` guarantees, so nothing here asserts an operator's seed.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \
        --module tatva_connect.tests.lead.test_section_history
"""
import frappe
from frappe.model import NO_VALUE_FIELDS
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

	def test_a_tie_on_the_key_falls_to_idx_then_name(self):
		"""RED before the fix, and what a rep saw: `creation` was the tiebreak, but frappe stamps a child
		row with its PARENT's creation, so every row of one lead carries the same timestamp and the tie fell
		through to `name` — a random id. A patient's newest punch ranked below an older one whenever the ids
		sorted that way. `idx` is frappe's own record of which row was appended later."""
		a = _row(report_date="2026-01-10", idx=1, name="aaa")
		b = _row(report_date="2026-01-10", idx=2, name="zzz")
		c = _row(report_date="2026-01-10", idx=3, name="mmm")
		self.assertEqual([r.name for r in multirow.sorted_child_rows([a, b, c], "report_date")],
		                 ["mmm", "zzz", "aaa"])

	def test_idx_is_compared_as_a_number_not_as_text(self):
		"""Read as text, row 10 sorts below row 9 — and the DB, which orders it numerically, would then
		disagree with the Python sorter about which row is current."""
		rows = [_row(report_date="2026-01-10", idx=i, name=f"r{i}") for i in (2, 9, 10)]
		self.assertEqual(multirow.latest_child_row(rows, "report_date").idx, 10)

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

	def test_the_python_sorter_and_the_db_order_by_name_the_same_fields_in_the_same_order(self):
		"""The divergence lock: `order_keys` is the ONE declaration and both renderings are built from
		it, so a Python reader and a DB reader cannot disagree about which row is newer."""
		self.assertEqual(multirow.order_keys("report_date"), ("report_date", "idx", "name"))
		self.assertEqual(multirow.order_by("report_date"),
		                 ", ".join(f"{f} desc" for f in multirow.order_keys("report_date")))

	def test_a_section_with_no_row_key_falls_to_idx_then_name(self):
		self.assertEqual(multirow.order_keys(""), ("idx", "name"))
		self.assertNotIn("`", multirow.order_by("report_date"), "frappe rejects backticked order_by")

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


class TestSectionRowsEndpoint(FrappeTestCase):
	"""The endpoint, driven exactly as `View more` drives it: (lead, the section key the panel served)."""

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
		# Tick the multi-row field under test AND one parent field (lead:status): the panel shows a field
		# only where the LEAD's grain is covered by a contract ticking it, and a contract this narrow makes
		# every OTHER field non-universal — so the parent-field case (test_a_parent_section_field_never_
		# offers_more) needs its subject granted here, exactly as the multi-row field is.
		cls.contract = frappe.get_doc({
			"doctype": "CRM Lead API Mapping", "contract_name": "ZZ Section History Contract", "enabled": 1,
			"is_internal": 1, "vertical": partner_fixture.VERTICAL, "crm_group": partner_fixture.GROUP,
			"allowed_fields": [{"field": FIELD_KEY}, {"field": "lead:status"}],
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

	def _panel_section(self, key):
		return {s["key"]: s for s in detail.lead_detail(self.lead.name)["sections"]}.get(key)

	# -- the rows are the detail -----------------------------------------------

	def test_a_multi_row_section_answers_its_whole_table(self):
		out = detail.lead_detail_rows(self.lead.name, SECTION)
		self.assertEqual({"label", "row_key", "columns", "data", "page_length", "page_length_count",
		                  "row_count", "total_count"}, set(out),
		                 "the Leads list envelope, copied not adapted (C2)")
		self.assertEqual(out["total_count"], 3)
		self.assertEqual(out["row_count"], 3)
		self.assertEqual(out["row_key"], self.section.row_key_field)

	def test_rows_are_newest_first_and_open_on_the_row_the_panel_shows(self):
		"""One ordering: the first line of the table IS the line the panel flattened to."""
		rows = detail.lead_detail_rows(self.lead.name, SECTION)["data"]
		self.assertEqual([r[FIELDNAME] for r in rows], ["summer-push", "spring-push", "winter-push"])
		self.assertEqual(rows[0][FIELDNAME], self._panel_field(FIELD_KEY)["value"])

	def test_every_column_of_the_child_doctype_is_a_column(self):
		"""The column lock: derived from the child doctype's meta, never `in_list_view` (4 of 26) and
		never a list kept in code. A section that grows a field grows a column."""
		out = detail.lead_detail_rows(self.lead.name, SECTION)
		served = {c["key"] for c in out["columns"]}
		meta = frappe.get_meta(self.section.target_doctype)
		expected = {df.fieldname for df in meta.fields
		            if df.fieldtype not in NO_VALUE_FIELDS and not df.hidden}
		self.assertEqual(served, expected)
		in_list_view = {df.fieldname for df in meta.fields if df.in_list_view}
		self.assertGreater(len(served), len(in_list_view), "the reader is not the in_list_view subset")

	def test_the_row_key_is_the_first_column(self):
		"""It is what a reader scans down, so it leads — never buried at the doctype's own idx."""
		out = detail.lead_detail_rows(self.lead.name, SECTION)
		self.assertEqual(out["columns"][0]["key"], self.section.row_key_field)

	def test_a_column_carries_only_what_it_takes_to_render_sort_and_filter_it(self):
		"""The server sends the table and nothing about which of it to SHOW: narrowing is the column
		picker's, exactly as on a listing page. `sortable` and `filterable` are not that decision —
		`sortable` is a fact about the data (D5) and `filterable` a fact about the column (a multi-value
		field is not addressable in SQL, so it can reach neither WHERE nor ORDER BY). Each gates its own
		control and neither hides the column."""
		for column in detail.lead_detail_rows(self.lead.name, SECTION)["columns"]:
			self.assertEqual({"key", "label", "fieldtype", "options", "sortable", "filterable"},
			                 set(column))

	def test_a_column_null_on_every_row_is_not_offered_as_a_sort(self):
		"""RED before the fix: SortBy was fed all 31 columns, so sorting by one that is null everywhere
		ordered by the tiebreaker while looking authoritative. It stays a column and stays filterable."""
		columns = {c["key"]: c for c in detail.lead_detail_rows(self.lead.name, SECTION)["columns"]}
		self.assertTrue(columns[FIELDNAME]["sortable"], "filled on every row of this lead")
		self.assertFalse(columns["utm_source"]["sortable"], "null on every row of this lead")
		self.assertIn("utm_source", columns, "still a column, and still filterable")

	def test_a_stored_zero_counts_as_filled(self):
		"""One answer about emptiness: `multirow.is_blank` calls 0 a real value, and COUNT(col) counts
		non-NULL, so a measurement recorded as 0 stays sortable. Neither is a copy, so they cannot drift."""
		self.assertFalse(multirow.is_blank(0))

	def test_search_does_not_match_a_measurement_by_coincidence_of_digits(self):
		"""RED before the fix: a leading-wildcard LIKE ran on every column, so `%7%` hit a triglyceride
		of 178. Search covers the text fieldtypes; a Float, an Int and a Date are not text."""
		columns = detail.lead_detail_rows(self.lead.name, SECTION)["columns"]
		searched = detail._row_search(columns, "7")
		for key in searched:
			fieldtype = next(c["fieldtype"] for c in columns if c["key"] == key)
			self.assertIn(fieldtype, detail._SEARCHABLE_FIELDTYPES)
		self.assertTrue(searched, "the text columns are still searched")

	def test_a_column_carries_its_real_fieldtype(self):
		"""The client formats a Date as a date and sizes a column by its type; it is told, never guesses."""
		by_key = {c["key"]: c for c in detail.lead_detail_rows(self.lead.name, SECTION)["columns"]}
		df = frappe.get_meta(self.section.target_doctype).get_field(self.section.row_key_field)
		self.assertEqual(by_key[self.section.row_key_field]["fieldtype"], df.fieldtype)

	# -- search and paging -----------------------------------------------------

	def test_search_narrows_the_rows_and_the_total_with_them(self):
		"""C7: the count carries the SAME narrowing as the page, or '1 of 3' contradicts the screen."""
		out = detail.lead_detail_rows(self.lead.name, SECTION, search="spring")
		self.assertEqual(out["total_count"], 1)
		self.assertEqual([r[FIELDNAME] for r in out["data"]], ["spring-push"])

	def test_search_that_matches_nothing_is_empty_not_everything(self):
		out = detail.lead_detail_rows(self.lead.name, SECTION, search="no-such-campaign")
		self.assertEqual(out["data"], [])
		self.assertEqual(out["total_count"], 0)

	def test_sorting_is_an_allowlist_over_the_served_columns(self):
		"""D3: a caller's order_by is never a passthrough. A served column sorts; anything else refuses."""
		asc = detail.lead_detail_rows(self.lead.name, SECTION, order_by=f"{FIELDNAME} asc")
		self.assertEqual([r[FIELDNAME] for r in asc["data"]],
		                 ["spring-push", "summer-push", "winter-push"])
		for bad in ("parent desc", "utm_campaign; drop", f"{FIELDNAME} sideways"):
			with self.subTest(order_by=bad), self.assertRaises(frappe.exceptions.ValidationError):
				detail.lead_detail_rows(self.lead.name, SECTION, order_by=bad)

	def test_filtering_is_an_allowlist_over_the_served_columns(self):
		"""The filter dict is frappe's own and goes to the query untouched — but only for a column the
		caller was served, so `parent` cannot be used to read another lead's rows."""
		hit = detail.lead_detail_rows(self.lead.name, SECTION, filters={FIELDNAME: "spring-push"})
		self.assertEqual(hit["total_count"], 1)
		with self.assertRaises(frappe.exceptions.ValidationError):
			detail.lead_detail_rows(self.lead.name, SECTION, filters={"parent": "some-other-lead"})

	def test_a_window_smaller_than_the_table_still_reports_the_whole_total(self):
		"""The footer reads 'rows of total'; the total is the answer to the question, not the window."""
		out = detail.lead_detail_rows(self.lead.name, SECTION, page_length=2)
		self.assertEqual(len(out["data"]), 2)
		self.assertEqual(out["row_count"], 2, "row_count is this page (C2)")
		self.assertEqual(out["total_count"], 3, "total_count is the whole answer (C2)")
		self.assertEqual(out["page_length"], 2, "the window is echoed back, so the client holds no copy")
		self.assertEqual(out["data"][0][FIELDNAME], "summer-push", "the window is still newest-first")

	# -- the section, not the field, carries `more` ----------------------------

	def test_the_panel_offers_more_once_per_section_not_once_per_field(self):
		"""RED before the fix: `has_more` was on every FIELD, so all 17 lab measurements grew a More
		button. It is a fact about the child table, so it is served once, on the section."""
		section = self._panel_section(SECTION)
		self.assertIsNotNone(section, "acq must be in the panel for an entitled viewer")
		self.assertTrue(section["multi_row"])
		self.assertEqual(section["row_count"], 3)
		for field in section["fields"]:
			self.assertNotIn("has_more", field, "a multi-row field opens nothing of its own")

	def test_a_parent_section_is_never_multi_row(self):
		section = self._panel_section("lead")
		self.assertIsNotNone(section)
		self.assertFalse(section["multi_row"], "the lead row holds one value; More would open on itself")
		self.assertEqual(section["row_count"], 0)

	def test_a_single_row_of_a_multi_row_section_reports_that_one_row(self):
		lead = frappe.get_doc("CRM Lead", self.lead.name)
		lead.set(self.section.child_table_field, lead.get(self.section.child_table_field)[:1])
		lead.save(ignore_permissions=True)
		self.assertEqual(self._panel_section(SECTION)["row_count"], 1)

	# -- hideEmpty -------------------------------------------------------------

	def test_a_field_blank_on_the_latest_row_shows_the_answer_the_lead_still_has(self):
		"""Two fixes, one field. `empty` was `_is_empty(value)` on the LATEST row alone, so the panel (with
		hideEmpty ON by default) dropped the field and the door to its history with it. The VALUE was read
		the same narrow way and showed a hole, which is what `multirow.current_for_section` now closes: the
		flag and the value are two halves of one rule and must not disagree."""
		lead = frappe.get_doc("CRM Lead", self.lead.name)
		rows = multirow.sorted_child_rows(lead.get(self.section.child_table_field), self.section.row_key_field)
		rows[0].set(FIELDNAME, "")
		lead.save(ignore_permissions=True)
		field = self._panel_field(FIELD_KEY)
		self.assertEqual(field["value"], rows[1].get(FIELDNAME), "the newest row that HAS an answer")
		self.assertFalse(field["empty"], "and the flag that would hide it agrees")

	def test_a_field_blank_in_every_row_stays_empty(self):
		"""The tidiness half: widening `empty` must not drag every never-filled field onto the panel."""
		lead = frappe.get_doc("CRM Lead", self.lead.name)
		for child in lead.get(self.section.child_table_field):
			child.set(FIELDNAME, "")
		lead.save(ignore_permissions=True)
		self.assertTrue(self._panel_field(FIELD_KEY)["empty"])

	# -- the gate --------------------------------------------------------------

	def test_rows_refuse_a_section_the_panel_never_opened(self):
		"""Gated on the section set `_select` yields — a section this viewer was shown no field of is
		refused, not answered, so the table cannot be read around the panel that hides it."""
		with self.assertRaises(frappe.exceptions.PermissionError):
			detail.lead_detail_rows(self.lead.name, "plan")

	def test_rows_refuse_a_section_that_does_not_exist(self):
		with self.assertRaises(frappe.exceptions.PermissionError):
			detail.lead_detail_rows(self.lead.name, "no_such_section")

	def test_rows_are_refused_on_a_lead_the_caller_cannot_read(self):
		victim = _ensure_plain_user()
		frappe.set_user(victim)
		try:
			with self.assertRaises(frappe.exceptions.PermissionError):
				detail.lead_detail_rows(self.lead.name, SECTION)
		finally:
			frappe.set_user("Administrator")

	# -- the reader that keeps none -------------------------------------------

	def test_a_multi_row_field_is_refused_its_own_history(self):
		"""RED before the fix: a multi-row field answered one COLUMN of its table, which is the defect —
		a table's detail is its rows. The refusal must be DELIBERATE, which is what excluding
		DoesNotExistError asserts (a 500 dressed as a validation error reads the same to a caller)."""
		with self.assertRaises(frappe.exceptions.ValidationError) as caught:
			detail.section_history(self.lead.name, FIELD_KEY)
		self.assertNotIsInstance(caught.exception, frappe.DoesNotExistError)

	def test_a_single_row_section_says_it_keeps_no_history(self):
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
