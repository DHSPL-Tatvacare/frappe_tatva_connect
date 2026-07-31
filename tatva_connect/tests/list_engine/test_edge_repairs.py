# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The five edges the 2026-07-31 adversarial audit broke, each asserted as what a REP SEES.

The audit's finding about this suite was sharper than any of its findings about the code: five of six
severe defects were things the tests were structurally incapable of catching, because they asserted that
the mechanism RAN rather than that the answer was right. Every test here is written the other way.

    #1  A field that no longer exists must not lock a rep out of their own saved view.
    #2  A field authored today must reach a rep who loaded the page yesterday.
    #3  An export must ship the rows that were on the screen.
    #4  A card field the rep picked must come back, and must not be overwritten server-side.
    #5  A filter must not build a query the size of the table, and a calendar must not lie about a month.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \
        --module tatva_connect.tests.list_engine.test_edge_repairs
"""

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_to_date, now_datetime

from tatva_connect.api import boot, list_export, list_link_titles
from tatva_connect.list_engine import derived, engine, repair, views

DOCTYPE = "CRM Derived Field"
VIEWS = "CRM View Settings"
TASK = "CRM Task"
PROBE = "EdgeProbe"
DUE_STATE = "due_state"
GONE = "retired_state"

BUCKETS = [
	{"value": "Open", "theme": "blue", "filters": [["status", "not in", ["Done", "Canceled"]]]},
	{"value": "Closed", "theme": "green", "filters": [["status", "in", ["Done", "Canceled"]]]},
]


def _drop(dt, fieldname):
	name = frappe.db.exists(DOCTYPE, {"dt": dt, "fieldname": fieldname})
	if name:
		frappe.delete_doc(DOCTYPE, name, force=True, ignore_permissions=True)


class EdgeCase(FrappeTestCase):
	"""Six probe tasks, three open and three closed, so every bucket has rows and the counts are known."""

	def setUp(self):
		frappe.set_user("Administrator")
		frappe.db.delete(TASK, {"title": ["like", f"{PROBE}%"]})
		self.authored = []
		# ONE instant for the whole case: re-reading the clock moves a boundary past a fixture sitting on it.
		now = self.now = now_datetime()
		self.tasks = {}
		for i, (status, due) in enumerate(
			[
				("Todo", add_to_date(now, minutes=-30)),
				("Backlog", add_to_date(now, days=1)),
				("Todo", None),
				("Done", add_to_date(now, days=-2)),
				("Canceled", add_to_date(now, days=-1)),
				("Done", None),
			]
		):
			doc = frappe.get_doc(
				{"doctype": TASK, "title": f"{PROBE} {i}", "status": status, "due_date": due}
			).insert(ignore_permissions=True)
			self.tasks[f"{PROBE} {i}"] = doc.name

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.delete(TASK, {"title": ["like", f"{PROBE}%"]})
		for name in self.authored:
			if frappe.db.exists(DOCTYPE, name):
				frappe.delete_doc(DOCTYPE, name, force=True, ignore_permissions=True)
		frappe.db.delete(VIEWS, {"label": ["like", f"{PROBE}%"]})
		derived.reload()

	def author(self, fieldname, **overrides):
		row = {
			"doctype": DOCTYPE,
			"dt": TASK,
			"fieldname": fieldname,
			"label": fieldname.replace("_", " ").title(),
			"surfaces": "column, filter, sort, group_by, quick_filter",
			"enabled": 1,
			"buckets": frappe.as_json(BUCKETS),
		}
		row.update(overrides)
		_drop(TASK, fieldname)
		doc = frappe.get_doc(row).insert(ignore_permissions=True)
		self.authored.append(doc.name)
		return doc

	def scoped(self, **overrides):
		payload = {
			"doctype": TASK,
			"filters": {"title": ["like", f"{PROBE}%"]},
			"order_by": "creation asc",
			"rows": ["name", "title", "status", "due_date"],
			"page_length": 50,
		}
		payload.update(overrides)
		return list_link_titles.get_data(**payload)


class TestARetiredFieldDoesNotLockARepOut(EdgeCase):
	"""#1 — the field is gone; the list still opens, says so once, and does not arrive broken tomorrow.

	RED before the janitor: `frappe.get_meta` has never heard of the name, `DatabaseQuery` throws
	`Unknown column`, and the client persists the filter BEFORE the request — so the rep sees an error on
	this load and on every load after it, with no chip to clear."""

	def _view(self, **overrides):
		row = {
			"doctype": VIEWS,
			"label": f"{PROBE} view",
			"dt": TASK,
			"type": "list",
			"user": frappe.session.user,
			"filters": frappe.as_json({"title": ["like", f"{PROBE}%"], GONE: "Open"}),
			"order_by": f"{GONE} asc",
			"rows": frappe.as_json(["name", "title", GONE]),
			"columns": frappe.as_json([{"key": "title", "label": "Title"}, {"key": GONE, "label": "Gone"}]),
		}
		row.update(overrides)
		doc = frappe.get_doc(row).insert(ignore_permissions=True)
		return doc

	def test_the_list_still_renders_and_names_what_it_dropped(self):
		view = self._view()
		result = self.scoped(
			filters={"title": ["like", f"{PROBE}%"], GONE: "Open"},
			order_by=f"{GONE} asc",
			rows=["name", "title", GONE],
			view={"custom_view_name": view.name, "view_type": "list"},
		)
		self.assertEqual(result["removed_fields"], [GONE])
		self.assertEqual(len(result["data"]), len(self.tasks), "a rep was shown fewer rows than exist")

	def test_the_saved_view_is_repaired_so_it_does_not_come_back(self):
		view = self._view()
		self.scoped(
			filters={"title": ["like", f"{PROBE}%"], GONE: "Open"},
			order_by=f"{GONE} asc",
			view={"custom_view_name": view.name, "view_type": "list"},
		)
		after = frappe.get_doc(VIEWS, view.name)
		self.assertNotIn(GONE, after.filters or "")
		self.assertNotIn(GONE, after.order_by or "")
		self.assertNotIn(GONE, after.rows or "")
		self.assertNotIn(GONE, after.columns or "")

	def test_the_second_load_is_clean_and_says_nothing(self):
		view = self._view()
		self.scoped(
			filters={"title": ["like", f"{PROBE}%"], GONE: "Open"},
			view={"custom_view_name": view.name, "view_type": "list"},
		)
		repaired = frappe.get_doc(VIEWS, view.name)
		again = self.scoped(
			filters=frappe.parse_json(repaired.filters),
			view={"custom_view_name": view.name, "view_type": "list"},
		)
		self.assertNotIn("removed_fields", again)

	def test_a_live_declaration_is_never_mistaken_for_a_retired_one(self):
		self.author(DUE_STATE)
		result = self.scoped(filters={"title": ["like", f"{PROBE}%"], DUE_STATE: "Open"})
		self.assertNotIn("removed_fields", result)
		self.assertEqual({r[DUE_STATE] for r in result["data"]}, {"Open"})

	def test_a_healthy_request_is_handed_on_as_the_very_object_it_arrived_as(self):
		"""The janitor runs on EVERY list in the app, so it has to cost a healthy one nothing at all."""
		payload = {
			"doctype": TASK,
			"filters": frappe.as_json({"status": "Todo"}),
			"order_by": "modified desc",
		}
		scrubbed, removed = repair.scrub(payload)
		self.assertIs(scrubbed, payload)
		self.assertEqual(removed, [])

	def test_the_config_form_counts_the_views_that_would_be_affected(self):
		"""What the operator is asked to confirm is the same number the janitor will act on."""
		self._view()
		self.assertEqual(repair.views_using(TASK, GONE)["views"], 1)
		self.assertEqual(repair.views_using(TASK, "no_such_field_anywhere")["views"], 0)

	def test_the_engine_and_the_janitor_read_the_same_payload_keys(self):
		"""THE LOCK on the one enumeration. The engine asks which derived fields a payload names; the janitor
		asks which names it cannot serve. Two walks of the payload would drift, and drift is silent in both
		directions — a key only the engine reads leaves a broken view un-repaired, and a key only the janitor
		reads drops a field the engine was about to serve. A sixth key handled in one and not the other fails
		here, because every key in the declaration is driven with a name that must be seen."""
		self.author(DUE_STATE)
		payloads = {
			"filters": {"filters": {DUE_STATE: "Open"}},
			"default_filters": {"default_filters": {DUE_STATE: "Open"}},
			"rows": {"rows": ["name", DUE_STATE]},
			"kanban_fields": {"kanban_fields": [DUE_STATE]},
			"columns": {"columns": [{"key": DUE_STATE, "label": "S"}]},
			"order_by": {"order_by": f"modified desc, {DUE_STATE} asc"},
			"column_field": {"column_field": DUE_STATE},
			"group_by_field": {"group_by_field": DUE_STATE},
			"title_field": {"title_field": DUE_STATE},
			"view.column_field": {"view": {"column_field": DUE_STATE}},
			"view.group_by_field": {"view": {"group_by_field": DUE_STATE}},
			"view.title_field": {"view": {"title_field": DUE_STATE}},
		}
		declared = {*repair.DICT_KEYS, *repair.LIST_KEYS, *repair._SINGLE, "columns", "order_by"}
		self.assertEqual(
			declared - {k.split(".")[0] for k in payloads},
			set(),
			"a key was added to the declaration and this lock was not driven with it",
		)
		for key, payload in payloads.items():
			with self.subTest(key):
				view = payload.pop("view", {})
				said = repair.named_in(payload, view)
				self.assertIn(DUE_STATE, said, f"{key} names a field the payload walk does not see")
				self.assertEqual(
					repair.unserveable(TASK, {**payload, "doctype": TASK}, view),
					[],
					f"{key} judged a LIVE declaration unserveable",
				)


class TestBothDoorsCleanUpTheSameWay(EdgeCase):
	"""A saved view reaches the server through TWO doors and a retired field is stale at both.

	It is READ into a listing request when the rep opens the page, and it is WRITTEN back when they change
	the board. Two doors is fine; two different CLEANUPS is not — that is the same decision made twice, and
	the second one drifts. Both must ask the same question, remove the same names and persist through the
	same write, and this asserts the stored row ends up IDENTICAL whichever door was used.

	RED before the unification: the save door blanked `column_field` on the payload and never touched the
	stored row at all, so the next load found the board still broken."""

	def _board(self):
		return frappe.get_doc(
			{
				"doctype": VIEWS,
				"label": f"{PROBE} board",
				"dt": TASK,
				"type": "kanban",
				"user": frappe.session.user,
				"column_field": GONE,
				"kanban_columns": frappe.as_json([{"name": "Open"}, {"name": "Closed"}]),
				"filters": frappe.as_json({"title": ["like", f"{PROBE}%"], GONE: "Open"}),
			}
		).insert(ignore_permissions=True)

	def _state(self, name):
		doc = frappe.get_doc(VIEWS, name)
		return (doc.column_field, doc.kanban_columns, doc.filters)

	def test_the_load_door_cleans_the_stored_board(self):
		board = self._board()
		self.scoped(
			filters={"title": ["like", f"{PROBE}%"], GONE: "Open"},
			view={"custom_view_name": board.name, "view_type": "kanban"},
			column_field=GONE,
		)
		column_field, columns, filters = self._state(board.name)
		self.assertIsNone(column_field)
		self.assertEqual(frappe.parse_json(columns), [])
		self.assertNotIn(GONE, filters)

	def test_the_save_door_cleans_the_stored_board_the_same_way(self):
		board = self._board()
		views.create_or_update_standard_view(
			{
				"name": board.name,
				"doctype": TASK,
				"dt": TASK,
				"type": "kanban",
				"label": board.label,
				"column_field": GONE,
				"filters": frappe.as_json({"title": ["like", f"{PROBE}%"], GONE: "Open"}),
			}
		)
		column_field, columns, filters = self._state(board.name)
		self.assertIsNone(column_field)
		self.assertEqual(frappe.parse_json(columns), [])
		self.assertNotIn(GONE, filters)

	def test_the_edit_is_on_the_record_so_it_is_attributable_and_reversible(self):
		"""`CRM View Settings` is `track_changes: 1`, so repairing through the doctype writes a Version row.
		A rep's own configuration is not edited invisibly — the change is on the record, with a history."""
		board = self._board()
		before = frappe.db.count("Version", {"ref_doctype": VIEWS, "docname": board.name})
		self.scoped(view={"custom_view_name": board.name, "view_type": "kanban"}, column_field=GONE)
		self.assertGreater(frappe.db.count("Version", {"ref_doctype": VIEWS, "docname": board.name}), before)

	def test_a_board_on_a_live_field_is_never_touched_by_either_door(self):
		self.author(DUE_STATE)
		board = self._board()
		board.column_field = DUE_STATE
		board.filters = frappe.as_json({"title": ["like", f"{PROBE}%"]})
		board.save(ignore_permissions=True)
		untouched = self._state(board.name)
		self.scoped(view={"custom_view_name": board.name, "view_type": "kanban"}, column_field=DUE_STATE)
		self.assertEqual(self._state(board.name), untouched)


