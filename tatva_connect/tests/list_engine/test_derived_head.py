# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A derived field is an OPERATOR-AUTHORED ROW now — live on Save, no deploy, no migrate, no restart.

`due_state` shipped hardcoded in `fields.py`, so every second derived field was a code change. The head
makes the declaration a `CRM Derived Field` row: `dt`, `fieldname`, `label`, `order_by`, `surfaces`,
`default_column`, `enabled`, and the buckets as JSON — the same frappe filter tuples the engine already
hands to both of frappe's readers. Nothing here parses anything.

EVERY TEST BELOW ASSERTS THE PROMISE, NEVER THE CODE PATH. The promise is one sentence:

    the rows the list SHOWS as X are exactly the rows a filter on X RETURNS

Column, filter, sort, group-by and board are five restatements of it. The 2026-07-31 audit found almost
every real defect in this layer by looking at the screen while the suite stayed green, and the pattern was
always the same: the tests asserted that the mechanism RAN. So a row is authored the way an operator
authors one — through the doctype, so `validate` runs — and then the LIST is asked.

WHAT MAKES EACH TEST RED TODAY. There is no `CRM Derived Field` doctype, so every test that authors a row
raises `DoesNotExistError` on today's code. That is the shallow reason and it stops applying the moment the
doctype lands; each class names the deeper one it keeps failing on until the behaviour is really there.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \
        --module tatva_connect.tests.list_engine.test_derived_head
