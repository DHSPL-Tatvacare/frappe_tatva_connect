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
from copy import deepcopy
from unittest.mock import patch

import frappe

from tatva_connect.intake import builder
from tatva_connect.smart_setup import bundle, parts, recipes

DRAFT = "Draft"


def _a_root(recipe):
	"""One root of `recipe` its own controller accepts and lets go of today (a Draft, for a workflow). A row saved
	before a newer rule, or one another record still uses, is refused by design, and a round trip measures neither."""
	doctype = recipes.get(recipe)["root"]
	filters = {"lifecycle_state": DRAFT} if doctype in ("CRM Workflow", "CRM Task Type") else {}  # only a Draft takes an edit
	if doctype == "CRM Task Type":  # `_probe_row` copies one of the type's own non-lead fields
		filters["name"] = ["in", frappe.get_all("CRM Task Type Field", filters={"parenttype": doctype, "source": ["!=", "Lead"]},
		                                         pluck="parent", distinct=True) or [""]]
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


def _probe_row(task_type):
	"""A plain activity field a task type accepts today, copied off one of its own rows."""
	row = dict(next(row for row in task_type["schema"] if row.get("source") != "Lead"), fieldtype="Data", options=None)
	row.update(fieldname="zz_probe_field", label="ZZ Probe", target=None, depends_on=None, mandatory_depends_on=None)
	return row


def _renamed(record, prefix):
	"""A copy of `record` under a new name, as a second setup built on the source site would arrive."""
	copy = deepcopy(record)
	for key in ("name", "type_name", "workflow_name", "title", "label"):
		if isinstance(copy.get(key), str):
			copy[key] = f"{prefix} {copy[key]}"
	return copy