class TestAFieldAuthoredTodayReachesARepFromYesterday(EdgeCase):
	"""#2 — the field menus are cached in the browser with no expiry, so the SERVER has to hand over a key
	that moves. RED before the boot door: nothing produced `derived_field_version` at all, so a rep who had
	already loaded the page would never be offered a field an operator authored."""

	def test_the_boot_carries_a_declaration_version(self):
		self.assertIn("derived_field_version", boot.keys())

	def test_the_version_moves_when_a_field_is_authored(self):
		before = boot.keys()["derived_field_version"]
		self.author("boot_probe_state")
		self.assertNotEqual(boot.keys()["derived_field_version"], before)

	def test_the_version_moves_again_when_the_field_is_retired(self):
		doc = self.author("boot_probe_state")
		authored = boot.keys()["derived_field_version"]
		doc.enabled = 0
		doc.save(ignore_permissions=True)
		self.assertNotEqual(boot.keys()["derived_field_version"], authored)

	def test_the_page_render_puts_it_where_the_browser_reads_it(self):
		"""`crm.html` writes every key of `boot` onto `window`, so a key added to that dict IS the global
		the cache generation reads."""
		context = frappe._dict({"boot": frappe._dict({"site_name": "probe"})})
		boot.website_context(context)
		self.assertIn("derived_field_version", context.boot)
		self.assertEqual(context.boot.site_name, "probe")

	def test_a_page_without_a_boot_is_untouched(self):
		context = frappe._dict({"title": "some website page"})
		boot.website_context(context)
		self.assertEqual(dict(context), {"title": "some website page"})


