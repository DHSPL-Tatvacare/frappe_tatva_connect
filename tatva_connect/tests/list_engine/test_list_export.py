# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Export was the ONE silent-wrong surface: it went straight to `frappe.desk.reportview.export_query`,
which is outside the engine, so a rep exporting a list filtered to `Overdue` received rows that filter
never selected — no error, no warning, a spreadsheet that disagrees with the screen (freeze list §16 #13).

The property asserted here is the layer's own, not a shape: the rows the export's filters SELECT are
exactly the rows the list SHOWS. The three arguments are also checked for what `export_query` can actually
be given — a derived name in `fields` fails `reportview.validate_fields`, and the same name in `order_by`
fails `DatabaseQuery`, so neither may ever leave this function.

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
		for value in ("Overdue", "Due Today", "History"):
			with self.subTest(value):
				shown = {
					name
					for name, shows in self._shown(self._get_data(filters={**SCOPE, FIELD: value})).items()
					if shows == value
				}
				args = self._args(filters=frappe.as_json({**SCOPE, FIELD: value}))
				exported = {
					r.name for r in frappe.get_all(TASK, fields=["name"], filters=args["filters"], limit=0)
				}
				self.assertTrue(shown)
				self.assertEqual(shown, exported)

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