class TestSmartSetupBundle(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		frappe.set_user("Administrator")

	def tearDown(self):
		frappe.db.rollback()

	def _bundle(self, recipe, root):
		return bundle.read(bundle.build(recipe, [root]))

	def _apply(self, b, before_commit=None):
		with patch.object(frappe.db, "commit") as committed:
			results = bundle.apply(b, before_commit=before_commit)
		return results, committed.called

	def _applied_with_restore_point(self, b):
		"""Apply as the worker does, and return the restore point it writes in the apply's own transaction, read back."""
		before, kept = bundle.versions(b), {}
		results, committed = self._apply(b, before_commit=lambda done: kept.update(text=bundle.restore_point(b, before, done)))
		self.assertTrue(committed, [r for r in results if r["action"] == bundle.REFUSED])
		return bundle.read(kept["text"])

	def _task_type_update_and_create(self):
		"""A task type bundle that updates its root (a new field) and creates a renamed copy of it."""
		root = _a_root("Task type")
		if not root:
			self.skipTest("no task type on this bench validates today")
		b = self._bundle("Task type", root)
		record = next(r for r in b["records"] if r["name"] == root)
		copy = _renamed(record, "ZZ Restore")
		record["schema"].append(_probe_row(record))
		b["records"].append(copy)
		return b, root, copy["name"]

	def _round_trip(self, recipe):
		"""Build, remove the root, check that it would be created, apply, and find it exactly as it was, switched off."""
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
		landed = deepcopy(original)
		parts.lands(landed)
		self.assertTrue(bundle.same(bundle._adapter(doctype).read(doctype, root), landed))

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
		b["records"][-1]["vertical"] = "zz-no-such-vertical"
		refused = [r for r in bundle.check(b) if r["action"] == bundle.REFUSED]
		self.assertEqual([r["record"] for r in refused], [root])
		self.assertIn("Needs CRM Vertical zz-no-such-vertical", refused[0]["message"])

	def test_an_import_creates_a_record_switched_off_and_a_reimport_keeps_this_sites_state(self):
		"""RED before: an import created a record as live as it was on the source, and a re-import switched it back."""
		root = _a_root("API contract")
		b = self._bundle("API contract", root)
		next(r for r in b["records"] if r["name"] == root)["enabled"] = 1
		_remove("CRM Lead API Mapping", root)
		results, committed = self._apply(b)
		self.assertTrue(committed, [r for r in results if r["action"] == bundle.REFUSED])
		self.assertEqual({r["record"]: r["arrives_as"] for r in results}[root], parts.SWITCHED_OFF)
		self.assertEqual(frappe.db.get_value("CRM Lead API Mapping", root, ["enabled", "partner_user"]), (0, None))
		parts.turn_on("CRM Lead API Mapping", root)
		self.assertEqual({r["record"]: r["action"] for r in bundle.check(b)}[root], bundle.UNCHANGED)
		self.assertEqual(frappe.db.get_value("CRM Lead API Mapping", root, "enabled"), 1)

	def test_an_intake_form_from_a_file_written_before_its_shape_reads_unchanged(self):
		"""A restore point or bundle saved by an earlier release still carries the builder's derived fields; it must still match."""
		root = _a_root("Web form")
		if not root:
			self.skipTest("no intake form on this bench validates today")
		b = frappe.parse_json(bundle.build("Web form", [root]))
		record = next(r for r in b["records"] if r["doctype"] == "CRM Intake Form")
		record.update(web_form_doctype=frappe.db.get_value("CRM Intake Form", root, "web_form_doctype") or "ZZ Old Sink",
		              route=builder.default_route(record["form_name"]))
		actions = {r["record"]: r["action"] for r in bundle.check(bundle.read(frappe.as_json(b)))}
		self.assertEqual(actions[root], bundle.UNCHANGED)

	def test_a_renamed_workflow_imports_under_its_id_with_its_title(self):
		"""RED before: the import created the workflow under its title, then saved its graph under an id nothing held."""
		root = _a_root("Workflow")
		if not root:
			self.skipTest("no Draft workflow on this bench can be removed and recreated")
		b = self._bundle("Workflow", root)
		record = next(r for r in b["records"] if r["doctype"] == "CRM Workflow")
		record["workflow_name"] = f"ZZ Renamed {record['workflow_name']}"
		_remove("CRM Workflow", root)
		results, committed = self._apply(b)
		self.assertTrue(committed, [r for r in results if r["action"] == bundle.REFUSED])
		self.assertEqual(frappe.db.get_value("CRM Workflow", root, "workflow_name"), record["workflow_name"])
		self.assertEqual({r["record"]: r["action"] for r in bundle.check(b)}[root], bundle.UNCHANGED)

	def test_a_check_that_creates_an_intake_form_builds_nothing(self):
		"""RED before: with web intake on, the check scaffolded the form's DocType, and that DDL committed the check."""
		root = _a_root("Web form")
		if not root:
			self.skipTest("no intake form on this bench can be removed and recreated")
		b = self._bundle("Web form", root)
		_remove("CRM Intake Form", root)
		is_enabled = builder.automation.is_enabled
		with patch.object(builder.automation, "is_enabled", side_effect=lambda key: key == builder.INTAKE_SWITCH or is_enabled(key)), \
		     patch.object(builder, "_ensure_doctype", side_effect=AssertionError("a check built the intake form's DocType")):
			actions = {r["record"]: r["action"] for r in bundle.check(b)}
		self.assertEqual(actions[root], bundle.CREATED)

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
		_results, _committed = self._apply(b)
		self.assertEqual(frappe.db.get_value("CRM Vertical", vertical["name"], "deals_enabled"), here)

	def test_an_update_names_what_it_changes(self):
		"""RED before: Check said only 'updated', so a row an update adds or removes was invisible until Apply."""
		root = _a_root("Task type")
		if not root:
			self.skipTest("no task type on this bench validates today")
		b = self._bundle("Task type", root)
		record = next(r for r in b["records"] if r["name"] == root)
		record["schema"].append(_probe_row(record))
		verdict = {(r["ref_doctype"], r["record"]): r for r in bundle.check(b)}[("CRM Task Type", root)]
		self.assertEqual(verdict["action"], bundle.UPDATED, verdict["message"])
		self.assertIn("Schema: 1 added, 0 removed", verdict["message"])

	def test_a_restore_puts_back_every_update_and_removes_every_create(self):
		"""RED before: nothing an apply changed could be put back."""
		b, root, copy = self._task_type_update_and_create()
		before = bundle._adapter("CRM Task Type").read("CRM Task Type", root)
		restore = self._applied_with_restore_point(b)
		self.assertEqual(bundle.size(restore), 2, "a restore acts on the update it puts back and the create it removes")
		self.assertTrue(frappe.db.exists("CRM Task Type", copy))
		results, committed = self._apply(restore)
		self.assertTrue(committed, [r for r in results if r["action"] == bundle.REFUSED])
		self.assertEqual({(r["record"], r["action"]) for r in results}, {(root, bundle.UPDATED), (copy, bundle.REMOVED)})
		self.assertTrue(bundle.same(bundle._adapter("CRM Task Type").read("CRM Task Type", root), before))
		self.assertFalse(frappe.db.exists("CRM Task Type", copy))

	def test_a_restore_refuses_a_record_changed_after_the_apply(self):
		"""A later edit on production is never overwritten: the record is refused and named."""
		b, root, _copy = self._task_type_update_and_create()
		restore = self._applied_with_restore_point(b)
		doc = frappe.get_doc("CRM Task Type", root)
		doc.description = "Edited on this site after the apply"
		doc.save()
		refused = [r for r in bundle.check(restore) if r["action"] == bundle.REFUSED]
		self.assertEqual([(r["record"], r["message"]) for r in refused],
		                 [(root, "Changed on this site after the apply; restore it by hand.")])

	def test_a_restore_does_not_remove_a_record_something_now_links_to(self):
		"""A created record now in use stays: frappe's own delete check refuses it, in its own sentence."""
		b, _root, copy = self._task_type_update_and_create()
		restore = self._applied_with_restore_point(b)
		frappe.get_doc({"doctype": "CRM Task Option", "task_type": copy, "fieldname": "zz_probe_field",
		                "option_value": "ZZ Probe"}).insert()
		refused = [r for r in bundle.check(restore) if r["action"] == bundle.REFUSED]
		self.assertEqual([r["record"] for r in refused], [copy])
		self.assertIn("linked", refused[0]["message"])

	def test_a_smart_setup_naming_a_record_never_keeps_it_from_being_removed(self):
		"""RED before: the apply's own verdict row linked the record it created, so a restore could never remove it."""
		b, _root, copy = self._task_type_update_and_create()
		restore = self._applied_with_restore_point(b)
		frappe.get_doc({"doctype": "CRM Smart Setup", "direction": "Export", "recipe": "Task type",
		                "roots": [{"root_doctype": "CRM Task Type", "record": copy}]}).insert()
		self.assertIn((copy, bundle.REMOVED), {(r["record"], r["action"]) for r in bundle.check(restore)})

	def test_a_restore_refuses_a_workflow_published_after_the_apply(self):
		"""A workflow the apply created is removed only while it is still a Draft."""
		root = _a_root("Workflow")
		if not root:
			self.skipTest("no Draft workflow on this bench can be copied")
		b = self._bundle("Workflow", root)
		copy = _renamed(next(r for r in b["records"] if r["doctype"] == "CRM Workflow"), "ZZ Restore")
		b["records"].append(copy)
		restore = self._applied_with_restore_point(b)
		published = frappe.get_doc("CRM Workflow", copy["name"])
		published.lifecycle_state = "Active"
		published.save()
		refused = [r for r in bundle.check(restore) if r["action"] == bundle.REFUSED]
		self.assertEqual([r["record"] for r in refused], [copy["name"]])
		self.assertIn("not a Draft", refused[0]["message"])