class TestTheAnswerEchoesEveryKeyTheRequestNamed(EdgeCase):
	"""#4 and the lock that stops a sixth key going missing.

	A derived name is taken OUT of the payload so `frappe.get_meta` can resolve the rest, and it has to go
	back INTO the answer — the client reads the answer's own lists back into the params it re-sends and
	then saves them. `kanban_fields` was stripped and never restored, so the card badge never drew and the
	rep's pick was reverted server-side on every load.

	RED before the fix: `kanban_fields` comes back without the name."""

	def setUp(self):
		super().setUp()
		self.author(DUE_STATE)

	def test_a_card_field_the_rep_picked_comes_back(self):
		result = self.scoped(
			view={"view_type": "kanban", "column_field": "status"},
			column_field="status",
			kanban_fields=["name", "title", DUE_STATE],
			rows=["name", "title", "status", "due_date"],
		)
		self.assertIn(DUE_STATE, result["kanban_fields"])

	def test_every_card_on_every_column_is_told_the_field(self):
		"""A column's own `fields` list is what the card renders from; the answer's top-level list being
		right is worth nothing if the columns were built from the stripped one."""
		result = self.scoped(
			view={"view_type": "kanban", "column_field": DUE_STATE},
			column_field=DUE_STATE,
			kanban_fields=["name", "title", DUE_STATE],
			rows=["name", "title", "status", "due_date"],
		)
		for column in result["data"]:
			with self.subTest(column["column"]["name"]):
				self.assertIn(DUE_STATE, column["fields"])

	def test_the_value_is_on_the_cards_not_only_in_the_field_list(self):
		result = self.scoped(
			view={"view_type": "kanban", "column_field": DUE_STATE},
			column_field=DUE_STATE,
			kanban_fields=["name", "title", DUE_STATE],
			rows=["name", "title", "status", "due_date"],
		)
		for column in result["data"]:
			for card in column["data"]:
				with self.subTest(card["name"]):
					self.assertEqual(card[DUE_STATE], column["column"]["name"])

	def test_the_same_value_is_dressed_the_same_on_every_surface(self):
		"""A bucket's colour is the declaration's, and it has to reach EVERY surface that draws the value.

		The client holds no colour table any more, so a surface whose descriptor arrives without `themes`
		renders gray while the identical value is coloured elsewhere. That is what happened to the group-by
		header: the shaped `group_by_field` hand-listed its keys and dropped `themes`.

		RED before the fix: the header's descriptor carries no themes and the group reads gray."""
		declared = {b["value"]: b["theme"] for b in BUCKETS}

		listed = self.scoped(rows=["name", "title", DUE_STATE])
		cell = next(f for f in listed["fields"] if f.get("fieldname") == DUE_STATE)

		grouped = self.scoped(
			view={"view_type": "group_by", "group_by_field": DUE_STATE}, group_by_field=DUE_STATE
		)
		header = grouped["group_by_field"]

		boarded = self.scoped(
			view={"view_type": "kanban", "column_field": DUE_STATE},
			column_field=DUE_STATE,
			kanban_fields=["name", "title", DUE_STATE],
		)
		card = next(f for f in boarded["fields"] if f.get("fieldname") == DUE_STATE)

		for surface, descriptor in (("list cell", cell), ("group header", header), ("card", card)):
			with self.subTest(surface):
				self.assertEqual(descriptor.get("themes"), declared)

	def test_every_key_that_can_carry_the_name_carries_it_back(self):
		"""THE LOCK. Naming the field in a key and not getting it back means the rep's choice is silently
		reverted, whichever key it was. A sixth key stripped without a restore fails here."""
		echoes = {
			"rows": lambda r: r["rows"],
			"kanban_fields": lambda r: r["kanban_fields"],
			"columns": lambda r: [c.get("key") for c in r["columns"]],
			"title_field": lambda r: [r.get("title_field")],
			"group_by_field": lambda r: [(r.get("group_by_field") or {}).get("fieldname")],
		}
		payloads = {
			"rows": {"rows": ["name", "title", DUE_STATE]},
			"kanban_fields": {
				"view": {"view_type": "kanban", "column_field": "status"},
				"column_field": "status",
				"kanban_fields": ["name", DUE_STATE],
			},
			"columns": {"columns": [{"key": "title", "label": "Title"}, {"key": DUE_STATE, "label": "S"}]},
			"title_field": {
				"view": {"view_type": "kanban", "column_field": "status"},
				"column_field": "status",
				"title_field": DUE_STATE,
			},
			"group_by_field": {
				"view": {"view_type": "group_by", "group_by_field": DUE_STATE},
				"group_by_field": DUE_STATE,
			},
		}
		for key, payload in payloads.items():
			with self.subTest(key):
				result = self.scoped(**payload)
				self.assertIn(DUE_STATE, echoes[key](result))


