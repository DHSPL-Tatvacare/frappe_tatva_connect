# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A filter naming a composite master's LABEL selects the same rows on export as it does on the list.

A filter control at a grain master offers LABELS (`taxonomy.labels.label_query`), so a rep asks for a
type_name while every row holds `vertical::group::program::type_name`. `labels.filter_on` reads a label
back as every key it means and `list_engine.engine._read_labels` is where a listing request asks it.
Export composes a `ListRequest` directly, never came through `get_data`, and so sent the bare label to
`reportview` as an equality no row satisfies: the job completed green and the file held only its column
headers. The rule now runs as the request is BUILT, so no surface can hold a request that skipped it.

Asserted on the ids in the FILE the real endpoint produces, against the ids the list shows — comparing
filter dicts would go green on a translation that selects the wrong rows. Two grains share one type_name
because one label meaning several keys is the case an equality cannot express, and is what prod carries.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \
        --module tatva_connect.tests.list_engine.test_export_reads_a_label_like_the_list
"""
import csv
import io

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.api import list_export, list_link_titles
from tatva_connect.tests.activity import task_type_fixture

TASK = "CRM Task"
PROBE = "ZZ export label probe"

# ONE type_name, two grains — the collision that makes a label mean more than one key.
TYPE_NAME = "ZZ Verification Status"
GROUP_A = "ZZ Export Label Line A"
GROUP_B = "ZZ Export Label Line B"

SCHEMA = ({"label": "ZZ Note", "fieldname": "zz_note", "fieldtype": "Small Text"},)
FIELDS = ["name", "title", "custom_task_type", "status"]


class TestExportReadsALabelLikeTheList(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		cls.type_a = task_type_fixture.mint_type(TYPE_NAME, SCHEMA, group=GROUP_A)
		cls.type_b = task_type_fixture.mint_type(TYPE_NAME, SCHEMA, group=GROUP_B)

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		task_type_fixture.teardown()
		super().tearDownClass()

	def setUp(self):
		self.addCleanup(frappe.db.rollback)
		frappe.set_user("Administrator")
		self.on_a, self.on_b = [], []
		for task_type, holder in ((self.type_a, self.on_a), (self.type_b, self.on_b)):
			for n in range(2):
				doc = frappe.get_doc({
					"doctype": TASK, "title": f"{PROBE} {task_type} {n}",
					"status": "Todo", "custom_task_type": task_type,
				}).insert(ignore_permissions=True)
				holder.append(str(doc.name))

	def _scope(self, **extra):
		return {"title": ["like", f"{PROBE}%"], **extra}

	def _listed(self, filters):
		"""The ids the list shows, through the override the browser reaches."""
		result = list_link_titles.get_data(
			doctype=TASK, filters=dict(filters), order_by="creation asc", page_length=50
		)
		return {str(row["name"]) for row in result["data"]}

	def _exported(self, filters):
		"""The ids in the FILE, down the path prod runs: the endpoint records a job, the worker's producer
		builds the bytes from the job's own stored params."""
		args = list_export.export_args(
			doctype=TASK, fields=frappe.as_json(FIELDS),
			filters=frappe.as_json(filters), order_by="creation asc", export_all=1,
		)
		frappe.local.form_dict = frappe._dict({
			"doctype": TASK, "title": TASK, "file_format_type": "CSV",
			"fields": frappe.as_json(args["fields"]), "filters": frappe.as_json(args["filters"]),
			"order_by": args.get("order_by") or "", "start": "0", "view": "Report",
			"export_in_background": "1",
		})
		if args.get("selected_items"):
			frappe.local.form_dict["selected_items"] = frappe.as_json(args["selected_items"])
		job = frappe.get_doc("CRM Export Job", list_export.export_query()["job"])
		made = list_export.produce_export(job, frappe.parse_json(job.params), lambda rows: None)
		rows = list(csv.reader(io.StringIO(frappe.safe_decode(made["content"]))))
		# Column 0 is frappe's own `Sr`; `name` is the first field asked for, so column 1 is the id.
		return {row[1] for row in rows[1:] if len(row) > 1}

	def _both_agree(self, filters, expected):
		self.assertTrue(expected, "the fixture selected nothing; the assertions below would be vacuous")
		self.assertEqual(self._listed(filters), expected)
		self.assertEqual(self._exported(filters), expected)

	def test_the_two_types_share_a_label_and_differ_only_by_grain(self):
		"""The premise: one label must really mean two keys, and the label is not itself a key."""
		self.assertNotEqual(self.type_a, self.type_b)
		self.assertEqual(
			frappe.db.get_value("CRM Task Type", self.type_a, "type_name"),
			frappe.db.get_value("CRM Task Type", self.type_b, "type_name"),
		)
		self.assertFalse(frappe.db.exists("CRM Task Type", TYPE_NAME))

	def test_a_label_exports_the_rows_it_lists_across_every_grain_that_carries_it(self):
		"""The lock on the empty file: the list answered four rows and the export answered none."""
		self._both_agree(self._scope(custom_task_type=TYPE_NAME), set(self.on_a) | set(self.on_b))

	def test_a_label_narrows_rather_than_widening_when_it_is_combined(self):
		"""The expansion is an `in` over the label's keys, so another filter still bites."""
		self._both_agree(
			self._scope(custom_task_type=TYPE_NAME, title=["like", f"{PROBE} {self.type_a}%"]),
			set(self.on_a),
		)

	def test_a_saved_view_holding_a_raw_key_is_answered_exactly_as_it_was(self):
		"""A key is already a key; reading it a second time may not move it."""
		self._both_agree(self._scope(custom_task_type=self.type_a), set(self.on_a))

	def test_an_ordinary_column_is_never_translated(self):
		"""Nothing outside a Link at a composite master goes near the rule."""
		self._both_agree(self._scope(status="Todo"), set(self.on_a) | set(self.on_b))


