# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A lead field that takes MORE THAN ONE value, and the consumers that must agree about it.

The defect this shape replaces was silent: `custom_side_effects_detail` was a `Table MultiSelect` on a
child doctype — a second level of child rows, which Frappe never loads and never saves — so the loader
wrote 96 of 250 leads' answers and every reader showed an empty cell. `tests/architecture/
test_no_nested_table_multiselect.py` is the gate that stops that recurring; this file proves the
replacement behaves.

What is proved here:
  1. ADDRESSING     — (field_key, row_key) is the whole address, a BLANK row_key is a real one, and a
                      write at one address never touches another.
  2. FLATTENING     — on a multi-row section the flattened surfaces show the LATEST row's selections,
                      through `multirow.latest_child_row` and no second picker.
  3. AGREEMENT      — the Data tab, the section's rows table, the partner projection and the resolver
                      answer the SAME thing about the same row, filled and empty alike. A consumer that
                      shows a value where another shows blank is the original defect growing back.
  4. ROUND TRIP     — a write through one consumer is visible through the others on the next read.

Data-driven: the field, its section and its vocabulary are read from the declaration, never named here.
A site that declares no multi-value field skips rather than failing — that is operator data, not a bug.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \
        --module tatva_connect.tests.lead.test_multi_value
"""
import random

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import cstr

from tatva_connect.api import partner
from tatva_connect.lead import detail, multi_value, multirow
from tatva_connect.taxonomy import picklist

_ROW_KEYS = ("2026-03-14", "2026-06-02")   # deliberately out of order; the LATER one must win


def _declared_on_a_multi_row_section():
	"""(field_key, section doc) for a multi-value field on a multi-row child section, or (None, None).

	Read off the declaration so this file names no field: what is multi-value is the catalog's answer and
	which sections keep many rows is the section brain's."""
	for (section_key, _fieldname), field_key in sorted(multi_value.declared().items()):
		section = frappe.get_cached_doc("CRM Lead Section", section_key)
		if section.is_multi_row and section.child_table_field and section.row_key_field:
			return field_key, section
	return None, None


def _vocabulary(fieldname):
	"""(grain, [value pk, ...]) for the field's own picklist category — three values from ONE grain.

	The category comes from `picklist.category_of`, the one rule; the grain comes from the rows
	themselves, so the test runs against whatever vocabulary an operator has actually seeded."""
	rows = frappe.get_all(
		"CRM Picklist Value", filters={"category": picklist.category_of(fieldname)},
		fields=["name", "vertical", "group", "program"], order_by="position asc, value asc",
	)
	for row in rows:
		grain = (row["vertical"] or "", row["group"] or "", row["program"] or "")
		same = [r["name"] for r in rows
		        if (r["vertical"] or "", r["group"] or "", r["program"] or "") == grain]
		if len(same) >= 3:
			return grain, same[:3]
	return None, []


class TestLeadMultiValue(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.field_key, cls.section = _declared_on_a_multi_row_section()
		if not cls.field_key:
			return
		cls.fieldname = frappe.db.get_value("CRM Lead API Field", cls.field_key, "fieldname")
		cls.grain, cls.values = _vocabulary(cls.fieldname)

	def setUp(self):
		if not self.field_key:
			self.skipTest("no multi-value field is declared on a multi-row section")
		if not self.values:
			self.skipTest(f"no seeded picklist vocabulary for {self.fieldname}")
		# A phone of its own per test: (phone, vertical, group) is the lead's UNIQUE dedup index, and the
		# rollback is at the end of the CLASS, so a shared number collides on the second test.
		phone = f"+9190{random.randint(10_000_000, 99_999_999)}"
		self.lead = frappe.get_doc({
			"doctype": "CRM Lead", "first_name": "ZZ Multi Value", "mobile_no": phone,
			"custom_vertical": self.grain[0], "custom_group": self.grain[1],
			"custom_current_program": self.grain[2],
		}).insert(ignore_permissions=True)
		for row_key in _ROW_KEYS:
			self.lead.append(self.section.child_table_field, {self.section.row_key_field: row_key})
		self.lead.save(ignore_permissions=True)

	def _reload(self):
		return frappe.get_doc("CRM Lead", self.lead.name)

	def _panel_field(self):
		"""The Data tab's entry for the field, or None — what a rep is shown."""
		out = detail.lead_detail(self.lead.name)
		hits = [f for s in out["sections"] for f in s["fields"] if f["field_key"] == self.field_key]
		return hits[0] if hits else None

	def _rows_cells(self):
		"""{row_key: [label, ...]} from the section's rows table — the history layer, row by row."""
		out = detail.lead_detail_rows(self.lead.name, self.section.name, page_length=50)
		return {cstr(r.get(self.section.row_key_field)): r.get(self.fieldname) for r in out["data"]}, out["columns"]

	def _curated_rows(self):
		"""{row_key: [value pk, ...]} from the partner projection — the same rows, over the wire."""
		_u, _mp, _s, parent_fields, child_allow = partner._caller_fields()
		curated = partner._curate(self._reload(), parent_fields, child_allow)
		rows = curated.get(self.section.child_table_field) or []
		return {cstr(r.get(self.section.row_key_field)): r.get(self.fieldname, "<<key absent>>")
		        for r in rows}

	def _latest_row_key(self):
		"""The row every flattened surface must be answering about — the ONE rule, asked directly."""
		doc = self._reload()
		row = multirow.latest_child_row(doc.get(self.section.child_table_field), self.section.row_key_field)
		return cstr(row.get(self.section.row_key_field)) if row else ""

	# ---------------------------------------------------------------- addressing
	def test_a_blank_row_key_is_a_real_address_and_not_the_named_one(self):
		"""Many leads carry no cycle date at all, so blank has to ADDRESS rather than mean unset. A write
		at the blank address must be invisible at a named one and the other way round."""
		doc = self._reload()
		multi_value.replace(doc, self.field_key, "", [self.values[0]])
		multi_value.replace(doc, self.field_key, _ROW_KEYS[0], [self.values[1]])
		doc.save(ignore_permissions=True)
		doc = self._reload()
		self.assertEqual(multi_value.read(doc, self.field_key, ""), [self.values[0]])
		self.assertEqual(multi_value.read(doc, self.field_key, _ROW_KEYS[0]), [self.values[1]])
		self.assertEqual(multi_value.read(doc, self.field_key, "nothing-is-here"), [])
		self.assertEqual(
			multi_value.read_all(doc),
			{(self.field_key, ""): [self.values[0]], (self.field_key, _ROW_KEYS[0]): [self.values[1]]},
		)

	def test_replace_drops_what_was_there_and_leaves_every_other_address_alone(self):
		doc = self._reload()
		multi_value.replace(doc, self.field_key, _ROW_KEYS[0], [self.values[0], self.values[1]])
		multi_value.replace(doc, self.field_key, _ROW_KEYS[1], [self.values[2]])
		doc.save(ignore_permissions=True)
		doc = self._reload()
		multi_value.replace(doc, self.field_key, _ROW_KEYS[0], [self.values[2]])
		doc.save(ignore_permissions=True)
		doc = self._reload()
		self.assertEqual(multi_value.read(doc, self.field_key, _ROW_KEYS[0]), [self.values[2]])
		self.assertEqual(multi_value.read(doc, self.field_key, _ROW_KEYS[1]), [self.values[2]])

	def test_a_value_picked_twice_is_stored_once_and_a_blank_is_never_stored(self):
		"""A selection is a SET: the address plus the value is the identity, so a second row of it says
		nothing. A blank is not a selection and must never become a Link to nowhere."""
		doc = self._reload()
		multi_value.replace(doc, self.field_key, "", [self.values[0], self.values[0], "", None])
		doc.save(ignore_permissions=True)
		self.assertEqual(multi_value.read(self._reload(), self.field_key, ""), [self.values[0]])

	# ---------------------------------------------------------------- flattening
	def test_a_flattened_surface_shows_the_latest_rows_selections_and_not_the_union(self):
		"""CLAUDE.md: a multi-row section shows exactly ONE row, the latest by its date, in EVERY
		consumer. Merging the addresses would show a patient's March side effects beside their June ones
		as though they were one answer."""
		doc = self._reload()
		multi_value.replace(doc, self.field_key, _ROW_KEYS[0], [self.values[0], self.values[1]])
		multi_value.replace(doc, self.field_key, _ROW_KEYS[1], [self.values[2]])
		doc.save(ignore_permissions=True)
		self.assertEqual(self._latest_row_key(), _ROW_KEYS[1])
		self.assertEqual(self._panel_field()["value"], [self.values[2]])

	# ---------------------------------------------------------------- agreement
	def test_every_consumer_answers_the_same_thing_about_the_same_row(self):
		doc = self._reload()
		multi_value.replace(doc, self.field_key, _ROW_KEYS[0], [self.values[0], self.values[1]])
		multi_value.replace(doc, self.field_key, _ROW_KEYS[1], [self.values[2]])
		doc.save(ignore_permissions=True)

		held = multi_value.read_all(self._reload())
		cells, columns = self._rows_cells()
		curated = self._curated_rows()
		panel = self._panel_field()
		latest = self._latest_row_key()

		# The rows table is the history layer: every row, its own selections.
		# Compared through the resolver: each consumer answers the SAME selections in its own vocabulary — a partner sends and reads labels, the panel keeps the key a picker must send back and carries `display` beside it.
		from tatva_connect.taxonomy import labels
		master = multi_value.value_field().options

		def shown(row_key):
			return [labels.label(v, master) for v in held[(self.field_key, row_key)]]

		self.assertEqual(curated[_ROW_KEYS[0]], shown(_ROW_KEYS[0]))
		self.assertEqual(curated[_ROW_KEYS[1]], shown(_ROW_KEYS[1]))
		# The rows table serves LABELS too, through that same resolver.
		for row_key in _ROW_KEYS:
			self.assertEqual(cells[row_key], shown(row_key),
			                 f"the rows table and the resolver disagree at {row_key}")
		# And it is offered to neither sort nor filter, because SQL cannot address it.
		column = next(c for c in columns if c["key"] == self.fieldname)
		self.assertFalse(column["sortable"])
		self.assertFalse(column["filterable"])
		# The panel is the flattened layer: the LATEST row, and it agrees with that row everywhere else.
		self.assertEqual(panel["value"], held[(self.field_key, latest)])
		self.assertEqual(panel["display"], curated[latest], "the panel's label and the partner projection must agree")
		self.assertFalse(panel["empty"])

	def test_a_lead_with_no_selections_reads_empty_everywhere_rather_than_absent_in_one_place(self):
		"""An omitted key in one consumer and a None in another IS a disagreement — a client branching on
		`in` and a client branching on truthiness would then draw different screens."""
		cells, _columns = self._rows_cells()
		curated = self._curated_rows()
		panel = self._panel_field()
		self.assertEqual(multi_value.read_all(self._reload()), {})
		self.assertEqual(panel["value"], [])
		self.assertTrue(panel["empty"])
		for row_key in _ROW_KEYS:
			self.assertEqual(curated[row_key], [], "the partner projection must answer [], not omit the key")
			self.assertEqual(cells[row_key], [])

	# ---------------------------------------------------------------- round trip
	def test_a_write_through_the_data_tab_is_visible_through_the_partner_projection(self):
		detail.update_lead_detail(self.lead.name, frappe.as_json({self.field_key: [self.values[1]]}))
		latest = self._latest_row_key()
		from tatva_connect.taxonomy import labels
		label = labels.label(self.values[1], multi_value.value_field().options)
		# The panel keeps the key a picker sends back and shows the label beside it; the partner projection has no picker and reads as the label it would be sent.
		self.assertEqual(self._panel_field()["value"], [self.values[1]])
		self.assertEqual(self._panel_field()["display"], [label])
		self.assertEqual(self._curated_rows()[latest], [label])
		self.assertEqual(multi_value.read(self._reload(), self.field_key, latest), [self.values[1]])

	def test_the_catalog_row_can_still_be_saved_once_the_box_is_ticked(self):
		"""RED before the controller knew: `_fieldname_resolves_against_the_section` resolves a fieldname
		against the section's target doctype, and a multi-value field deliberately has no column there —
		so an operator opening the row in Desk and saving it was refused, on the very row that declares
		the shape. The exemption is the same one a key-value section already has."""
		frappe.get_doc("CRM Lead API Field", self.field_key).save(ignore_permissions=True)

	def test_the_field_is_writable_even_though_its_section_has_no_column_for_it(self):
		"""`_is_readonly` fails closed on a field it cannot resolve to a docfield, which is exactly what a
		multi-value field looks like. The catalog tick is what makes it writable, and it is asked."""
		self.assertFalse(detail._is_readonly(self.section, self.fieldname, is_multi_value=True))
		self.assertTrue(detail._is_readonly(self.section, self.fieldname))
