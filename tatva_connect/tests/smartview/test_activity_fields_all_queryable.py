# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Phase 4 of docs/plans/task-form-layer/2026-07-25-task-slots-to-sections-and-form-layer.md: every declared field is a
real column somewhere, so every declared field can be filtered and sorted.

Before this phase an activity field that named none of the 9 promoted CRM Task columns lived in the JSON
payload, reachable only through `JSON_EXTRACT` — the catalog marked it `filterable=0, sortable=0,
surface="detail"` and no amount of UI work could change that. Measured on the UAT replay: 418 declared
fields across 61 types, 160 filterable. The other 258 could be looked at and nothing else.

Phase 2 gave every answer a second home and Phase 3 moved the history into it, so the composer can now
resolve a field the way the writer wrote it: `field_target` names a retained common CRM Task column or a
section row, and both are columns a WHERE can reach. This asserts the three properties that follow:

  * EVERY declared field is offered as a column AND as a filter, whichever shape it routes to;
  * a filter on a field that used to be payload-only really narrows the result set, from an exact N to an
    exact M — not "the query ran", but the two tasks became one;
  * a section is joined ONCE however many of its fields the view projects (D3), so a wide view costs
    joins per section and not per field.

D17 is asserted behaviourally, on values whose lexical and chronological order DISAGREE: a Datetime
answer is compared in `value_datetime` and read from the column the section declares, so a range filter
selects by date. Compared as text the same filter selects nothing and the same sort comes back reversed —
which is exactly what the two assertions here would catch.

Nothing here names a section key, a child table or a storage column: the fixture reads the declaration for
the shapes it needs, exactly as the Phase 2 and Phase 3 tests do.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \\
        --module tatva_connect.tests.smartview.test_activity_fields_all_queryable