class TestAnExportedCellReadsAsTheScreenDoes(TestExportReadsALabelLikeTheList):
	"""The other half of the same disconnect: WHICH rows was the filter, HOW they read is the cell."""

	def _cells(self, column):
		args = list_export.export_args(
			doctype=TASK, fields=frappe.as_json(FIELDS),
			filters=frappe.as_json(self._scope()), order_by="creation asc", export_all=1,
		)
		frappe.local.form_dict = frappe._dict({
			"doctype": TASK, "title": TASK, "file_format_type": "CSV",
			"fields": frappe.as_json(args["fields"]), "filters": frappe.as_json(args["filters"]),
			"order_by": args.get("order_by") or "", "start": "0", "view": "Report",
			"export_in_background": "1",
		})
		job = frappe.get_doc("CRM Export Job", list_export.export_query()["job"])
		made = list_export.produce_export(job, frappe.parse_json(job.params), lambda rows: None)
		rows = list(csv.reader(io.StringIO(frappe.safe_decode(made["content"]))))
		at = rows[0].index(column)
		return rows[0][at], {row[at] for row in rows[1:]}

	def _screen_labels(self, fieldname):
		"""What the LIST renders in that column — the `_link_titles` map it attaches beside the key."""
		result = list_link_titles.get_data(
			doctype=TASK, filters=self._scope(), order_by="creation asc", page_length=50
		)
		titles = result.get("_link_titles") or {}
		master = frappe.get_meta(TASK).get_field(fieldname).options
		return {titles.get(f"{master}::{row[fieldname]}") for row in result["data"]}

	def test_a_composite_key_leaves_as_the_label_the_list_shows(self):
		"""The manager opens the file and reads what the rep read, not a database key."""
		header, values = self._cells("Task Type")
		self.assertEqual(header, "Task Type")
		self.assertNotIn("::", "".join(values))
		self.assertEqual(values, self._screen_labels("custom_task_type"))

	def test_the_key_is_still_what_the_filter_matched(self):
		"""Reading the cell may not move the rows: both grains are still in the file."""
		_header, titles = self._cells("Title")
		self.assertEqual(len(titles), len(self.on_a) + len(self.on_b))

	def test_an_ordinary_cell_is_untouched(self):
		"""Only a Link at a composite master is read; everything else is frappe's own value."""
		_header, values = self._cells("Status")
		self.assertEqual(values, {"Todo"})

	def test_desk_keeps_exporting_keys_for_data_import(self):
		"""Desk's own export round-trips through Data Import, which matches on the key, never the label."""
		frappe.local.form_dict = frappe._dict({
			"doctype": TASK, "title": TASK, "file_format_type": "CSV",
			"fields": frappe.as_json(FIELDS),
			"filters": frappe.as_json({"custom_task_type": ["in", [self.type_a, self.type_b]]}),
			"order_by": "creation asc", "start": "0", "view": "Report",
		})
		list_export.export_query()
		rows = list(csv.reader(io.StringIO(frappe.safe_decode(frappe.local.response.filecontent))))
		at = rows[0].index("Task Type")
		self.assertEqual({row[at] for row in rows[1:]}, {self.type_a, self.type_b})