class TestAnExportShipsTheRowsThatWereOnTheScreen(EdgeCase):
	"""#3 — the export door cannot be told "order by bucket", so it took the first N of a differently
	ordered set: the right COUNT of the wrong ROWS, with no error anywhere.

	RED before the fix: `selected_items` is absent and the export's own ordering decides which N."""

	def setUp(self):
		super().setUp()
		self.author(DUE_STATE, order_by="due_date")

	def _args(self, **overrides):
		payload = {
			"doctype": TASK,
			"fields": frappe.as_json(["name", "title", DUE_STATE]),
			"filters": frappe.as_json({"title": ["like", f"{PROBE}%"]}),
			"order_by": f"{DUE_STATE} asc",
			"page_length": 3,
			"export_all": 0,
		}
		payload.update(overrides)
		return list_export.export_args(**payload)

	def test_it_names_exactly_the_records_the_screen_composed(self):
		shown = self.scoped(order_by=f"{DUE_STATE} asc", page_length=3)
		self.assertEqual(
			self._args()["selected_items"], [r["name"] for r in shown["data"]], "the export chose other rows"
		)

	def test_the_derived_column_is_not_asked_of_a_door_that_has_no_such_column(self):
		self.assertNotIn(DUE_STATE, self._args()["fields"])

	def test_exporting_everything_names_no_records_because_it_needs_none(self):
		"""Every record is included whatever order they arrive in, and pinning thousands of ids into a query
		string is how a URL reaches its length limit."""
		self.assertNotIn("selected_items", self._args(export_all=1))

	def test_a_sort_on_a_real_column_is_left_to_the_export_door(self):
		args = self._args(order_by="due_date asc")
		self.assertNotIn("selected_items", args)
		self.assertEqual(args["order_by"], "due_date asc")

	def test_assigned_to_me_is_resolved_before_it_leaves(self):
		"""`reportview` has no session-user substitution, so the literal token matched nothing and the rep
		got someone else's rows or none. The list prepares this; the export now prepares it the same way."""
		args = list_export.export_args(
			doctype=TASK,
			fields=frappe.as_json(["name"]),
			filters=frappe.as_json({"_assign": ["like", "%@me%"]}),
			order_by="modified desc",
		)
		self.assertEqual(args["filters"]["_assign"], ["like", f"%{frappe.session.user}%"])


