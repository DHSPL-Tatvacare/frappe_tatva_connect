# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A lead's own search row is written once, by frappe's on_update; our job restamps only the child rows,
which copy nothing but the lead's name. So a phone, stage or grain change queues no search job."""
import os
import shutil
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from tatva_connect.search import index as search_index
from tatva_connect.search.index import _PENDING, TOGGLE, CRMLeadSearch

PHONE_PREFIX = "+91610019"


class TestIndexWritesOnce(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		_purge()
		cls.toggle_was = frappe.db.get_value("CRM Tatva Automation", TOGGLE, "enabled")
		frappe.db.set_value("CRM Tatva Automation", TOGGLE, "enabled", 1)
		cls.lead = _lead(1)
		cls.file = frappe.get_doc({
			"doctype": "File", "file_name": f"zz_index_once_{frappe.generate_hash(length=8)}.txt",
			"content": frappe.generate_hash(length=16), "is_private": 1,
			"attached_to_doctype": "CRM Lead", "attached_to_name": cls.lead,
		}).insert(ignore_permissions=True).name  # authz-ok: tier-a — test fixture, runs as Administrator
		frappe.db.commit()
		# The index is a file the rollback cannot undo, so the site's own is set aside and restored.
		engine = CRMLeadSearch()
		cls.backup = f"{engine.db_path}.index-writes-once.bak"
		if os.path.exists(engine.db_path):
			shutil.copy2(engine.db_path, cls.backup)
		engine.drop_index()
		engine.build_index()

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		_restore(CRMLeadSearch().db_path, cls.backup)
		frappe.delete_doc("File", cls.file, force=True, ignore_permissions=True)
		_purge()
		frappe.db.set_value("CRM Tatva Automation", TOGGLE, "enabled", cls.toggle_was)
		frappe.db.commit()
		super().tearDownClass()

	def setUp(self):
		self.addCleanup(frappe.db.rollback)
		self.addCleanup(lambda: hasattr(frappe.local, _PENDING) and delattr(frappe.local, _PENDING))
		self.enqueued = []
		recorder = patch("frappe.enqueue", lambda method, **_kwargs: self.enqueued.append(method))
		recorder.start()
		self.addCleanup(recorder.stop)

	def test_a_change_the_child_rows_do_not_copy_queues_nothing(self):
		doc = frappe.get_doc("CRM Lead", self.lead)
		doc.mobile_no = f"{PHONE_PREFIX}9999"
		doc.save(ignore_permissions=True)
		self.assertFalse(getattr(frappe.local, _PENDING, None), "a phone change queued a child-row reindex")

	def test_a_name_change_restamps_only_the_file_row(self):
		doc = frappe.get_doc("CRM Lead", self.lead)
		doc.first_name = "Zzrenamed"
		doc.save(ignore_permissions=True)
		self.assertEqual(getattr(frappe.local, _PENDING, None), {self.lead}, "a name change queued no reindex")

		written, real = [], CRMLeadSearch.index_doc

		def recording(engine, doctype, name):
			written.append((doctype, name))
			return real(engine, doctype, name)

		with patch.object(CRMLeadSearch, "index_doc", recording):
			search_index.reindex_leads([self.lead])
		self.assertEqual(written, [("File", self.file)], "the job rewrote the lead row frappe already wrote")

		row = CRMLeadSearch().sql(
			"SELECT lead, lead_name, stage, phone, vertical FROM search_fts WHERE doc_id = ?", [f"File:{self.file}"], read_only=True
		)[0]
		self.assertEqual((row["lead"], row["lead_name"]), (self.lead, doc.lead_name))
		self.assertFalse(row["stage"] or row["phone"] or row["vertical"], "a file row still copies the lead's stage, phone or grain")

	def test_deals_are_not_indexed(self):
		self.assertNotIn("CRM Deal", CRMLeadSearch().doc_configs)


def _restore(db_path, backup):
	if os.path.exists(backup):
		shutil.move(backup, db_path)
	elif os.path.exists(db_path):
		os.unlink(db_path)


def _purge():
	for name in frappe.get_all("CRM Lead", filters={"mobile_no": ["like", f"{PHONE_PREFIX}%"]}, pluck="name"):
		frappe.delete_doc("CRM Lead", name, force=True, ignore_permissions=True)


def _lead(n):
	return frappe.get_doc({
		"doctype": "CRM Lead", "first_name": "Zzindexonce", "last_name": f"N{n}", "status": "New",
		"mobile_no": f"{PHONE_PREFIX}{n:04d}",
	}).insert(ignore_permissions=True).name
