# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Export was the ONE silent-wrong surface: it went straight to `frappe.desk.reportview.export_query`,
which is outside the engine, so a rep exporting a list filtered to `Overdue` received rows that filter
never selected — no error, no warning, a spreadsheet that disagrees with the screen (freeze list §16 #13).

The property asserted here is the layer's own, not a shape: the rows the export SELECTS are exactly the
rows the list SHOWS. The arguments are also checked for what `export_query` can actually be given — a
derived name in `fields` fails `reportview.validate_fields`, the same name in `order_by` fails
`DatabaseQuery`, and a bucket predicate is a NESTED group that `reportview.validate_filters` cannot read
at all. So the narrowing leaves as identifiers and nothing nested is ever handed over; that is asserted
by calling frappe's own validator rather than by describing a shape.

And the line: a request naming no derived field gets its own three arguments back, unchanged, so an
ordinary column's export is byte for byte the URL it was before this endpoint existed.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \
        --module tatva_connect.tests.list_engine.test_list_export
"""

import frappe

from tatva_connect.api import list_export
from tatva_connect.tests.list_engine.test_list_engine import FIELD, PROBE, TASK, ListEngineCase

SCOPE = {"title": ["like", f"{PROBE}%"]}
COLUMNS = ["name", "title", "status", "due_date", FIELD]


class ExportCase(ListEngineCase):
	def _args(self, **overrides):
		payload = {
			"doctype": TASK,
			"fields": frappe.as_json(COLUMNS),
			"filters": frappe.as_json(SCOPE),
			"order_by": "creation asc",
		}
		payload.update(overrides)
		return list_export.export_args(**payload)


class TestTheExportShipsWhatTheScreenShowed(ExportCase):
	def test_a_derived_filter_selects_exactly_the_rows_the_list_shows(self):
		"""The narrowing travels as identifiers, so the ids ARE the promise — `reportview.export_query:437`
		turns `selected_items` into `name in (…)` and discards the filters entirely."""
		for value in ("Overdue", "Due Today", "History"):
			with self.subTest(value):
				shown = {
					name
					for name, shows in self._shown(self._get_data(filters={**SCOPE, FIELD: value})).items()
					if shows == value
				}
				args = self._args(filters=frappe.as_json({**SCOPE, FIELD: value}))
				self.assertTrue(shown)
				self.assertEqual(shown, set(args["selected_items"]))

	def test_what_export_is_given_is_something_reportview_can_actually_read(self):
		"""THE LOCK ON THE 500. A bucket predicate is a nested group; `reportview.validate_filters` reads
		every condition as a flat 3- or 4-tuple and reached `.strip()` on a list, so the export died with
		HTTP 500 while the identical filter ran clean through `frappe.get_list` (measured 2026-08-01).

		The gate asserted is frappe's OWN — the first thing the export URL hits — rather than a shape of
		our invention, so this cannot pass while the real endpoint fails."""
		from frappe.desk.reportview import validate_filters

		for order_by in ("creation asc", f"{FIELD} asc"):
			with self.subTest(order_by):
				args = self._args(filters=frappe.as_json({**SCOPE, FIELD: "Overdue"}), order_by=order_by)
				validate_filters(frappe._dict({"doctype": TASK}), args["filters"])

	def test_the_page_and_the_ceiling_both_bound_what_is_exported(self):
		"""`max_report_rows` is the operator's own field. The cap CAPS — nothing here refuses."""
		cap = list_export.row_cap()
		self.assertGreater(cap, 0, "the ceiling must always be a real number, never unbounded")
		self.assertEqual(list_export._wanted(page_length=5, export_all=0), 5)
		self.assertEqual(list_export._wanted(page_length=0, export_all=0), cap)
		self.assertEqual(list_export._wanted(page_length=cap + 10**6, export_all=0), cap)
		self.assertEqual(list_export._wanted(page_length=5, export_all=1), cap)

	def test_the_derived_name_never_reaches_reportview(self):
		args = self._args(filters=frappe.as_json({**SCOPE, FIELD: "Overdue"}), order_by=f"{FIELD} asc")
		self.assertNotIn(FIELD, args["fields"])
		self.assertNotIn(FIELD, args["order_by"])
		# Dropped, not swapped for the proxy: an export ordered by `due_date` under a Task Status heading
		# ships a different question's answer. Bucket order is not expressible to reportview.
		self.assertEqual(args["order_by"], "")
		self.assertNotIn(FIELD, frappe.as_json(args["filters"]))

	def test_a_derived_column_is_dropped_even_when_nothing_derived_is_filtered_or_sorted(self):
		args = self._args()
		self.assertEqual(args["fields"], [c for c in COLUMNS if c != FIELD])


class TestNativeIsUntouched(ExportCase):
	"""The line. Only real columns in play means the caller's own arguments come straight back."""

	def test_a_request_naming_no_derived_field_gets_its_own_arguments_back(self):
		given = {"status": "Todo", "priority": "Low"}
		args = self._args(
			fields=frappe.as_json(["name", "title", "status"]),
			filters=frappe.as_json(given),
			order_by="modified desc",
		)
		expected = {"fields": ["name", "title", "status"], "filters": given, "order_by": "modified desc"}
		self.assertEqual(args, expected)

	def test_a_doctype_that_declares_nothing_is_never_translated(self):
		args = list_export.export_args(
			doctype="CRM Lead",
			fields=frappe.as_json(["name", "status"]),
			filters=frappe.as_json({"status": "Open"}),
			order_by="modified desc",
		)
		expected = {"fields": ["name", "status"], "filters": {"status": "Open"}, "order_by": "modified desc"}
		self.assertEqual(args, expected)
