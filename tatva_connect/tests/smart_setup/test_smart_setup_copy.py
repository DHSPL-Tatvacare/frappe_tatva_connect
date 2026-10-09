# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A grain copied onto another lands every record on the target, names nothing of the source, and never overwrites."""
import unittest
from unittest.mock import patch

import frappe

from tatva_connect.smart_setup import bundle, grain_copy, recipes
from tatva_connect.workflow_engine import registry

SOURCE = "Goodflip-Care::Zydus::Liver-Forever"
INTAKE_SOURCE = "Goodflip-Care::Anaya::Nivolumab"
PROGRAM = "zz-copy-target"
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
		text = grain_copy.build(source, target or self.target)
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

	def test_a_copied_partner_contract_arrives_off_and_unbound(self):
		self._applied("CRM Lead API Mapping")
		for contract in frappe.get_all("CRM Lead API Mapping", filters={"program": PROGRAM, "is_internal": 0},
		                               fields=["enabled", "partner_user"]):
			self.assertEqual((contract.enabled, contract.partner_user), (0, None))

	def test_each_copied_record_says_what_it_was_and_how_it_arrives(self):
		with _members("CRM Task Type", "Assignment Rule"):
			copied = {c["from"]: c for c in bundle.read(grain_copy.build(SOURCE, self.target))["copied"]}
		self.assertEqual((copied["zz pool Liver-Forever"]["name"], copied["zz pool Liver-Forever"]["arrives_as"]),
		                 (f"zz pool {PROGRAM}", "Disabled"))
		self.assertEqual(copied["Liver-Forever::In Program"]["arrives_as"], "Ready")

	def test_a_copied_pool_arrives_disabled_on_the_target(self):
		self._applied("Assignment Rule")
		self.assertEqual(frappe.db.get_value("Assignment Rule", f"zz pool {PROGRAM}", ["grain_program", "disabled"]),
		                 (PROGRAM, 1))

	def test_a_copied_view_reaches_the_target_and_filters_on_it(self):
		self._applied("CRM Lead API Mapping", "CRM Smart View")
		view = frappe.get_doc("CRM Smart View", f"zz-view-{PROGRAM}")
		self.assertEqual(view.program, PROGRAM)
		self.assertIn(f'"{PROGRAM}"', view.predicate)
		self.assertIn(f"{PROGRAM}::Enrolled", view.predicate)

	def test_a_copied_workflow_names_the_copied_forms_and_the_target_grain(self):
		with _members("CRM Task Type", "Assignment Rule", "CRM Workflow"):
			found = bundle.read(grain_copy.build(SOURCE, self.target))
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
