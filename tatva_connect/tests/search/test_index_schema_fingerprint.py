# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The FTS index is a FILE, and the framework creates its table with `CREATE VIRTUAL TABLE IF NOT EXISTS`
(`frappe/search/sqlite_search.py:1242). On any site whose index already exists, adding a column to
`INDEX_SCHEMA` therefore reaches NOTHING: the table keeps the columns it was born with and `bench migrate`
goes green. A patch cannot fix that either — a patch runs once and is dead, while the check must hold after
every future schema edit.

So the declaration stamps itself into the index it built: a fingerprint of `INDEX_SCHEMA` +
`INDEXABLE_DOCTYPES`, written into a `search_meta` row after a finished build, compared on every
`after_migrate`. Stale ⇒ the index is dropped and a forced rebuild is enqueued — forced because
`sqlite_search.build_index` only builds when `is_continuation or force` (16.22.0 line 1772), so the
3-hourly `build_index_if_not_exists` (force=False) rebuilds a dropped index never, which this suite proved.

No mocked index anywhere: these tests back up the site's REAL index file, rebuild it for real, and restore
the original in cleanup — the DB transaction rolls back, a file does not.

Run:
    bench --site uatreplay.localhost run-tests --app tatva_connect \\
        --module tatva_connect.tests.search.test_index_schema_fingerprint
"""
import os
import shutil
from functools import partial
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils.background_jobs import enqueue

from tatva_connect.search.activation import reconcile_index_schema
from tatva_connect.search.index import TOGGLE, CRMLeadSearch

# The column P7 adds for real; here it stands for any future metadata field.
NEW_FIELD = "date_bucket"


def _schema_with_extra_field():
	# Exactly what a source edit looks like: the declared metadata list, plus one. `doctype`/`name` are the
	# framework's own additions (_get_schema appends them in place), so they are excluded to keep this the
	# same shape a fresh interpreter would read from the literal.
	base = CRMLeadSearch.INDEX_SCHEMA
	declared = [f for f in base["metadata_fields"] if f not in ("doctype", "name")]
	return {**base, "metadata_fields": [*declared, NEW_FIELD]}


class TestIndexSchemaFingerprint(FrappeTestCase):
	def setUp(self):
		frappe.db.set_value("CRM Tatva Automation", TOGGLE, "enabled", 1)
		# The rebuild the guard enqueues is a real job on the long queue; run it inline so the end state it
		# produces is asserted here rather than by a worker outside this transaction, minutes later.
		inline = patch("frappe.enqueue", partial(enqueue, now=True))
		inline.start()
		self.addCleanup(inline.stop)
		engine = CRMLeadSearch()
		self.db_path = engine.db_path
		self.backup = f"{self.db_path}.fingerprint-test.bak"
		if os.path.exists(self.db_path):
			shutil.copy2(self.db_path, self.backup)
		self.addCleanup(self._restore_site_index)
		engine.drop_index()
		engine.build_index()

	def _restore_site_index(self):
		# Put the site's own index back byte for byte, whatever the test did to it.
		if os.path.exists(self.backup):
			shutil.move(self.backup, self.db_path)
		elif os.path.exists(self.db_path):
			os.unlink(self.db_path)

	def _fts_columns(self):
		return [row["name"] for row in CRMLeadSearch().sql("PRAGMA table_info(search_fts)", read_only=True)]

	def test_fingerprint_is_identical_for_an_unchanged_declaration(self):
		self.assertEqual(CRMLeadSearch().schema_fingerprint(), CRMLeadSearch().schema_fingerprint())

	def test_fingerprint_changes_when_a_metadata_field_is_added(self):
		before = CRMLeadSearch().schema_fingerprint()
		with patch.object(CRMLeadSearch, "INDEX_SCHEMA", _schema_with_extra_field()):
			self.assertNotEqual(before, CRMLeadSearch().schema_fingerprint())
		self.assertEqual(before, CRMLeadSearch().schema_fingerprint())

	def test_a_finished_build_stamps_the_current_fingerprint(self):
		engine = CRMLeadSearch()
		self.assertEqual(engine.stored_fingerprint(), engine.schema_fingerprint())

	def test_a_stale_stamp_drops_and_rebuilds_the_index(self):
		with patch.object(CRMLeadSearch, "INDEX_SCHEMA", _schema_with_extra_field()):
			reconcile_index_schema()
			engine = CRMLeadSearch()
			self.assertTrue(engine.index_exists())
			self.assertEqual(engine.stored_fingerprint(), engine.schema_fingerprint())

	def test_a_matching_stamp_leaves_the_index_untouched(self):
		before = os.stat(self.db_path)
		reconcile_index_schema()
		after = os.stat(self.db_path)
		self.assertTrue(CRMLeadSearch().index_exists())
		self.assertEqual(
			(before.st_ino, before.st_mtime_ns, before.st_size),
			(after.st_ino, after.st_mtime_ns, after.st_size),
		)

	def test_an_index_built_before_the_stamp_existed_is_rebuilt(self):
		# The UAT upgrade case: a live index with no search_meta at all.
		CRMLeadSearch().sql("DROP TABLE search_meta", commit=True)
		self.assertIsNone(CRMLeadSearch().stored_fingerprint())
		reconcile_index_schema()
		engine = CRMLeadSearch()
		self.assertEqual(engine.stored_fingerprint(), engine.schema_fingerprint())

	def test_a_mid_build_index_is_never_dropped(self):
		CRMLeadSearch().sql("UPDATE search_index_progress SET is_complete = 0", commit=True)
		with patch.object(CRMLeadSearch, "INDEX_SCHEMA", _schema_with_extra_field()):
			reconcile_index_schema()
		self.assertTrue(CRMLeadSearch().index_exists())

	def test_the_toggle_off_is_a_no_op(self):
		frappe.db.set_value("CRM Tatva Automation", TOGGLE, "enabled", 0)
		with patch.object(CRMLeadSearch, "INDEX_SCHEMA", _schema_with_extra_field()):
			reconcile_index_schema()
		self.assertTrue(CRMLeadSearch().index_exists())

	def test_an_absent_index_is_a_no_op(self):
		CRMLeadSearch().drop_index()
		with patch.object(CRMLeadSearch, "INDEX_SCHEMA", _schema_with_extra_field()):
			reconcile_index_schema()
		self.assertFalse(CRMLeadSearch().index_exists())

	def test_a_schema_edit_reaches_the_index_only_because_of_the_guard(self):
		# The whole point, end to end: the framework's own table setup cannot add the column to a live index,
		# and only this guard turns a declaration edit into a table that really carries it.
		with patch.object(CRMLeadSearch, "INDEX_SCHEMA", _schema_with_extra_field()):
			CRMLeadSearch()._ensure_fts_table()
			self.assertNotIn(NEW_FIELD, self._fts_columns())
			reconcile_index_schema()
			self.assertIn(NEW_FIELD, self._fts_columns())
