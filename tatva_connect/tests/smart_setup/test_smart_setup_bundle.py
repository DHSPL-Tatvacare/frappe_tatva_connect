# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Every recipe, built on this site and applied back to it: the round trip is the proof. The refusals prove a
bad import writes nothing.

Every test rolls back; `apply`'s commit is replaced, so nothing persists.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \\
        --module tatva_connect.tests.smart_setup.test_smart_setup_bundle
"""
import unittest
from unittest.mock import patch

import frappe

from tatva_connect.smart_setup import bundle, recipes

DRAFT = "Draft"


def _a_root(recipe):
	"""One root of `recipe` its own controller accepts and lets go of today (a Draft, for a workflow). A row saved
	before a newer rule, or one another record still uses, is refused by design, and a round trip measures neither."""
	doctype = recipes.get(recipe)["root"]
	filters = {"lifecycle_state": DRAFT} if recipe == "Workflow" else {}
	for name in frappe.get_all(doctype, filters=filters, pluck="name", order_by="modified desc", limit=20):
		frappe.db.savepoint("pick_root")
		try:
			frappe.get_doc(doctype, name).run_method("validate")
			_remove(doctype, name)
			return name
		except frappe.ValidationError:
			frappe.clear_messages()
		finally:
			frappe.db.rollback(save_point="pick_root")
	return None


def _remove(doctype, name):
	"""Delete a root so a check has something to create; a workflow's nodes go with it. The delete's own
	background housekeeping is held: a test run executes queued jobs inline, and they commit."""
	with patch.object(frappe, "enqueue"):
		if doctype == "CRM Workflow":
			for node in frappe.get_all("CRM Workflow Node", filters={"workflow": name}, pluck="name"):
				frappe.delete_doc("CRM Workflow Node", node, force=True, ignore_permissions=True)
		frappe.delete_doc(doctype, name, force=True, ignore_permissions=True)


class TestSmartSetupBundle(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		frappe.set_user("Administrator")

	def tearDown(self):
		frappe.db.rollback()

	def _bundle(self, recipe, root):
		return bundle.read(bundle.build(recipe, [root]))

	def _apply(self, b):
		with patch.object(frappe.db, "commit") as committed:
			results = bundle.apply(b)
		return results, committed.called

	def _round_trip(self, recipe):
		"""Build, remove the root, check that it would be created, apply, and find it exactly as it was."""
		root = _a_root(recipe)
		if not root:
			self.skipTest(f"no {recipe} on this bench can be removed and recreated")
		b = self._bundle(recipe, root)
		doctype = recipes.get(recipe)["root"]
		original = next(r for r in b["records"] if r["doctype"] == doctype and r["name"] == root)
		self.assertEqual({r["action"] for r in bundle.check(b)}, {bundle.UNCHANGED})
		_remove(doctype, root)
		actions = {(r["ref_doctype"], r["record"]): r["action"] for r in bundle.check(b)}
		self.assertEqual(actions[(doctype, root)], bundle.CREATED)
		_remove(doctype, root)  # check rolled the delete back with everything else
		results, committed = self._apply(b)
		self.assertTrue(committed, [r for r in results if r["action"] == bundle.REFUSED])
		self.assertTrue(bundle.same(bundle._adapter(doctype).read(doctype, root), original))

	def test_an_api_contract_round_trips(self):
		self._round_trip("API contract")

	def test_a_task_type_round_trips(self):
		self._round_trip("Task type")

	def test_a_web_form_round_trips(self):
		self._round_trip("Web form")

	def test_a_workflow_round_trips(self):
		self._round_trip("Workflow")

	def test_one_refused_record_leaves_nothing_behind(self):
		root = _a_root("API contract")
		b = self._bundle("API contract", root)
		_remove("CRM Lead API Mapping", root)
		b["records"][-1]["vertical"] = "No Such Vertical"
		results, committed = self._apply(b)
		self.assertFalse(committed, "a bundle with a refused record must not commit")
		self.assertIn(bundle.REFUSED, {r["action"] for r in results})
		self.assertNotEqual(frappe.db.get_value("CRM Lead API Mapping", root, "vertical"), "No Such Vertical")

	def test_a_missing_prerequisite_is_named(self):
		root = _a_root("API contract")
		b = self._bundle("API contract", root)
		b["records"][-1]["partner_user"] = "nobody-here@example.com"
		refused = [r for r in bundle.check(b) if r["action"] == bundle.REFUSED]
		self.assertEqual([r["record"] for r in refused], [root])
		self.assertIn("Needs User nobody-here@example.com", refused[0]["message"])

	def test_a_missing_record_of_a_carried_doctype_is_named_the_same_way(self):
		"""RED before: a link to a doctype the recipe carries skipped the check and reached frappe's own sentence."""
		root = frappe.get_all("CRM Task Type", pluck="name", limit=1)[0]
		b = self._bundle("Task type", root)
		next(r for r in b["records"] if r["name"] == root)["vertical"] = "ZZ No Such Product Line"
		refused = [r for r in bundle.check(b) if r["action"] == bundle.REFUSED]
		self.assertEqual([r["record"] for r in refused], [root])
		self.assertEqual(refused[0]["message"], "Needs CRM Vertical ZZ No Such Product Line: set it up on this site first.")

	def test_a_live_workflow_is_never_overwritten(self):
		live = frappe.get_all("CRM Workflow", filters={"lifecycle_state": ["!=", DRAFT]}, pluck="name", limit=1)
		if not live:
			self.skipTest("no released workflow on this bench")
		b = self._bundle("Workflow", live[0])
		next(r for r in b["records"] if r["doctype"] == "CRM Workflow")["canvas_json"] = "{}"
		refused = [r for r in bundle.check(b) if r["action"] == bundle.REFUSED]
		self.assertEqual([r["record"] for r in refused], [live[0]])
		self.assertIn("Revise it", refused[0]["message"])
		self.assertNotIn("<", refused[0]["message"], "a verdict is plain text: the grid shows markup literally")

	def test_a_fault_on_our_side_reaches_the_error_log(self):
		"""RED before this: a code fault read as the record's own refusal, and the check's rollback left no trace."""
		from frappe.deferred_insert import queue_prefix

		b = self._bundle("API contract", _a_root("API contract"))
		queue = f"{queue_prefix}Error Log"
		before = frappe.cache.llen(queue)
		with patch.object(bundle, "same", side_effect=RuntimeError("probe fault")):
			results = bundle.check(b)
		self.assertEqual({r["action"] for r in results}, {bundle.REFUSED})
		self.assertIn("Error Log", results[0]["message"])
		self.assertEqual(frappe.cache.llen(queue) - before, len(results), "every fault is queued, past the rollback")
		frappe.cache.ltrim(queue, 0, before - 1) if before else frappe.cache.delete_value(queue)

	def test_an_unsafe_bundle_is_refused(self):
		b = self._bundle("API contract", _a_root("API contract"))
		for tamper in (
			lambda x: x.update(format="something-else"),
			lambda x: x.update(version=bundle.VERSION + 1),
			lambda x: x["records"].append({"doctype": "User", "name": "someone@example.com"}),
		):
			copy = frappe.parse_json(frappe.as_json(b))
			tamper(copy)
			with self.assertRaises(frappe.ValidationError):
				bundle.read(frappe.as_json(copy))
		meta = frappe.get_meta("CRM Telephony Account")
		password = meta.get("fields", {"fieldtype": "Password"})[0].fieldname
		with self.assertRaises(frappe.ValidationError):
			bundle._refuse_secrets(meta, {"name": "x", password: "s"})

	def test_a_hand_edited_bundle_cannot_name_an_owner_or_a_docstatus(self):
		"""RED before: read() kept the bookkeeping a hand-edited file carried, and the insert trusted it."""
		root = frappe.get_all("CRM Task Type", pluck="name", limit=1)[0]
		b = frappe.parse_json(bundle.build("Task type", [root]))
		for record in b["records"]:
			record.update(owner="someone@example.com", docstatus=1, creation="2020-01-01 00:00:00")
		for record in bundle.read(frappe.as_json(b))["records"]:
			self.assertFalse({"owner", "docstatus", "creation"} & set(record), record["name"])

	def test_an_export_reads_only_what_the_operator_may_read(self):
		"""RED before: the export read every record it walked to, whoever pressed Build."""
		root = frappe.get_all("CRM Task Type", pluck="name", limit=1)[0]
		frappe.set_user("Guest")
		try:
			with self.assertRaises(frappe.PermissionError):
				bundle.build("Task type", [root])
		finally:
			frappe.set_user("Administrator")

	def test_a_record_the_operator_did_not_pick_is_kept_as_this_site_has_it(self):
		"""RED before: every carried record that differed was updated, so a task type import could flip a vertical's deals."""
		root = frappe.get_all("CRM Task Type", pluck="name", limit=1)[0]
		b = self._bundle("Task type", root)
		vertical = next(r for r in b["records"] if r["doctype"] == "CRM Vertical")
		here = frappe.db.get_value("CRM Vertical", vertical["name"], "deals_enabled")
		vertical["deals_enabled"] = 0 if here else 1
		verdict = {(r["ref_doctype"], r["record"]): r for r in bundle.check(b)}[("CRM Vertical", vertical["name"])]
		self.assertEqual(verdict["action"], bundle.KEPT, verdict["message"])
		results, _committed = self._apply(b)
		self.assertEqual(frappe.db.get_value("CRM Vertical", vertical["name"], "deals_enabled"), here)

	def test_an_update_names_what_it_changes(self):
		"""RED before: Check said only 'updated', so a row an update adds or removes was invisible until Apply."""
		root = _a_root("Task type")
		if not root:
			self.skipTest("no task type on this bench validates today")
		b = self._bundle("Task type", root)
		record = next(r for r in b["records"] if r["name"] == root)
		probe = dict(next(row for row in record["schema"] if row.get("source") != "Lead"), fieldtype="Data", options=None)
		probe.update(fieldname="zz_probe_field", label="ZZ Probe", target=None, depends_on=None, mandatory_depends_on=None)
		record["schema"].append(probe)
		verdict = {(r["ref_doctype"], r["record"]): r for r in bundle.check(b)}[("CRM Task Type", root)]
		self.assertEqual(verdict["action"], bundle.UPDATED, verdict["message"])
		self.assertIn("Schema: 1 added, 0 removed", verdict["message"])