"""

import copy
import pathlib
import re
import typing

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_to_date, now_datetime, nowdate

from tatva_connect.api import list_link_titles, task_lenses

# `fields` is imported for its SIDE EFFECT: a code declaration registers at import, and the seed suite has
# to know whether one still stands for `CRM Task.due_state`.
from tatva_connect.list_engine import derived, engine, fields

DOCTYPE = "CRM Derived Field"
TASK = "CRM Task"
LEAD = "CRM Lead"
DEAL = "CRM Deal"
PROBE = "HeadProbe"
CLOSED = ["Done", "Canceled"]

DUE_STATE = "due_state"

# The shipped seed. Gitignored by design, so a checkout without the go-live bundle skips rather than lies.
SEED_FILE = (
	pathlib.Path(__file__).resolve().parents[3]
	/ "docs/go-live/3-seed/db-seeds/2026-07-31-6b7cb13-derived-fields.sql"
)

# The HbA1c case from the plan, on a REAL numeric column of a REAL second doctype. Half-open, so the row
# sitting exactly on 7 or on 9 belongs to the upper bucket in SQL and in Python alike.
HBA1C = "custom_latest_hba1c"
CONTROL = "hba1c_control"
HBA1C_BUCKETS = [
	{"value": "Controlled", "theme": "green", "filters": [[HBA1C, "<", 7]]},
	{"value": "Borderline", "theme": "orange", "filters": [[HBA1C, ">=", 7], [HBA1C, "<", 9]]},
	{"value": "Uncontrolled", "theme": "red", "filters": [[HBA1C, ">=", 9]]},
]

ALL_SURFACES = "column, filter, sort, group_by, quick_filter"

MENUS = {
	"filter": task_lenses.get_filterable_fields,
	"group_by": task_lenses.get_group_by_fields,
	"sort": task_lenses.sort_options,
	"columns": task_lenses.get_column_fields,
}


def _native_shapes(owner_field):
	"""The eight payload shapes the inert path is proven on, in terms no doctype has to be special to answer.

	`status is set` rather than a literal value, because the point is that OUR answer and NATIVE's answer are
	the same bytes — not what the rows are."""
	return {
		"plain list, real-column filter": {
			"filters": {"status": ["is", "set"]},
			"order_by": "modified desc",
		},
		"real-column sort": {"filters": {}, "order_by": "modified asc"},
		"explicit rows": {"filters": {}, "rows": ["name", "status"]},
		"explicit columns": {
			"filters": {},
			"columns": [{"label": "Status", "type": "Data", "key": "status", "width": "10rem"}],
		},
		"group-by a real column": {
			"filters": {},
			"view": {"view_type": "group_by", "group_by_field": "status"},
		},
		"kanban on a real column": {
			"filters": {},
			"view": {"view_type": "kanban"},
			"column_field": "status",
		},
		"default_filters scoping": {
			"filters": {"status": ["is", "set"]},
			"default_filters": {"name": ["like", "%"]},
		},
		"@me filter": {"filters": {owner_field: "@me"}},
	}


def _offered(menu, doctype):
	return [f.get("fieldname") for f in MENUS[menu](doctype)]


def _without_relay(fields):
	"""A menu with `link_query` taken back off, so a byte-identity check still means what it meant."""
	return [{k: v for k, v in f.items() if k != "link_query"} for f in fields]


def _drop(dt, fieldname):
	"""Take a declaration back out of the world. A row is live the moment it is saved, so a leaked one
	would follow this suite into every other module."""
	name = frappe.db.exists(DOCTYPE, {"dt": dt, "fieldname": fieldname})
	if name:
		frappe.delete_doc(DOCTYPE, name, force=True, ignore_permissions=True)


class HeadCase(FrappeTestCase):
	"""Rows are authored the way an operator authors them — through the doctype, so `validate` runs."""

	def setUp(self):
		frappe.set_user("Administrator")
		self.authored = []

	def tearDown(self):
		frappe.set_user("Administrator")
		for name in self.authored:
			if frappe.db.exists(DOCTYPE, name):
				frappe.delete_doc(DOCTYPE, name, force=True, ignore_permissions=True)

	def author(self, dt, fieldname, buckets, **overrides):
		row = {
			"doctype": DOCTYPE,
			"dt": dt,
			"fieldname": fieldname,
			"label": fieldname.replace("_", " ").title(),
			"surfaces": ALL_SURFACES,
			"default_column": 0,
			"enabled": 1,
			"buckets": frappe.as_json(buckets),
		}
		row.update(overrides)
		_drop(dt, fieldname)
		doc = frappe.get_doc(row).insert(ignore_permissions=True)
		self.authored.append(doc.name)
		return doc


class LeadCase(HeadCase):
	"""Six leads straddling both ends of both numeric bounds, scoped by name so counts stay deterministic."""

	EXPECTED: typing.ClassVar[dict] = {
		"controlled_low": (2.5, "Controlled"),
		"controlled_edge": (6.9, "Controlled"),
		"borderline_floor": (7.0, "Borderline"),
		"borderline_edge": (8.9, "Borderline"),
		"uncontrolled_floor": (9.0, "Uncontrolled"),
		"uncontrolled_high": (12.4, "Uncontrolled"),
	}

	def setUp(self):
		super().setUp()
		frappe.db.delete(LEAD, {"lead_name": ["like", f"{PROBE}%"]})
		self.leads = {}
		for label, (value, _bucket) in self.EXPECTED.items():
			doc = frappe.get_doc(
				{"doctype": LEAD, "first_name": PROBE, "last_name": label, HBA1C: value}
			).insert(ignore_permissions=True)
			self.leads[label] = doc.name

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.delete(LEAD, {"lead_name": ["like", f"{PROBE}%"]})
		super().tearDown()

	def _get_data(self, **overrides):
		payload = {
			"doctype": LEAD,
			"filters": {"lead_name": ["like", f"{PROBE}%"]},
			"order_by": "creation asc",
			"rows": ["name", "lead_name", HBA1C, CONTROL],
			"page_length": 50,
		}
		payload.update(overrides)
		return list_link_titles.get_data(**payload)

	def _shown(self, result=None):
		result = self._get_data() if result is None else result
		return {r["name"]: r.get(CONTROL) for r in result["data"]}


class TestASecondDoctypeNeedsNoCode(LeadCase):
	"""One authored row, on a doctype that has never declared anything, and no Python is edited.

	RED beyond the missing doctype: until the loader puts an enabled row into the same registry the code
	declarations populate, `CRM Lead` declares nothing, so the column is blank, the filter is an unknown
	column and the four menus never offer it."""

	def setUp(self):
		super().setUp()
		self.field = self.author(LEAD, CONTROL, HBA1C_BUCKETS, label="HbA1c Control", order_by=HBA1C)

	def test_the_value_shown_is_the_value_the_declaration_gives(self):
		shown = self._shown()
		for label, (_value, bucket) in self.EXPECTED.items():
			with self.subTest(label):
				self.assertEqual(shown[self.leads[label]], bucket)

	def test_filtering_returns_exactly_the_rows_the_list_shows(self):
		shown = self._shown()
		for value in ("Controlled", "Borderline", "Uncontrolled"):
			with self.subTest(value):
				filtered = self._get_data(filters={"lead_name": ["like", f"{PROBE}%"], CONTROL: value})
				self.assertEqual(
					{r["name"] for r in filtered["data"]},
					{name for name, v in shown.items() if v == value},
				)
				self.assertEqual(filtered["total_count"], len(filtered["data"]))

	def test_a_numeric_range_claims_its_own_floor_in_both_readers(self):
		"""7.0 and 9.0 are the whole point of a range declaration: `>=` opens the upper bucket and `<` closes
		the lower one, so the row sitting ON the bound must read as the upper bucket AND come back from a
		filter on it. A declaration that disagreed with itself here would display a lead the board never shows."""
		shown = self._shown()
		for label, expected in (("borderline_floor", "Borderline"), ("uncontrolled_floor", "Uncontrolled")):
			with self.subTest(label):
				self.assertEqual(shown[self.leads[label]], expected)
				filtered = self._get_data(filters={"lead_name": ["like", f"{PROBE}%"], CONTROL: expected})
				self.assertIn(self.leads[label], {r["name"] for r in filtered["data"]})

	def test_sorting_composes_the_page_in_declaration_order(self):
		ordered = self._get_data(order_by=f"{CONTROL} asc")
		seen = [r[CONTROL] for r in ordered["data"] if r.get(CONTROL)]
		runs = [v for i, v in enumerate(seen) if i == 0 or seen[i - 1] != v]
		declared = ["Controlled", "Borderline", "Uncontrolled"]
		self.assertEqual(runs, sorted(set(runs), key=declared.index), f"buckets interleave: {seen}")

	def test_grouping_answers_the_field_and_its_options(self):
		result = self._get_data(view={"view_type": "group_by", "group_by_field": CONTROL})
		self.assertEqual(result["group_by_field"]["fieldname"], CONTROL)
		self.assertEqual(result["group_by_field"]["options"], ["Controlled", "Borderline", "Uncontrolled"])
		self.assertTrue(all(CONTROL in row for row in result["data"]))

	def test_every_menu_offers_it_and_only_on_its_own_doctype(self):
		for menu in MENUS:
			with self.subTest(menu):
				self.assertIn(CONTROL, _offered(menu, LEAD), f"{menu} does not offer the authored field")
				self.assertNotIn(CONTROL, _offered(menu, TASK), f"{menu} leaked it onto {TASK}")


class TestADeclarationThatCannotBeServedIsRefusedAtSave(HeadCase):
	"""A bad declaration is refused when it is WRITTEN, never discovered on a rep's click.

	RED beyond the missing doctype: `verify()` exists but is exercised only by tests, so nothing runs it on
	Save. Until `validate` does, both rows below save clean and take the list down later."""

	def test_overlapping_buckets_are_refused_and_the_overlap_is_named(self):
		"""SQL has no first-match ordering to rescue an overlap, so a row would display in one bucket and be
		returned by a filter on another. The message must name the bucket, or an operator cannot fix it."""
		with self.assertRaises(frappe.ValidationError) as raised:
			self.author(
				TASK,
				"_head_overlap",
				[
					{"value": "Open", "filters": [["status", "not in", CLOSED]]},
					{"value": "Any Status", "filters": [["status", "is", "set"]]},
				],
			)
		self.assertIn("Open", str(raised.exception), "the refusal does not name the overlapping bucket")
		self.assertFalse(
			frappe.db.exists(DOCTYPE, {"dt": TASK, "fieldname": "_head_overlap"}),
			"the row was refused and stored anyway",
		)

	def test_a_fieldname_that_collides_with_a_real_column_is_refused(self):
		"""A shadowed column makes the cell and the column disagree in silence, and `_assert_declarable`
		raises from the HOT READ PATH — so allowing this row saved would 500 every Tasks list on the site."""
		with self.assertRaises(frappe.ValidationError) as raised:
			self.author(TASK, "status", [{"value": "A", "filters": [["due_date", "is", "set"]]}])
		self.assertIn("status", str(raised.exception))
		self.assertFalse(frappe.db.exists(DOCTYPE, {"dt": TASK, "fieldname": "status"}))

	def test_a_value_with_a_line_break_is_refused(self):
		"""Options travel newline-separated, so one line break inside a value becomes TWO entries in every
		menu — and no record can ever read as either of them."""
		for bad in ("Over\ndue", "Over\tdue", " Overdue "):
			with self.subTest(repr(bad)):
				with self.assertRaises(frappe.ValidationError):
					self.author(TASK, "_head_newline", [{"value": bad, "filters": [["status", "is", "set"]]}])
				self.assertFalse(frappe.db.exists(DOCTYPE, {"dt": TASK, "fieldname": "_head_newline"}))

	def test_a_colour_no_badge_can_wear_is_refused_and_the_colours_are_named(self):
		"""A colour outside frappe-ui's tokens draws an unstyled pill, which reads as a bug on screen. The
		client filters nothing, so this is the ONE place a bad colour can be caught."""
		with self.assertRaises(frappe.ValidationError) as raised:
			self.author(
				TASK,
				"_head_colour",
				[{"value": "Any", "theme": "purple", "filters": [["status", "is", "set"]]}],
			)
		self.assertIn("purple", str(raised.exception))
		self.assertIn("orange", str(raised.exception), "the refusal does not name the colours that work")


class TestRetiringAFieldIsNeverBlocked(HeadCase):
	"""Switching a field OFF must always be possible, whatever has happened to the columns it reads.

	The proof runs the declaration against real records, so a field whose source column has since changed
	fails it — and if that ran on every save, the operator could not even retire the thing. Off is the
	escape hatch; an escape hatch with a lock on it is not one.

	RED before the fix: `_prove` runs unconditionally and the disable is refused."""

	def test_a_declaration_that_no_longer_proves_can_still_be_switched_off(self):
		row = self.author(TASK, "_head_retire", [{"value": "Any", "filters": [["status", "is", "set"]]}])
		# Overlapping buckets: the same declaration would now be refused if it were being turned ON.
		row.buckets = frappe.as_json(
			[
				{"value": "Open", "filters": [["status", "not in", CLOSED]]},
				{"value": "Any Status", "filters": [["status", "is", "set"]]},
			]
		)
		row.enabled = 0
		row.save(ignore_permissions=True)
		self.assertEqual(frappe.db.get_value(DOCTYPE, row.name, "enabled"), 0)

	def test_switching_it_back_ON_is_still_refused(self):
		"""The escape hatch must not become a way to smuggle a broken declaration into every rep's list."""
		row = self.author(TASK, "_head_retire", [{"value": "Any", "filters": [["status", "is", "set"]]}])
		row.buckets = frappe.as_json(
			[
				{"value": "Open", "filters": [["status", "not in", CLOSED]]},
				{"value": "Any Status", "filters": [["status", "is", "set"]]},
			]
		)
		row.enabled = 0
		row.save(ignore_permissions=True)
		row.enabled = 1
		with self.assertRaises(frappe.ValidationError):
			row.save(ignore_permissions=True)


