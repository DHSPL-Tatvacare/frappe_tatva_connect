# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The client branches on the COLUMN, so the descriptor has to reach the column — and only the column.

`_announce` already puts the descriptor in `result["fields"]`, but a table renders each cell from the
matching entry in `result["columns"]`, and that dict is the CALLER's: `ColumnSettings.vue:237-244` builds
`{label, type, key, options, width, align}` and has never carried `is_derived`. So every renderer had to
branch on the fieldname itself (`TasksListView.vue:42`), which is exactly what stops a derived field on
another doctype from rendering correctly with no second edit.

Three properties:

  * THE DERIVED COLUMN CARRIES IT. `is_derived` is on the column dict, not only in `fields`, and the
    caller's position, label, width and align are returned untouched beside it.
  * THE STORED VIEW GETS THE SAME TREATMENT. Native replaces `columns` with the rep's saved view when the
    caller sends none (`crm/api/doc.py:329-334`), so the derived column can reach the answer without ever
    being in `asked_columns`. It is stamped there too, and never duplicated.
  * NOTHING ELSE MOVES. A real column dict is byte-identical to what native returns for it.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \
        --module tatva_connect.tests.list_engine.test_descriptor
"""

import copy

import frappe

from tatva_connect.list_engine import fields
from tatva_connect.tests.list_engine.test_list_engine import FIELD, PROBE, TASK, ListEngineCase, _rows_arg

# The shape ColumnSettings really sends — six keys, no is_derived among them.
ASKED = [
	{"label": "Title", "type": "Data", "key": "title", "width": "16rem", "align": "left"},
	{
		"label": "Due State",
		"type": "Select",
		"key": FIELD,
		"options": "",
		"width": "12rem",
		"align": "left",
	},
]


class TestTheColumnCarriesTheDescriptor(ListEngineCase):
	def _answer(self):
		return self._get_data(columns=copy.deepcopy(ASKED), rows=_rows_arg("name", "title", FIELD))

	def test_the_derived_column_dict_is_the_caller_s_dict_with_the_declaration_s_own_label(self):
		"""Position, type, options, width and align are the caller's. The LABEL and the flag are the
		declaration's: `CRM View Settings` stores the label a column had when the rep added it, so reading
		it back would leave a renamed field showing its old name on the one surface a rep looks at most."""
		result = self._answer()
		column = next(c for c in result["columns"] if c.get("key") == FIELD)
		self.assertEqual(result["columns"].index(column), 1, "the column moved")
		declared = fields.DUE_STATE.descriptor()
		self.assertEqual(column, {**ASKED[1], "label": declared["label"], "is_derived": 1})

	def test_a_stale_saved_label_is_replaced_by_the_declaration_s(self):
		"""What a rep's saved view holds is a snapshot, not a source. Renaming in `fields.py` moves the
		column header with the four menus, or the field answers to two names at once."""
		stale = copy.deepcopy(ASKED)
		stale[1]["label"] = "Due State"
		result = self._get_data(columns=stale, rows=_rows_arg("name", "title", FIELD))
		column = next(c for c in result["columns"] if c.get("key") == FIELD)
		self.assertEqual(column["label"], fields.DUE_STATE.descriptor()["label"])

	def test_the_flag_is_on_the_column_and_not_only_in_fields(self):
		"""Both readers of the response must agree, and the column is the one the renderer reads."""
		result = self._answer()
		announced = next(f for f in result["fields"] if f.get("fieldname") == FIELD)
		self.assertEqual(announced.get("is_derived"), 1)
		column = next(c for c in result["columns"] if c.get("key") == FIELD)
		self.assertIn("is_derived", column, "the client cannot branch generically without it")

	def test_a_real_column_is_byte_identical_to_what_native_returns(self):
		"""The line. A derived field named in the payload may not change one byte of a database column."""
		from crm.api.doc import get_data as native

		ours = self._answer()
		theirs = native(
			doctype=TASK,
			filters={"title": ["like", f"{PROBE}%"]},
			order_by="creation asc",
			columns=copy.deepcopy(ASKED[:1]),
			rows=_rows_arg("name", "title"),
			page_length=50,
		)
		mine = next(c for c in ours["columns"] if c.get("key") == "title")
		yours = next(c for c in theirs["columns"] if c.get("key") == "title")
		self.assertEqual(frappe.as_json(mine), frappe.as_json(yours))


class TestTheSavedViewColumnGetsTheSameTreatment(ListEngineCase):
	"""A column native loaded from `CRM View Settings` was never in `asked_columns`, so the re-insert branch
	never sees it. Driven at `project()` — the whole read of a stored view whose `rows` name a derived field
	is a separate defect, and this asserts the stamp, not that read."""

	def _stored(self):
		return {
			"data": [],
			"columns": [
				{"label": "Title", "type": "Data", "key": "title", "width": "16rem"},
				{"label": "Due State", "type": "Select", "key": FIELD, "width": "10rem"},
			],
			"rows": ["name", "title", FIELD],
			"fields": [],
		}

	def _request(self):
		from tatva_connect.list_engine.engine import ListRequest

		return ListRequest(
			{"doctype": TASK, "filters": {FIELD: "Overdue"}, "order_by": "creation desc", "page_length": 5}
		)

	def test_a_column_native_loaded_from_the_stored_view_is_stamped(self):
		result = self._request().project(self._stored())
		self.assertEqual(result["columns"][1].get("is_derived"), 1)

	def test_the_stored_column_is_stamped_in_place_and_never_duplicated(self):
		result = self._request().project(self._stored())
		self.assertEqual([c.get("key") for c in result["columns"]], ["title", FIELD])
		self.assertEqual(
			result["columns"][0], {"label": "Title", "type": "Data", "key": "title", "width": "16rem"}
		)
