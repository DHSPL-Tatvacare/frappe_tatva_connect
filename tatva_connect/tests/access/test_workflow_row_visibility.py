# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Every doctype the row-visibility registry claims is hooked, switchable and produces SQL that runs."""

import frappe
from frappe.tests import IntegrationTestCase

from tatva_connect.access import visibility
from tatva_connect.automation.registry import AUTOMATIONS
from tatva_connect.tests.workflow_engine.fixtures import set_switch

WORKFLOW_DOCTYPES = ("CRM Workflow Journey", "CRM Workflow Signal", "CRM Workflow Step Log", "CRM Workflow Version")
USER = "vis.probe@example.test"


class TestWorkflowRowVisibility(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		# Registered before super(): it runs after the class rollback, which fires no hook to drop the cached switch rows.
		cls.addClassCleanup(frappe.clear_document_cache, "CRM Tatva Automation")
		super().setUpClass()
		frappe.get_doc({"doctype": "User", "email": USER, "first_name": "Vis Probe", "send_welcome_email": 0,
		                "roles": [{"role": "Sales User"}]}).insert(ignore_permissions=True)
		for key in {s.switch for s in visibility.SCOPED.values() if s.switch}:
			set_switch(key, 1)  # armed through their own rows, as an operator arms them

	# ------------------------------------------------------------------ drift locks

	def test_every_scoped_doctype_declares_at_least_one_strategy(self):
		"""A doctype registered with no strategy shows an empty list to everyone but an operator."""
		empty = sorted(dt for dt, scope in visibility.SCOPED.items() if not scope.by)
		self.assertEqual(empty, [], f"registered with no strategy at all: {empty}")

	def test_every_hooked_doctype_is_declared_and_every_declared_one_is_hooked(self):
		"""Every doctype hooked in `hooks.py` is declared in `SCOPED`, and every declared one is hooked."""
		from tatva_connect import hooks

		hooked = set(hooks.permission_query_conditions) & set(hooks.has_permission)
		# The picklist clamp is a grain filter and `lms_permissions` scopes LMS, so neither is registered.
		outside = {"CRM Picklist Value", "LMS Batch", "LMS Program", "LMS Quiz"}
		for dt in ("LMS Batch", "LMS Program", "LMS Quiz"):
			self.assertIn(dt, hooked, f"{dt} is exempt from the registry because lms_permissions scopes it — and it no longer does")
		missing = sorted((hooked - outside) - set(visibility.SCOPED))
		self.assertEqual(missing, [], f"hooked in hooks.py but never declared: {missing}")
		unhooked = sorted(set(visibility.SCOPED) - hooked)
		self.assertEqual(unhooked, [], f"declared but no hook consults it: {unhooked}")

	def test_every_switch_is_a_declared_automation(self):
		"""Every switch key is a declared automation, or the seed never creates its row and it stays off forever."""
		declared = {auto.key for auto in AUTOMATIONS}
		switches = {s.switch for s in visibility.SCOPED.values() if s.switch}
		undeclared = sorted(switches - declared)
		self.assertEqual(undeclared, [], f"switch declared nowhere in AUTOMATIONS: {undeclared}")

	def test_a_strategy_names_the_columns_its_own_predicate_reads(self):
		"""A strategy declares every column it reads, or `row.get` returns None and it denies for the wrong reason."""
		for doctype, scope in visibility.SCOPED.items():
			with self.subTest(doctype=doctype):
				for strategy in scope.by:
					for column in strategy.fields:
						self.assertIn(column, scope.fields())

	# ------------------------------------------------------------------ the SQL really runs

	def test_every_scoped_doctype_produces_runnable_sql(self):
		"""Each doctype's clause is really executed, so a clause naming a missing column fails here."""
		for doctype in visibility.SCOPED:
			with self.subTest(doctype=doctype):
				clause = visibility.scoped_pqc(doctype, USER)
				self.assertTrue(clause, "a non-privileged user must get a scoping clause")
				frappe.db.sql(f"select name from `tab{doctype}` where {clause} limit 1")

	def test_a_step_log_is_scoped_through_its_run(self):
		"""A step log has no `subject_doctype`, so its clause reaches the lead through `journey`."""
		clause = visibility.scoped_pqc("CRM Workflow Step Log", USER)
		self.assertIn("`journey`", clause)
		self.assertNotIn("`tabCRM Workflow Step Log`.`subject_doctype`", clause)
		self.assertIn("`tabCRM Workflow Journey`", clause, "it must recurse into the journey's own clause")

	# ------------------------------------------------------------------ the single-doc gate

	def test_the_single_doc_gate_answers_instead_of_raising(self):
		for doctype in WORKFLOW_DOCTYPES:
			with self.subTest(doctype=doctype):
				doc = frappe._dict(
					doctype=doctype,
					owner="someone-else@example.test",
					subject_doctype="CRM Lead",
					subject_name="CRM-LEAD-DOES-NOT-EXIST",
					journey="CRM-RUN-DOES-NOT-EXIST",
					workflow="CRM-WORKFLOW-DOES-NOT-EXIST",
				)
				self.assertFalse(
					visibility.scoped_has_permission(doc, "read", USER),
					"an unresolvable parent must DENY, fail-closed — never raise",
				)

	def test_parent_readable_is_the_one_rule_both_callers_ask(self):
		self.assertFalse(visibility.parent_readable("CRM Lead", "CRM-LEAD-DOES-NOT-EXIST", USER))
		self.assertFalse(visibility.parent_readable("CRM Lead", None, USER))
		self.assertFalse(visibility.parent_readable(None, "anything", USER))