class TestADisabledRowChangesNothing(LeadCase):
	"""`enabled` ships at 0, so a row can be written, read, reviewed and left off. Off must mean OFF.

	RED beyond the missing doctype: the moment the loader stops filtering on `enabled`, a half-written
	declaration reaches every rep's menus."""

	def setUp(self):
		super().setUp()
		self.field = self.author(
			LEAD, CONTROL, HBA1C_BUCKETS, label="HbA1c Control", order_by=HBA1C, enabled=0
		)

	def test_a_disabled_row_is_in_no_registry(self):
		self.assertEqual(derived.for_doctype(LEAD), ())
		self.assertNotIn(CONTROL, derived.names(LEAD))

	def test_no_menu_offers_it_and_each_is_byte_identical_to_native(self):
		"""`link_query` is stripped before comparing, for the reason `test_quick_filters` strips it: a Link
		at a composite master is RELAYED which scoped query its control must use, always and independently
		of any derived row, so it is not something a disabled row turned on."""
		from crm.api.doc import get_filterable_fields, get_group_by_fields, sort_options

		native = {"filter": get_filterable_fields, "group_by": get_group_by_fields, "sort": sort_options}
		for menu, native_menu in native.items():
			with self.subTest(menu):
				self.assertNotIn(CONTROL, _offered(menu, LEAD))
				self.assertEqual(frappe.as_json(_without_relay(MENUS[menu](LEAD))),
				                 frappe.as_json(native_menu(LEAD)))
		# The column picker has no native twin — its contract for "nothing declared" is an empty list.
		self.assertEqual(_offered("columns", LEAD), [])

	def test_the_list_is_byte_identical_to_native_on_every_shape(self):
		from crm.api.doc import get_data as native

		for label, shape in _native_shapes("lead_owner").items():
			with self.subTest(label):
				payload = {"doctype": LEAD, "order_by": "creation desc", "page_length": 5, **shape}
				self.assertEqual(
					frappe.as_json(engine.get_data(**copy.deepcopy(payload))),
					frappe.as_json(native(**copy.deepcopy(payload))),
				)