class TestAFilterDoesNotBuildAQueryTheSizeOfTheTable(EdgeCase):
	"""#5, first half — `!=`, `not in` and `is set` resolve the records the OTHER buckets claim and pass
	their ids to the query. Fine at three thousand, a hang at a hundred thousand.

	THREE buckets, deliberately: `!=` on a two-bucket field names exactly one other bucket and takes the
	fast path, so a two-bucket fixture would have asserted the bound while never reaching it.

	RED before the bound: the cap does not exist and the list simply builds the whole thing."""

	def setUp(self):
		super().setUp()
		self.author(
			DUE_STATE,
			buckets=frappe.as_json(
				[
					{"value": "Open", "filters": [["status", "in", ["Todo"]]]},
					{"value": "Waiting", "filters": [["status", "in", ["Backlog"]]]},
					{"value": "Closed", "filters": [["status", "in", ["Done", "Canceled"]]]},
				]
			),
		)
		self.cap = engine.UNION_ROW_CAP

	def tearDown(self):
		engine.UNION_ROW_CAP = self.cap
		super().tearDown()

	def test_under_the_bound_it_answers_exactly_the_complement(self):
		closed = self.scoped(filters={"title": ["like", f"{PROBE}%"], DUE_STATE: "Closed"})
		other = self.scoped(filters={"title": ["like", f"{PROBE}%"], DUE_STATE: ["!=", "Closed"]})
		self.assertEqual(
			{r["name"] for r in closed["data"]} | {r["name"] for r in other["data"]},
			set(self.tasks.values()),
			"the two halves do not add up to the whole",
		)

	def test_a_union_resolves_no_identifiers_at_all(self):
		"""THE FIX. `!=` used to run a query per bucket, collect every matching id and hand the whole list
		back as `name in (…)`. The declaration's own tuples now reach SQL as one OR-of-AND condition, so the
		cap cannot bite however large the table is — asserted by pinning it at 1 and filtering anyway."""
		engine.UNION_ROW_CAP = 1
		result = self.scoped(filters={"title": ["like", f"{PROBE}%"], DUE_STATE: ["!=", "Closed"]})
		self.assertEqual({r[DUE_STATE] for r in result["data"]}, {"Open", "Waiting"})

	def test_the_condition_carries_the_declaration_and_names_no_record(self):
		"""What reaches the database is the buckets' own tuples, ORed. A `name in (…)` here would mean the
		identifier walk came back."""
		request = engine.ListRequest(
			{
				"doctype": TASK,
				"filters": {"title": ["like", f"{PROBE}%"], DUE_STATE: ["!=", "Closed"]},
			}
		)
		flat = frappe.as_json(request.terms)
		self.assertIn('"or"', flat, "the union is not expressed as a condition")
		self.assertNotIn('"name"', flat, "the union still resolves record identifiers")

	def test_is_not_set_is_the_one_case_that_still_resolves_records(self):
		"""frappe's nested filters have `and` and `or` but no `not`, so "the rows NO bucket claims" cannot be
		written as a condition. It is bounded and refused readably — and on a declaration that partitions,
		which `verify()` proves ours does, it names nothing anyway."""
		engine.UNION_ROW_CAP = 1
		with self.assertRaises(frappe.ValidationError) as refused:
			self.scoped(filters={"title": ["like", f"{PROBE}%"], DUE_STATE: ["is", "not set"]})
		self.assertIn("Narrow the list", str(refused.exception))

	def test_a_single_bucket_never_resolves_a_record_at_all(self):
		"""The fast path stays fast: one bucket inlines its own declared tuples and the bound cannot bite."""
		engine.UNION_ROW_CAP = 1
		result = self.scoped(filters={"title": ["like", f"{PROBE}%"], DUE_STATE: "Open"})
		self.assertEqual({r[DUE_STATE] for r in result["data"]}, {"Open"})


