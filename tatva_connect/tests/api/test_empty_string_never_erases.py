# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""An empty string is "not sent", never "erase this" — on a child column as much as on a parent one.

The parent leg has always held this rule (`_collect` drops a value that is None or ""), because many
clients serialise an absent field as "" and a PUT carrying one had already blanked a clinical note. The
child leg did not: `_merge_row` sets whatever reaches it, so a "" overwrote the stored value.

Nothing reached that gap while the Facebook fold skipped blank answers. It stopped skipping them when a
blank answer became a fact worth recording as a key-value row, which put a "" on the path to any column
an operator maps a question to. The rule is therefore held once, for every column shape, at the same
seam the parent leg uses.

A key-value row is deliberately exempt. Its value column carries one answer per row rather than one
fact per lead, so "asked and answered blank" is a record in its own right and erases nothing.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \
        --module tatva_connect.tests.api.test_empty_string_never_erases
"""
import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.api import partner

SINGLE_ROW_TABLE = "custom_acquisition_profile"
MULTI_ROW_TABLE = "custom_lab_profile"
KEY_VALUE_TABLE = "custom_screening_answers"


class TestEmptyStringNeverErases(FrappeTestCase):
	def _collect_child(self, table, allowed, rows):
		_parent, children = partner._collect(
			frappe._dict({table: rows}), parent_fields=[], child_allow={table: allowed}, allow_routing=False
		)
		return children.get(table) or []

	def test_a_blank_parent_field_is_not_sent(self):
		"""The rule the parent leg already held, restated so the two legs are locked together."""
		parent, _children = partner._collect(
			frappe._dict({"mobile_no": "+919812300111", "first_name": ""}),
			parent_fields=["mobile_no", "first_name"], child_allow={}, allow_routing=False,
		)
		self.assertIn("mobile_no", parent)
		self.assertNotIn("first_name", parent, "a blank parent field must not reach the doc")

	def test_a_blank_single_row_child_column_is_not_sent(self):
		"""`_merge_row` sets whatever reaches it, so a blank arriving here overwrote a stored value."""
		rows = self._collect_child(
			SINGLE_ROW_TABLE, ["utm_campaign", "utm_source"], [{"utm_campaign": "spring-push", "utm_source": ""}]
		)
		self.assertEqual(
			rows, [{"utm_campaign": "spring-push"}], "a blank child column must be dropped, not written"
		)

	def test_a_multi_row_child_keeps_its_key_even_though_the_key_is_blank_elsewhere(self):
		"""The row key is an address, not a value: dropping it would orphan the row from its upsert."""
		rows = self._collect_child(
			MULTI_ROW_TABLE, ["report_date"], [{"report_date": "2026-07-20", "custom_hba1c": ""}]
		)
		self.assertEqual(rows, [{"report_date": "2026-07-20"}])

	def test_the_delete_flag_survives_whatever_it_holds(self):
		"""_delete is an instruction, not a value, and the upsert engine reads it to drop a keyed row."""
		rows = self._collect_child(MULTI_ROW_TABLE, ["report_date"], [{"report_date": "2026-07-20", "_delete": True}])
		self.assertEqual(rows, [{"report_date": "2026-07-20", "_delete": True}])

	def test_a_blank_sub_entity_field_is_not_sent(self):
		"""The leg where the defect actually bit: notes, files and calls collect through `field_spec.collect`,
		not through `partner._collect`, and it passed a "" straight to the doc. The only thing that stopped a
		PUT with content:"" blanking a clinical note was a bare truthiness check in `partner_note`, which any
		reasonable tidy-up ("if 'content' in fields") would have removed with nothing going red."""
		from tatva_connect.api import partner_note
		from tatva_connect.api.field_spec import collect

		fields = collect(partner_note.NOTE_FIELDS, {"content": "", "title": "Kept"})
		self.assertNotIn("content", fields, "a blank sub-entity field must not reach the doc")
		self.assertEqual(fields.get("title"), "Kept", "a field that was sent still lands")

	def test_a_sub_entity_field_that_carries_a_real_value_still_lands(self):
		"""The rule drops blanks, never content — a note whose body is one character is still a write."""
		from tatva_connect.api import partner_note
		from tatva_connect.api.field_spec import collect

		self.assertEqual(collect(partner_note.NOTE_FIELDS, {"content": "x"}).get("content"), "x")

	def test_a_key_value_row_may_carry_a_blank_answer(self):
		"""A question asked and answered blank is a fact about the patient, and its row erases nothing."""
		if not frappe.get_meta("CRM Lead").get_field(KEY_VALUE_TABLE):
			self.skipTest("the screening section has not landed on this site")
		rows = self._collect_child(
			KEY_VALUE_TABLE, [""], [{"question": "asked_it", "question_hash": "zz-hash", "value": ""}]
		)
		self.assertEqual(len(rows), 1, "a blank key-value answer is a record, not an omission")
		self.assertEqual(rows[0]["value"], "")
		self.assertEqual(rows[0]["question"], "asked_it")