class TestADoctypeThatDeclaresNothingIsStillNative(HeadCase):
	"""The inertness guarantee, asserted WHILE another doctype has a live declaration.

	RED beyond the missing doctype: an authored `CRM Lead` row must not be able to reach `CRM Deal`. A
	registry keyed by anything but `dt` — or a loader that answers every doctype from one list — puts this
	suite red and the whole app's read path with it."""

	def setUp(self):
		super().setUp()
		self.field = self.author(LEAD, CONTROL, HBA1C_BUCKETS, label="HbA1c Control", order_by=HBA1C)

	def test_a_doctype_with_no_row_is_answered_by_native_byte_for_byte(self):
		from crm.api.doc import get_data as native

		for label, shape in _native_shapes("deal_owner").items():
			with self.subTest(label):
				payload = {"doctype": DEAL, "order_by": "creation desc", "page_length": 5, **shape}
				self.assertEqual(
					frappe.as_json(engine.get_data(**copy.deepcopy(payload))),
					frappe.as_json(native(**copy.deepcopy(payload))),
				)

	def test_no_menu_on_another_doctype_is_offered_it(self):
		for menu in MENUS:
			with self.subTest(menu):
				self.assertNotIn(CONTROL, _offered(menu, DEAL))
				self.assertNotIn(CONTROL, _offered(menu, TASK))


