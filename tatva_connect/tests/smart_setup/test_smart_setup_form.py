# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The form never shows one thing while the file or records say another. Each test was red before its fix.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \\
        --module tatva_connect.tests.smart_setup.test_smart_setup_form
"""
import unittest
from unittest.mock import patch

import frappe

from tatva_connect.smart_setup import api, bundle

DOCTYPE = "CRM Smart Setup"
RECIPE = "Task type"


def _a_task_type():
	return frappe.get_all("CRM Task Type", pluck="name", order_by="modified desc", limit=1)[0]


def _bundle_file(setup, text):
	"""A bundle attached to `setup` the way an upload or a Build attaches it."""
	return frappe.get_doc({"doctype": "File", "file_name": f"zz-smart-setup-{frappe.generate_hash(length=6)}.json",
	                       "attached_to_doctype": DOCTYPE, "attached_to_name": setup,
	                       "attached_to_field": "bundle_file", "content": text}).insert(ignore_permissions=True)  # authz-ok: tier-a — test fixture


class TestSmartSetupForm(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		frappe.set_user("Administrator")
		cls.root = _a_task_type()
		cls.text = bundle.build(RECIPE, [cls.root])

	def tearDown(self):
		frappe.db.rollback()

	def _import(self, status):
		"""A saved import holding a bundle, moved to `status` the way a stage moves it."""
		doc = frappe.get_doc({"doctype": DOCTYPE, "direction": "Import"}).insert()
		doc.bundle_file = _bundle_file(doc.name, self.text).file_url
		doc.save()
		doc.db_set({"status": status, "created_count": 2})
		doc.reload()
		return doc

	def _row(self):
		"""A records row as the form adds one: its doctype set from the recipe."""
		return {"root_doctype": "CRM Task Type", "record": self.root}

	def _built_export(self):
		"""An export in the state Build leaves it: a file attached, its status Built."""
		doc = frappe.get_doc({"doctype": DOCTYPE, "direction": "Export", "recipe": RECIPE,
		                      "roots": [self._row()]}).insert()
		doc.db_set({"bundle_file": _bundle_file(doc.name, self.text).file_url, "status": "Built"})
		doc.reload()
		return doc

	def test_removing_the_file_drops_the_verdict_and_offers_nothing(self):
		doc = self._import("Checked")
		doc.bundle_file = None
		doc.save()
		self.assertEqual((doc.status, doc.created_count, doc.record_count), ("Draft", 0, 0))
		self.assertIsNone(api.next_stage(doc), "Apply was offered with no file")

	def test_nothing_on_a_setup_changes_while_its_stage_runs_but_the_stage_writes_its_end(self):
		doc = self._import("Checking")
		with patch.object(api, "is_job_enqueued", return_value=True):
			doc.bundle_file = None
			with self.assertRaises(frappe.ValidationError) as caught:
				doc.save()
			self.assertIn("Wait for it to end", str(caught.exception))
			frappe.clear_messages()
			doc.reload()
			doc.status, doc.flags.ends_stage = "Checked", True
			doc.save()
		self.assertEqual(doc.status, "Checked")

	def test_a_stage_whose_worker_was_cut_off_is_offered_again(self):
		doc = self._import("Checking")
		with patch.object(api, "is_job_enqueued", return_value=True):
			self.assertIsNone(api.next_stage(doc), "a live stage must not be offered twice")
		self.assertEqual(api.next_stage(doc), "check", "a Checking setup with no worker had no way out")

	def test_editing_an_export_after_build_drops_the_built_file(self):
		doc = self._built_export()
		built = doc.bundle_file
		doc.append("roots", self._row())
		doc.save()
		self.assertEqual((doc.status, doc.bundle_file), ("Draft", None))
		self.assertFalse(frappe.db.exists("File", {"file_url": built, "attached_to_name": doc.name}),
		                 "the stale bundle could still be downloaded")
		self.assertEqual(api.next_stage(doc), "build")

	def test_a_check_through_the_worker_records_a_record_it_would_create(self):
		"""RED before: the verdict row linked a record the check had rolled back, so every check with a create crashed."""
		b = frappe.parse_json(self.text)
		new = next(r for r in b["records"] if r["name"] == self.root)
		for key in ("name", "type_name", "title", "label"):
			if isinstance(new.get(key), str):
				new[key] = f"ZZ Worker Check {new[key]}"
		doc = frappe.get_doc({"doctype": DOCTYPE, "direction": "Import"}).insert()
		doc.bundle_file = _bundle_file(doc.name, frappe.as_json(b)).file_url
		doc.save()
		frappe.db.commit()  # the worker runs on a committed setup and commits its own verdict
		try:
			api.run(doc.name, "check")
			doc.reload()
			self.assertEqual((doc.status, doc.created_count, doc.error), ("Checked", 1, None))
			self.assertFalse(frappe.db.exists("CRM Task Type", new["name"]), "a check wrote the record it only checked")
		finally:
			frappe.delete_doc(DOCTYPE, doc.name, force=True, ignore_permissions=True)  # authz-ok: tier-a — test fixture teardown
			frappe.db.commit()
