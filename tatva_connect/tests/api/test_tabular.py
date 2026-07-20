# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""CSV and XLSX are read and written in ONE place, by frappe's own readers and writers.

The dict-ising rule was born inside `partner_bulk_worker._csv_records` and was right: the header keys
each row, and a row whose column count does not match the header becomes a per-record failure rather
than a record silently shifted into the wrong columns. It moved here unchanged so both directions and
every consumer share it — the bulk import reads through it, a template is written through it, and a
bulk export later writes through it without the rule being stated a second time.

Cell values are passed through, never stringified: a CSV cell is already a string, and an XLSX cell
arrives as the number or date openpyxl read. Coercing those to text here would hand the ORM
"2026-01-15 00:00:00" for a Date field that accepts the datetime perfectly well.
"""
import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect import tabular
from tatva_connect.api.partner_bulk_worker import _csv_records


class TestTabularSeam(FrappeTestCase):
	def test_the_header_keys_each_row(self):
		rows = tabular.read(b"Phone,Name\n9876500004,Asha\n", "csv")
		self.assertEqual(rows, [{"Phone": "9876500004", "Name": "Asha"}])

	def test_a_ragged_row_names_itself_rather_than_shifting_columns(self):
		rows = tabular.read(b"Phone,Name\n9876500006\n", "csv")
		self.assertIn("__error__", rows[0])
		self.assertIn("1", rows[0]["__error__"])
		self.assertIn("2", rows[0]["__error__"])

	def test_a_wholly_blank_row_is_dropped_rather_than_read_as_a_record(self):
		rows = tabular.read(b"Phone,Name\n,\n9876500007,Bina\n", "csv")
		self.assertEqual(rows, [{"Phone": "9876500007", "Name": "Bina"}])

	def test_a_file_with_only_a_header_yields_no_records(self):
		self.assertEqual(tabular.read(b"Phone,Name\n", "csv"), [])

	def test_csv_round_trips_through_the_seam(self):
		raw = tabular.write(["Phone", "Name"], [["9876500008", "Chetan"]], "csv")
		self.assertEqual(tabular.read(raw, "csv"), [{"Phone": "9876500008", "Name": "Chetan"}])

	def test_xlsx_round_trips_through_the_seam(self):
		raw = tabular.write(["Phone", "Name"], [["9876500009", "Dev"]], "xlsx")
		self.assertTrue(raw.startswith(b"PK"), "an xlsx workbook was not produced")
		self.assertEqual(tabular.read(raw, "xlsx"), [{"Phone": "9876500009", "Name": "Dev"}])

	def test_an_xlsx_number_is_not_stringified_on_the_way_through(self):
		"""The ORM coerces a real value better than this seam could coerce it to text and back."""
		raw = tabular.write(["Count"], [[7]], "xlsx")
		self.assertEqual(tabular.read(raw, "xlsx")[0]["Count"], 7)

	def test_an_unsupported_format_is_refused_by_name(self):
		with self.assertRaises(frappe.ValidationError):
			tabular.read(b"anything", "pdf")
		with self.assertRaises(frappe.ValidationError):
			tabular.write(["A"], [["b"]], "pdf")

	def test_the_partner_csv_lane_still_parses_exactly_as_before(self):
		"""_csv_records kept its name and its answer; only the rule's home moved."""
		self.assertEqual(_csv_records(b"mobile_no,first_name\n9876500010,Eshan\n"),
		                 [{"mobile_no": "9876500010", "first_name": "Eshan"}])