class TestSurfacesRestrictExactlyTheMenusTheyName(HeadCase):
	"""`surfaces` is the operator's answer to "where may a rep meet this field?" — and it binds only where
	it is written. A row naming two menus reaches two; a row naming all five reaches all five.

	RED beyond the missing doctype: `engine.lens_fields` hands every declared field to every menu today, so
	the restricted row appears in all four."""

	def setUp(self):
		super().setUp()
		self.restricted = self.author(LEAD, CONTROL, HBA1C_BUCKETS, order_by=HBA1C, surfaces="filter,sort")
		self.everywhere = self.author(LEAD, "hba1c_band", HBA1C_BUCKETS, order_by=HBA1C)

	def test_a_restricted_row_reaches_the_menus_it_names_and_no_others(self):
		for menu in ("filter", "sort"):
			with self.subTest(f"named: {menu}"):
				self.assertIn(CONTROL, _offered(menu, LEAD))
		for menu in ("group_by", "columns"):
			with self.subTest(f"unnamed: {menu}"):
				self.assertNotIn(CONTROL, _offered(menu, LEAD))

	def test_the_restriction_belongs_to_the_row_not_to_the_layer(self):
		# The control: an unrestricted row beside it still reaches every menu, so what narrowed the first
		# one was its own declaration and not something this layer decided.
		for menu in MENUS:
			with self.subTest(menu):
				self.assertIn("hba1c_band", _offered(menu, LEAD))


