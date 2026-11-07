# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A grain copied onto another lands every record on the target, names nothing of the source, and never overwrites."""
import unittest
from unittest.mock import patch

import frappe
from frappe.utils.data import sha256_hash

from tatva_connect.smart_setup import bundle, grain_copy, parts, recipes
from tatva_connect.workflow_engine import registry
from tatva_connect.workflows import api as workflows

SOURCE = "Goodflip-Care::Zydus::Liver-Forever"
INTAKE_SOURCE = "Goodflip-Care::Anaya::Nivolumab"
PROGRAM = "zz-copy-target"
SUFFIX = "ZZ"
MEMBERS = recipes.get(grain_copy.RECIPE)["members"]


def _members(*doctypes):
	"""The Grain recipe narrowed to these member doctypes, so a test applies what this site can hold."""
	return patch.dict(MEMBERS, {d: f for d, f in MEMBERS.items() if d in doctypes}, clear=True)


class TestSmartSetupCopy(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		frappe.set_user("Administrator")

	def setUp(self):
		if not frappe.db.exists("CRM Grain", SOURCE):
			self.skipTest(f"{SOURCE} is not on this site")
		frappe.get_doc({"doctype": "CRM Program", "program_name": PROGRAM}).insert()
		self.target = self._grain("Zydus")
		frappe.get_doc({"doctype": "Assignment Rule", "document_type": "CRM Lead", "description": "zz", "rule": "Round Robin",
		                "assigned_by_workflow": 1, "assignment_days": [{"day": "Monday"}], "users": [{"user": "Administrator"}],
		                "grain_vertical": "Goodflip-Care", "grain_group": "Zydus", "grain_program": "Liver-Forever"},
		               ).insert(set_name="zz pool Liver-Forever")
		frappe.get_doc({"doctype": "CRM Smart View", "label": "zz view", "base_object": "Lead", "is_standard": 1,
		                "vertical": "Goodflip-Care", "group": "Zydus", "program": "Liver-Forever",
		                "predicate": frappe.as_json({"op": "and", "conditions": [
			                {"field": "lead:program", "operator": "=", "value": "Liver-Forever"},
			                {"field": "lead:custom_substage", "operator": "in", "value": ["Liver-Forever::Enrolled"]}]})},
		               ).insert(set_name="zz-view")

	def tearDown(self):
		frappe.db.rollback()

	def _grain(self, group):
		return frappe.get_doc({"doctype": "CRM Grain", "vertical": "Goodflip-Care", "group": group, "program": PROGRAM}).insert().name

	def _copy(self, source=SOURCE, target=None):
		text = grain_copy.build(source, target or self.target, SUFFIX)
		with patch.object(frappe.db, "commit"):
			return bundle.apply(bundle.read(text))

	def _applied(self, *doctypes, **kwargs):
		with _members(*doctypes):
			results = self._copy(**kwargs)
		self.assertFalse([r for r in results if r["action"] == bundle.REFUSED], results)
		return results

	def test_every_copied_record_lands_on_the_target_and_names_nothing_of_the_source(self):
		results = self._applied("CRM Lead API Mapping", "CRM Task Type", "Assignment Rule", "CRM Smart View")
		self.assertTrue(results)
		for r in results:
			with self.subTest(record=r["record"]):
				self.assertNotIn("Liver-Forever", frappe.as_json(frappe.get_doc(r["ref_doctype"], r["record"]).as_dict()))

	def test_stages_and_their_parents_move_to_the_target_program(self):
		self._applied("CRM Lead API Mapping", "CRM Task Type")
		source = frappe.get_all("CRM Lead Stage", filters={"program": "Liver-Forever"}, fields=["stage"])
		copied = {s.stage: s.substage_of for s in frappe.get_all(
			"CRM Lead Stage", filters={"program": PROGRAM}, fields=["stage", "substage_of"])}
		self.assertEqual(set(copied), {s.stage for s in source})
		self.assertTrue(all(not parent or parent.startswith(f"{PROGRAM}::") for parent in copied.values()))

	def test_a_copy_carries_no_partner_contract(self):
		if not frappe.get_all("CRM Lead API Mapping", filters={"program": "Liver-Forever", "is_internal": 0}, limit=1):
			self.skipTest("no partner contract on Liver-Forever on this site")
		self._applied("CRM Lead API Mapping")
		self.assertFalse(frappe.get_all("CRM Lead API Mapping", filters={"program": PROGRAM, "is_internal": 0}))

	def test_a_copied_pool_leaves_its_people_says_so_and_cannot_turn_on_empty(self):
		with _members("Assignment Rule"):
			notes = {c["name"]: c["note"] for c in bundle.read(grain_copy.build(SOURCE, self.target, SUFFIX))["copied"]}
		self._applied("Assignment Rule")
		pool = f"zz pool {PROGRAM}"
		self.assertEqual(frappe.get_doc("Assignment Rule", pool).users, [])
		self.assertIn("People not carried: 1", notes[pool])
		with self.assertRaises(frappe.ValidationError):
			parts.turn_on("Assignment Rule", pool)

	def test_a_copied_workflow_lands_under_its_own_name_without_its_people(self):
		trigger = {"mode": registry.MODE_RECORD, "subject_doctype": "CRM Lead", "event": "Created",
		           "vertical": "Goodflip-Care", "group": "Zydus", "program": "Liver-Forever"}
		give = {"assignee_mode": "Literal", "assign_mode": "Reassign", "assign_to_user": "Administrator"}
		workflows.create_workflow("zz wf")
		workflows.save_draft("zz wf", [{"node_id": "start", "node_type": registry.TRIGGER, "config_json": frappe.as_json(trigger)},
		                               {"node_id": "give", "node_type": "Assign to User", "config_json": frappe.as_json(give)}],
		                     None, "start")
		with patch.dict(MEMBERS, {"CRM Workflow": {"name": "zz wf"}}, clear=True):  # only ours: this site's own may need webhooks it lacks
			results = self._copy()
		self.assertFalse([r for r in results if r["action"] == bundle.REFUSED], results)
		self.assertIn(("zz wf (ZZ)", bundle.CREATED), {(r["record"], r["action"]) for r in results})
		copied = {n["node_id"]: registry.config_of(n) for n in workflows._nodes_of("zz wf (ZZ)")}
		self.assertEqual((copied["start"]["program"], copied["give"].get("assign_to_user")), (PROGRAM, None))
		self.assertEqual(registry.config_of(next(n for n in workflows._nodes_of("zz wf") if n["node_id"] == "give"))
		                 ["assign_to_user"], "Administrator")

	def test_a_name_holding_nothing_of_its_grain_takes_the_suffix_then_the_grains_code(self):
		frappe.get_doc({"doctype": "Assignment Rule", "document_type": "CRM Lead", "description": "zz", "rule": "Round Robin",
		                "assignment_days": [{"day": "Monday"}], "grain_vertical": "Goodflip-Care", "grain_group": "Zydus",
		                "grain_program": "Liver-Forever"}).insert(set_name="zz plain")
		self._applied("Assignment Rule")
		self.assertTrue(frappe.db.exists("Assignment Rule", f"zz plain ({SUFFIX})"))
		twin = self._grain("Anaya")  # same product line and program, so the same suffix: the grain's code tells them apart
		self._applied("Assignment Rule", target=twin)
		self.assertEqual(frappe.db.get_value("Assignment Rule", f"zz plain ({SUFFIX}-{sha256_hash(twin)[:3]})", "grain_group"), "Anaya")
		again = self._applied("Assignment Rule", target=twin)
		self.assertNotIn(bundle.CREATED, {r["action"] for r in again})

	def test_each_copied_record_says_what_it_was_and_how_it_arrives(self):
		with _members("CRM Task Type", "Assignment Rule"):
			copied = {c["from"]: c["name"] for c in bundle.read(grain_copy.build(SOURCE, self.target, SUFFIX))["copied"]}
			arrives = {r["record"]: r["arrives_as"] for r in self._applied("CRM Task Type", "Assignment Rule")}
		self.assertEqual((copied["zz pool Liver-Forever"], arrives[f"zz pool {PROGRAM}"]), (f"zz pool {PROGRAM}", parts.SWITCHED_OFF))
		self.assertEqual(arrives[copied["Liver-Forever::In Program"]], "Ready")

	def test_a_copied_pool_arrives_disabled_on_the_target(self):
		self._applied("Assignment Rule")
		self.assertEqual(frappe.db.get_value("Assignment Rule", f"zz pool {PROGRAM}", ["grain_program", "disabled"]),
		                 (PROGRAM, 1))

	def test_a_copied_view_reaches_the_target_and_filters_on_it(self):
		self._applied("CRM Lead API Mapping", "CRM Smart View")
		view = frappe.get_doc("CRM Smart View", f"zz-view-{frappe.scrub(SUFFIX)}")
		self.assertEqual(view.program, PROGRAM)
		self.assertIn(f'"{PROGRAM}"', view.predicate)
		self.assertIn(f"{PROGRAM}::Enrolled", view.predicate)

	def test_a_copied_workflow_names_the_copied_forms_and_the_target_grain(self):
		with _members("CRM Task Type", "Assignment Rule", "CRM Workflow"):
			found = bundle.read(grain_copy.build(SOURCE, self.target, SUFFIX))
		workflows = [r for r in found["records"] if r["doctype"] == "CRM Workflow"]
		if not workflows:
			self.skipTest(f"{SOURCE} has no workflow on this site")
		for workflow in workflows:
			trigger = next(registry.config_of(n) for n in workflow["nodes"] if n["node_type"] == "Trigger")
			self.assertEqual(trigger.get("program"), PROGRAM)
			self.assertNotIn("Liver-Forever", frappe.as_json(workflow))

	def test_a_copied_intake_form_arrives_off_on_the_target(self):
		if not frappe.get_all("CRM Intake Form", filters={"custom_current_program": "Nivolumab"}, limit=1):
			self.skipTest("no intake form on Nivolumab on this site")
		target = self._grain("Anaya")
		results = self._applied("CRM Intake Form", source=INTAKE_SOURCE, target=target)
		forms = [r["record"] for r in results if r["ref_doctype"] == "CRM Intake Form"]
		self.assertTrue(forms)
		for form in forms:
			self.assertEqual(frappe.db.get_value("CRM Intake Form", form, ["custom_current_program", "enabled"]), (PROGRAM, 0))

	def test_a_second_copy_overwrites_nothing(self):
		doctypes = ("CRM Lead API Mapping", "CRM Task Type", "Assignment Rule", "CRM Smart View")
		self._applied(*doctypes)
		results = self._applied(*doctypes)
		self.assertEqual({r["action"] for r in results} - {bundle.UNCHANGED, bundle.KEPT}, set())