"""
import frappe
from frappe.query_builder import DocType
from frappe.tests.utils import FrappeTestCase

from tatva_connect.activity import api as activity_api
from tatva_connect.activity import backfill
from tatva_connect.smartview import api as smartview
from tatva_connect.tests.activity import task_type_fixture

TYPE_NAME = "ZZ Queryable Probe"
VIEW_LABEL = "ZZ Queryable View"
PHONE_PREFIX = "+91610007"

# A slot the plan retires, and a common column it keeps — the fixture must MEAN one of each.
DYING_SLOT = "custom_key_date_1"
RETAINED_COMMON = "custom_outcome"

# Two moments whose lexical order is the REVERSE of their chronological one, which is the whole of what a
# typed column buys: as text "2026-10-01..." sorts before "2026-9-1...", as dates it sorts after.
EARLY = "2026-9-1 10:00:00"
LATE = "2026-10-1 10:00:00"
# A range holding EARLY and not LATE. Compared as text it holds NEITHER: "2026-9-1" is lexically past
# "2026-09-15", so the row a user picked the date from disappears.
RANGE = ["2026-08-15 00:00:00", "2026-09-15 00:00:00"]

ALPHA = "ZZ alpha remark"
BETA = "ZZ beta remark"


def _column_section(count):
	"""A section whose rows carry real named columns, and `count` of those columns — read off the
	declaration, so this test names neither a section key nor a child table nor a storage column."""
	for section in frappe.get_all(
		"CRM Task Section",
		filters={"is_key_value": 0, "is_multi_row": 0},
		fields=["name", "target_doctype", "child_table_field"],
		order_by="display_order",
	):
		columns = [f.fieldname for f in frappe.get_meta(section.target_doctype).fields if f.fieldtype == "Data"]
		if len(columns) >= count:
			return section, columns[:count]
	return None, []


class TestActivityFieldsAllQueryable(FrappeTestCase):
	"""One type, one field of every routed shape, two tasks — and a filter that really picks one of them."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		cls._purge()
		cls.column_section, cls.columns = _column_section(3)
		cls.task_type = task_type_fixture.mint_type(TYPE_NAME, [
			# Rule 2 — a retained common CRM Task column: the task row itself.
			{"label": "ZZ Q Outcome", "fieldname": "zz_q_outcome", "fieldtype": "Data", "target": RETAINED_COMMON},
			# Rule 3 — no target at all: the JSON payload yesterday, an answer row of its own name today.
			{"label": "ZZ Q Remark", "fieldname": "zz_q_remark", "fieldtype": "Small Text"},
			# Rule 3 — a dying slot, declared Datetime: the same answer row, plus a column a date compares in.
			{"label": "ZZ Q Sample Collected", "fieldname": "zz_q_sample_collected",
			 "fieldtype": "Datetime", "target": DYING_SLOT},
			# Rule 1 — three real columns of ONE section's target doctype, so a join per field would show.
			{"label": "ZZ Q Col A", "fieldname": "zz_q_col_a", "fieldtype": "Data",
			 "section": cls.column_section.name, "target": cls.columns[0]},
			{"label": "ZZ Q Col B", "fieldname": "zz_q_col_b", "fieldtype": "Data",
			 "section": cls.column_section.name, "target": cls.columns[1]},
			{"label": "ZZ Q Col C", "fieldname": "zz_q_col_c", "fieldtype": "Data",
			 "section": cls.column_section.name, "target": cls.columns[2]},
		])
		cls.alpha = cls._activity(f"{PHONE_PREFIX}0001", ALPHA, EARLY, "ZZ Reached")
		cls.beta = cls._activity(f"{PHONE_PREFIX}0002", BETA, LATE, "ZZ Not Reached")
		cls.view = frappe.get_doc({
			"doctype": "CRM Smart View", "label": VIEW_LABEL, "base_object": "Activity",
			"activity_type": cls.task_type, "is_standard": 1,
			"vertical": task_type_fixture.VERTICAL, "group": task_type_fixture.GROUP,
			"columns": frappe.as_json(sorted(cls._catalog())),
		}).insert(ignore_permissions=True).name
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		cls._purge()
		task_type_fixture.teardown()
		frappe.db.commit()
		super().tearDownClass()

	@classmethod
	def _purge(cls):
		for name in frappe.get_all("CRM Smart View", filters={"label": VIEW_LABEL}, pluck="name"):
			frappe.delete_doc("CRM Smart View", name, force=True, ignore_permissions=True)
		for lead in frappe.get_all("CRM Lead", filters={"mobile_no": ["like", f"{PHONE_PREFIX}%"]}, pluck="name"):
			for task in frappe.get_all("CRM Task", filters={"reference_docname": lead}, pluck="name"):
				frappe.delete_doc("CRM Task", task, force=True, ignore_permissions=True)
			frappe.delete_doc("CRM Lead", lead, force=True, ignore_permissions=True)

	@classmethod
	def _activity(cls, phone, remark, collected, outcome):
		"""A lead on the minted grain and one saved activity on it — through the writer a rep uses, so the
		values under test are the ones a real save leaves behind."""
		lead = frappe.get_doc({
			"doctype": "CRM Lead", "first_name": "Queryable Probe", "mobile_no": phone, "status": "New",
			"custom_vertical": task_type_fixture.VERTICAL, "custom_group": task_type_fixture.GROUP,
		}).insert(ignore_permissions=True).name
		return activity_api.save_activity(lead, cls.task_type, {
			"zz_q_outcome": outcome,
			"zz_q_remark": remark,
			"zz_q_sample_collected": collected,
			"zz_q_col_a": remark, "zz_q_col_b": remark, "zz_q_col_c": remark,
		})

	@classmethod
	def _catalog(cls):
		return smartview._activity_catalog(cls.task_type)

	def setUp(self):
		self.addCleanup(frappe.db.rollback)
		frappe.set_user("Administrator")

	# ---- the premise ------------------------------------------------------------------------------

	def test_the_probe_carries_a_field_of_every_routed_shape(self):
		"""A fixture missing a shape would prove queryability for the shapes it happens to carry, no more."""
		self.assertIsNotNone(self.column_section, "no section declares 3 named columns — rule 1 is untestable")
		self.assertIn(DYING_SLOT, backfill.PROMOTED_COLUMNS,
					  f"`{DYING_SLOT}` was never a promoted column — it is no longer a dying slot")
		self.assertNotIn(DYING_SLOT, activity_api.COMMON_COLUMNS,
						 f"`{DYING_SLOT}` is retained — pick a slot the plan actually dropped")
		self.assertIn(RETAINED_COMMON, activity_api.COMMON_COLUMNS,
					  f"`{RETAINED_COMMON}` is not a retained common column — rule 2 is untestable")
		self.assertLess(LATE, EARLY, "the two moments no longer disagree lexically — D17 is untestable")

	# ---- every field is offered, as a column AND as a filter ---------------------------------------

	def test_every_declared_field_is_offered_as_a_column_and_as_a_filter(self):
		"""THE phase. Before it, four of these six were `filterable=0, sortable=0, surface="detail"`."""
		offered = {r["field_key"]: r for r in smartview.field_catalog(
			base_object="Activity", activity_type=self.task_type)}
		schema = activity_api.get_schema(self.task_type)
		self.assertEqual(set(offered), {f"activity:{f['fieldname']}" for f in schema},
						 "the picker no longer offers exactly the brain's schema")
		unfilterable = sorted(k for k, r in offered.items() if not r["filterable"])
		self.assertEqual(unfilterable, [], f"declared fields that still cannot be filtered: {unfilterable}")
		unsortable = sorted(k for k, r in offered.items() if not r["sortable"])
		self.assertEqual(unsortable, [], f"declared fields that still cannot be sorted: {unsortable}")
		detail_only = sorted(k for k, r in offered.items() if r["surface"] != "worklist")
		self.assertEqual(detail_only, [], f"declared fields still kept off the worklist: {detail_only}")

	def test_each_field_is_addressed_where_field_target_says_it_lives(self):
		"""The routing is asked of the ONE seam, never re-derived: a column the writer fills can never be
		a column the reader looks past."""
		cat = self._catalog()
		schema = {f["fieldname"]: f for f in activity_api.get_schema(self.task_type)}
		for fieldname, row in ((f, cat[f"activity:{f}"]) for f in schema):
			section_key, address = activity_api.field_target(schema[fieldname])
			self.assertEqual(row.fieldname, address, f"{fieldname} is addressed somewhere else")
			if section_key is None:
				self.assertEqual(row.target_doctype, smartview.TASK_DOCTYPE)
			else:
				self.assertEqual(
					row.target_doctype,
					frappe.db.get_value("CRM Task Section", section_key, "target_doctype"),
					f"{fieldname} reads from a doctype its section does not name")

	# ---- a filter on a former payload field really narrows -----------------------------------------

	def test_a_filter_on_a_former_payload_field_narrows_the_result_set(self):
		"""Not "the query ran": two tasks in, exactly one out, and it is the one that answered."""
		unfiltered = self._rows()
		self.assertEqual({r["name"] for r in unfiltered}, {self.alpha, self.beta},
						 "the view does not hold exactly the two probe tasks")

		rows = self._rows([["activity:zz_q_remark", "=", ALPHA]])
		self.assertEqual([r["name"] for r in rows], [self.alpha],
						 "a filter on a field that lived in the JSON payload did not narrow anything")
		self.assertEqual(rows[0]["activity:zz_q_remark"], ALPHA,
						 "the projected value is not the value the writer saved")

	def test_the_total_narrows_with_the_rows(self):
		"""The count is a second query over the same joins; a filter that narrows one and not the other
		gives a page of 1 row claiming there are 2."""
		self.assertEqual(self._total(), 2)
		self.assertEqual(self._total([["activity:zz_q_remark", "=", BETA]]), 1)

	def test_a_filter_on_a_retained_common_column_still_narrows(self):
		"""Rule 2 keeps the home it already had, so the phase must not have moved it."""
		rows = self._rows([["activity:zz_q_outcome", "=", "ZZ Reached"]])
		self.assertEqual([r["name"] for r in rows], [self.alpha])

	def test_a_filter_on_a_section_column_narrows(self):
		"""Rule 1: a real named column of the section's target doctype, filtered by its own name."""
		rows = self._rows([["activity:zz_q_col_a", "=", BETA]])
		self.assertEqual([r["name"] for r in rows], [self.beta])

	# ---- D17 — compared on the typed column, read from the declared one -----------------------------

	def test_a_date_range_on_a_datetime_answer_selects_by_DATE_not_by_TEXT(self):
		"""D17. The range holds the September answer and not the October one. Compared as text it holds
		NEITHER, because "2026-9-1" sorts past "2026-09-15" — so a text comparison returns zero rows here."""
		rows = self._rows([["activity:zz_q_sample_collected", "between", RANGE]])
		self.assertEqual([r["name"] for r in rows], [self.alpha],
						 "a date range compared the answer as text — the row a user picked the date from vanished")

	def test_sorting_a_datetime_answer_orders_by_DATE_not_by_TEXT(self):
		"""The same defect on the other axis: as text the October answer sorts FIRST, which is the exact
		reverse of the order a rep reads the column in."""
		self.assertEqual([r["name"] for r in self._rows(sort=["activity:zz_q_sample_collected", "asc"])],
						 [self.alpha, self.beta], "the sort compared the answer as text")
		self.assertEqual([r["name"] for r in self._rows(sort=["activity:zz_q_sample_collected", "desc"])],
						 [self.beta, self.alpha])

	def test_the_projected_value_is_the_declared_read_column(self):
		"""And the display side stays the section's declared value column — `value` is always populated, the
		typed column exists so a comparison is correct (D17), not so a screen reads a different answer."""
		rows = self._rows([["activity:zz_q_remark", "=", ALPHA]])
		self.assertEqual(rows[0]["activity:zz_q_sample_collected"], EARLY,
						 "the projection came from somewhere other than the column the section declares")

	# ---- one join per SECTION, not one per field ---------------------------------------------------

	def test_a_section_is_joined_once_however_many_of_its_fields_are_projected(self):
		"""D3, the reason a section beats a key-value row per field: three fields of one section cost ONE
		join. A join per field is what puts a wide view against MariaDB's 61-table ceiling."""
		one = self._join_count(["activity:zz_q_col_a"])
		three = self._join_count(["activity:zz_q_col_a", "activity:zz_q_col_b", "activity:zz_q_col_c"])
		self.assertEqual(one, 1, "a single section field did not resolve to exactly one join")
		self.assertEqual(three, 1, f"three fields of ONE section produced {three} joins — one per field")

	def test_a_retained_common_column_needs_no_join_at_all(self):
		"""Rule 2 is the driving row itself."""
		self.assertEqual(self._join_count(["activity:zz_q_outcome"]), 0)

	def test_a_key_value_answer_costs_the_join_per_field_D3_declares(self):
		"""The other half of D3, asserted so it stays a KNOWN cost rather than a surprise: a key-value
		section holds one row per field, so each field it answers for is addressed by its own join."""
		self.assertEqual(self._join_count(["activity:zz_q_remark"]), 1)
		self.assertEqual(self._join_count(["activity:zz_q_remark", "activity:zz_q_sample_collected"]), 2)

	# ---- helpers -----------------------------------------------------------------------------------

	def _rows(self, filters=None, sort=None):
		return smartview.get_data(
			self.view, filters=frappe.as_json(filters or []),
			sort=frappe.as_json(sort) if sort else None, page_size=50,
		)["rows"]

	def _total(self, filters=None):
		return smartview.get_data(self.view, filters=frappe.as_json(filters or []), page_size=50)["total"]

	def _join_count(self, keys):
		"""The LEFT JOINs the composer really emits for these columns, read off the generated SQL."""
		table = DocType(smartview.TASK_DOCTYPE)
		apply_joins, field_terms, _compare = smartview._joins(
			set(keys), self._catalog(), table, smartview.TASK_DOCTYPE)
		query = apply_joins(frappe.qb.from_(table).select(*[field_terms[k].as_(k) for k in keys]))
		return query.get_sql().lower().count("left join")