class TestACalendarAsksForTheMonthItIsShowing(EdgeCase):
	"""#5, second half — before this the calendar asked for EVERYTHING the filters matched and drew the
	month it was on, stopping at a thousand records with nothing said to the rep.

	RED before the window: the range params are ignored and every record comes back."""

	def setUp(self):
		super().setUp()
		self.author(DUE_STATE)
		self.cap = engine.CALENDAR_ROW_CAP

	def tearDown(self):
		engine.CALENDAR_ROW_CAP = self.cap
		super().tearDown()

	def _calendar(self, **overrides):
		now = self.now
		payload = {
			"view": {"view_type": "calendar"},
			"calendar_field": "due_date",
			"calendar_start": add_to_date(now, days=-1),
			"calendar_end": add_to_date(now, days=2),
		}
		payload.update(overrides)
		return self.scoped(**payload)

	def test_only_the_window_comes_back(self):
		drawn = {r["name"] for r in self._calendar()["data"]}
		self.assertEqual(drawn, {self.tasks[f"{PROBE} {i}"] for i in (0, 1, 4)})

	def test_a_wider_window_draws_more(self):
		now = now_datetime()
		wider = self._calendar(
			calendar_start=add_to_date(now, days=-30), calendar_end=add_to_date(now, days=30)
		)
		self.assertEqual(len(wider["data"]), 4, "the records with no due date are not on a calendar")

	def test_the_count_is_the_window_not_the_table(self):
		"""A count that ignored the window would contradict the grid and make `truncated` meaningless."""
		self.assertEqual(self._calendar()["total_count"], 3)

	def test_a_crowded_window_says_so_instead_of_drawing_a_thinner_month(self):
		engine.CALENDAR_ROW_CAP = 2
		crowded = self._calendar()
		self.assertTrue(crowded["truncated"])
		self.assertEqual(crowded["row_count"], 2)
		self.assertEqual(crowded["total_count"], 3)

	def test_a_window_that_holds_everything_says_nothing(self):
		self.assertFalse(self._calendar()["truncated"])