class TestADefaultColumnArrivesWithoutBeingAsked(LeadCase):
	"""`default_column` is what makes a field VISIBLE rather than merely available — and it is a default, so
	a rep who has chosen their own columns is never overruled.

	RED beyond the missing doctype: nothing today puts a derived field into a payload that never named it,
	so the column is absent and the rows carry no value."""

	def setUp(self):
		super().setUp()
		self.shown_by_default = self.author(
			LEAD, CONTROL, HBA1C_BUCKETS, label="HbA1c Control", order_by=HBA1C, default_column=1
		)
		self.opt_in = self.author(LEAD, "hba1c_band", HBA1C_BUCKETS, order_by=HBA1C)

	def _unasked(self, **overrides):
		payload = {
			"doctype": LEAD,
			"filters": {"lead_name": ["like", f"{PROBE}%"]},
			"order_by": "creation asc",
			"page_length": 50,
		}
		payload.update(overrides)
		return list_link_titles.get_data(**payload)

	def test_it_is_in_the_columns_of_a_caller_who_named_none(self):
		result = self._unasked()
		keys = [c.get("key") for c in result["columns"]]
		column = next((c for c in result["columns"] if c.get("key") == CONTROL), None)
		self.assertIsNotNone(column, f"the default column is absent: {keys}")
		self.assertEqual(column["is_derived"], 1)
		self.assertEqual(column["label"], "HbA1c Control")

	def test_the_rows_carry_the_value_the_column_promises(self):
		result = self._unasked()
		shown = {r["name"]: r.get(CONTROL) for r in result["data"]}
		for label, (_value, bucket) in self.EXPECTED.items():
			with self.subTest(label):
				self.assertEqual(shown[self.leads[label]], bucket)

	def test_a_row_that_is_not_a_default_column_stays_out(self):
		keys = [c.get("key") for c in self._unasked()["columns"]]
		self.assertNotIn("hba1c_band", keys, "an opt-in field arrived unasked")

	def test_a_caller_who_names_columns_gets_exactly_those(self):
		asked = [{"label": "Full Name", "type": "Data", "key": "lead_name", "width": "16rem"}]
		result = self._unasked(columns=copy.deepcopy(asked), rows=["name", "lead_name"])
		self.assertEqual([c.get("key") for c in result["columns"]], ["lead_name"])


