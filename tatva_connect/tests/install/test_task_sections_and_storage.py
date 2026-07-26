# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Phases 1 and 1b of docs/plans/task-form-layer/2026-07-25-task-slots-to-sections-and-form-layer.md: the declaration
reaches real storage on ANY site.

An activity section is a promise with three halves — a CRM Task Section row saying where the answers land,
a Table field on CRM Task holding those rows, and a child doctype whose real named columns they are. Any
half missing and the promise is a blank: a field routed to a section nothing can write, or a section whose
target doctype was never synced. Every half is asserted here against live meta, never against a restated
list, so the seed stays the one brain. A key-value section is the same promise addressed by fieldname, so
the column it keys on and the column it answers from are asserted the same way.

The indexes are asserted for the same reason schema_setup exists: `bench install-app` BASELINES
patches.txt without running it, so a patch alone reaches every existing site and no fresh one.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \
        --module tatva_connect.tests.install.test_task_sections_and_storage
"""
import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect import schema_setup
from tatva_connect.patches import add_task_answer_fieldname_index, add_task_document_kind_index
from tatva_connect.taxonomy import task_section_seed

# Each index patch and the fresh-install twin it must appear in; asserted as one list so a new index is
# covered by having been added here rather than by anyone remembering to write another test.
_INDEX_PATCHES = (add_task_document_kind_index, add_task_answer_fieldname_index)

TASK_DOCTYPE = "CRM Task"


class TestTaskSectionsAndStorage(FrappeTestCase):
	def test_every_declared_section_has_a_row_carrying_its_structure(self):
		"""The seed's structural facts are what every consumer routes by, so they are asserted, not assumed."""
		for row in task_section_seed._ROWS:
			key = row["section_key"]
			stored = frappe.db.get_value("CRM Task Section", key, task_section_seed._STRUCTURAL, as_dict=True)
			self.assertIsNotNone(stored, f"section `{key}` was never seeded — every field naming it has no home")
			for field in task_section_seed._STRUCTURAL:
				self.assertEqual(
					frappe.utils.cstr(stored[field]), frappe.utils.cstr(row[field]),
					f"section `{key}` carries {field}={stored[field]!r} where the seed declares {row[field]!r}",
				)

	def test_every_section_reaches_a_real_child_table_on_the_task(self):
		"""The row names a Table field and a target doctype; both must exist and agree, or a write lands nowhere."""
		meta = frappe.get_meta(TASK_DOCTYPE)
		for row in task_section_seed._ROWS:
			field = meta.get_field(row["child_table_field"])
			self.assertIsNotNone(field, f"{TASK_DOCTYPE} has no `{row['child_table_field']}` field")
			self.assertEqual(field.fieldtype, "Table", f"`{row['child_table_field']}` is not a Table field")
			self.assertEqual(
				field.options, row["target_doctype"],
				f"`{row['child_table_field']}` holds {field.options} rows, not {row['target_doctype']}",
			)
			self.assertTrue(
				frappe.get_meta(row["target_doctype"]).istable,
				f"{row['target_doctype']} is not a child table — its rows would carry an independent permission set",
			)

	def test_a_multi_row_section_addresses_a_real_column(self):
		"""Multi-row is meaningless without an address: the row key must be a column of the target."""
		for row in task_section_seed._ROWS:
			if not row["is_multi_row"]:
				continue
			target_meta = frappe.get_meta(row["target_doctype"])
			self.assertIsNotNone(
				target_meta.get_field(row["row_key_field"]),
				f"section `{row['section_key']}` keys on {row['row_key_field']}, not a column of {row['target_doctype']}",
			)

	def test_a_key_value_section_addresses_a_real_column_and_names_where_the_answer_lives(self):
		"""Key-value rows ARE the fields, so the address and the answer column must both be real."""
		for row in task_section_seed._ROWS:
			if not row["is_key_value"]:
				continue
			target_meta = frappe.get_meta(row["target_doctype"])
			for field in ("row_key_field", "value_field", "question_field"):
				named = row[field]
				self.assertTrue(named, f"key-value section `{row['section_key']}` names no {field} — nothing can read one of its rows")
				self.assertIsNotNone(
					target_meta.get_field(named),
					f"section `{row['section_key']}` names {field}={named}, not a column of {row['target_doctype']}",
				)
			self.assertFalse(
				row["is_multi_row"],
				f"section `{row['section_key']}` is key-value AND multi-row — it already holds exactly one row per field",
			)

	def test_the_child_table_indexes_exist(self):
		"""Without them the latest-by-kind pick and the per-fieldname read full-scan every row on the site."""
		for patch in _INDEX_PATCHES:
			name, _columns = patch._INDEX
			self.assertTrue(
				frappe.db.has_index(patch._TABLE, name),
				f"`{name}` is missing from {patch._TABLE} — the pick reads the table without an index",
			)

	def test_every_index_patch_has_its_fresh_install_twin(self):
		"""install-app baselines patches.txt WITHOUT running it, so a patch alone never reaches a new site."""
		for patch in _INDEX_PATCHES:
			self.assertIn(
				patch, schema_setup._STEPS,
				f"{patch.__name__} is not in schema_setup._STEPS — a fresh site would never get the index, and nothing would go red",
			)