class TestTheShippedDeclarationIsTheOneTheListServes(HeadCase):
	"""`due_state` is the FIRST CITIZEN, not a special case, and it is a DB SEED — a `.sql` an operator runs
	after migrate, exactly as they run the task types and the lead stages. It is not code and it is not a
	fixture, so nothing in this app authors it and nothing here executes it.

	What the suite CAN prove, and what it must: the declaration that file carries is admissible, and a row
	built from it keeps the promise. The file is read from the go-live bundle, which is gitignored on
	purpose — a checkout without it skips rather than pretending.

	RED beyond the missing doctype: with the code declaration gone and no row authored, the Tasks list has
	no Task Status at all."""

	def setUp(self):
		super().setUp()
		frappe.db.delete(TASK, {"title": ["like", f"{PROBE}%"]})
		# The ROW goes first, so whatever is left in the registry afterwards is the CODE declaration.
		_drop(TASK, DUE_STATE)
		self.code = derived._REGISTRY.get(TASK, {}).pop(DUE_STATE, None)
		derived._validated().discard(TASK)
		# One fixture on every boundary the declaration has, read off the clock HERE — a class attribute is
		# evaluated at import, and an import on one side of midnight would file "end of today" as overdue.
		now = now_datetime()
		self.expected = {
			"overdue_minute": ("Todo", add_to_date(now, minutes=-1), "Overdue"),
			"due_end_of_today": ("Backlog", f"{nowdate()} 23:59:59", "Due Today"),
			"due_tomorrow": ("Todo", add_to_date(now, days=1), "Upcoming"),
			"no_due_date": ("Todo", None, "No Due Date"),
			"done_past_due": ("Done", add_to_date(now, days=-3), "History"),
		}
		self.names = {}
		for label, (status, due, _value) in self.expected.items():
			doc = frappe.get_doc(
				{"doctype": TASK, "title": f"{PROBE} {label}", "status": status, "due_date": due}
			).insert(ignore_permissions=True)
			self.names[label] = doc.name

	def tearDown(self):
		frappe.db.delete(TASK, {"title": ["like", f"{PROBE}%"]})
		super().tearDown()
		derived._REGISTRY.get(TASK, {}).pop(DUE_STATE, None)
		if self.code:
			derived._REGISTRY.setdefault(TASK, {})[DUE_STATE] = self.code
		derived.reload()

	def _seed_text(self):
		if not SEED_FILE.exists():
			self.skipTest(f"the go-live bundle is not in this checkout: {SEED_FILE}")
		return SEED_FILE.read_text()

	def _get_data(self, **overrides):
		payload = {
			"doctype": TASK,
			"filters": {"title": ["like", f"{PROBE}%"]},
			"order_by": "creation asc",
			"rows": ["name", "title", "status", "due_date", DUE_STATE],
			"page_length": 50,
		}
		payload.update(overrides)
		return list_link_titles.get_data(**payload)

	def test_the_shipped_file_declares_the_five_buckets_in_order_with_their_colours(self):
		buckets = frappe.parse_json(re.search(r"'(\[.*\])'", self._seed_text(), re.S).group(1))
		self.assertEqual(
			[b["value"] for b in buckets],
			["Overdue", "Due Today", "Upcoming", "No Due Date", "History"],
		)
		# The colours are authored too, so the next field needs no renderer change to wear a badge.
		self.assertEqual([b["theme"] for b in buckets], ["red", "orange", "blue", "gray", "green"])

	def test_the_shipped_file_names_the_row_the_loader_looks_for(self):
		text = self._seed_text()
		for literal in ("'CRM Task::due_state'", "'due_state'", "'Task Status'", "'due_date'"):
			with self.subTest(literal):
				self.assertIn(literal, text)

	def test_a_re_run_never_switches_a_retired_field_back_on(self):
		"""An operator who retired the field must not have it enabled again by the next deploy's seed pass.
		Everything else about the declaration is re-imposed, which is what keeps the file the source of truth."""
		# The LAST occurrence, up to the semicolon: the file's own prose about the clause would answer too.
		update = self._seed_text().rsplit("ON DUPLICATE KEY UPDATE", 1)[1].split(";")[0]
		self.assertNotIn("`enabled`", update)
		for column in ("`label`", "`order_by`", "`surfaces`", "`buckets`"):
			with self.subTest(column):
				self.assertIn(column, update)

	def test_the_shipped_declaration_verifies_on_real_records(self):
		"""The file is a dump of a row `validate` already proved. If it stops verifying, applying it would
		put a field on every rep's list whose column and filter name different records."""
		buckets = re.search(r"'(\[.*\])'", self._seed_text(), re.S).group(1)
		field = derived.from_row(
			frappe._dict({"dt": TASK, "fieldname": DUE_STATE, "label": "Task Status", "buckets": buckets})
		)
		self.assertEqual(derived.verify(field, defaults={"title": PROBE}), [])

	def test_the_promise_holds_through_the_row_the_way_it_held_through_code(self):
		buckets = frappe.parse_json(re.search(r"'(\[.*\])'", self._seed_text(), re.S).group(1))
		self.author(TASK, DUE_STATE, buckets, label="Task Status", order_by="due_date")
		shown = {r["name"]: r.get(DUE_STATE) for r in self._get_data()["data"]}
		for label, (_status, _due, value) in self.expected.items():
			with self.subTest(label):
				self.assertEqual(shown[self.names[label]], value)
		for value in ("Overdue", "Due Today", "Upcoming", "No Due Date", "History"):
			with self.subTest(f"filter {value}"):
				filtered = self._get_data(filters={"title": ["like", f"{PROBE}%"], DUE_STATE: value})
				self.assertEqual(
					{r["name"] for r in filtered["data"]},
					{name for name, v in shown.items() if v == value},
				)
