# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The row-visibility brain must actually COVER every doctype it claims to.

`_SWITCH_OF` listed the three workflow tables, and the app's docstrings, hooks and `permissions.py`
all read as though they were scoped. None of it worked, in three separate ways, and nothing was red:

  1. the switch keys were never declared in `automation.registry.AUTOMATIONS`, so `seed.sync_catalog`
     never created their rows — and it PRUNES any row whose key left the registry, so the rows could
     not be created by hand either. `is_enabled` therefore answered False for ever and the three
     tables were entirely unscoped.
  2. `PARENT_OF` had no resolver for them, so the single-doc gate raised
     `KeyError('CRM Workflow Journey')` for every non-privileged, non-owner caller.
  3. `CRM Workflow Step Log` carries `subject_name` but NO `subject_doctype`, and `_link_columns`
     probed the name column — so the list clause named a column that does not exist and every query
     died with `(1054, "Unknown column ...")`.

Each of those is fixed. These tests are the lock, and the last two are the ones that matter most:
they are DRIFT locks, so a fourth doctype cannot be half-registered the same way. A declaration
nothing checks is exactly how three of them stayed wrong.

Run:
    bench --site dev.localhost run-tests --module tatva_connect.tests.access.test_workflow_row_visibility
"""
import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.access import visibility
from tatva_connect.automation.registry import AUTOMATIONS

WORKFLOW_DOCTYPES = ("CRM Workflow Journey", "CRM Workflow Signal", "CRM Workflow Step Log")
USER = "vis.probe@example.test"


class TestWorkflowRowVisibility(FrappeTestCase):
	def setUp(self):
		super().setUp()
		self._enabled = visibility.automation.is_enabled
		visibility.automation.is_enabled = lambda key: True

	def tearDown(self):
		visibility.automation.is_enabled = self._enabled
		super().tearDown()

	# ------------------------------------------------------------------ drift locks

	def test_every_switched_doctype_has_a_parent_resolver(self):
		"""A doctype in `_SWITCH_OF` and not in `PARENT_OF` gates lists and then KeyErrors on the
		single-doc read — half-scoped, which is worse than unscoped because it looks finished."""
		missing = sorted(set(visibility._SWITCH_OF) - set(visibility.PARENT_OF))
		self.assertEqual(missing, [], f"no PARENT_OF resolver for: {missing}")

	def test_every_switch_is_a_declared_automation(self):
		"""A switch key the registry does not declare gets no row from the seed, and `is_enabled`
		answers False for ever — the scoping is decoration, not enforcement."""
		declared = {auto.key for auto in AUTOMATIONS}
		undeclared = sorted(set(visibility._SWITCH_OF.values()) - declared)
		self.assertEqual(undeclared, [], f"switch declared nowhere in AUTOMATIONS: {undeclared}")

	# ------------------------------------------------------------------ the SQL really runs

	def test_every_switched_doctype_produces_runnable_sql(self):
		for doctype in visibility._SWITCH_OF:
			with self.subTest(doctype=doctype):
				clause = visibility.scoped_pqc(doctype, USER)
				self.assertTrue(clause, "a non-privileged user must get a scoping clause")
				frappe.db.sql(f"select name from `tab{doctype}` where {clause} limit 1")

	def test_a_step_log_is_scoped_through_its_run(self):
		"""It has no `subject_doctype` of its own, so the clause must reach its lead via `journey`
		— naming a column the table does not have is what broke it before."""
		clause = visibility.scoped_pqc("CRM Workflow Step Log", USER)
		self.assertIn("`journey`", clause)
		self.assertNotIn("`tabCRM Workflow Step Log`.`subject_doctype`", clause)
		self.assertIn("`tabCRM Workflow Journey`", clause, "it must recurse into the journey's own clause")

	# ------------------------------------------------------------------ the single-doc gate

	def test_the_single_doc_gate_answers_instead_of_raising(self):
		for doctype in WORKFLOW_DOCTYPES:
			with self.subTest(doctype=doctype):
				doc = frappe._dict(
					doctype=doctype, owner="someone-else@example.test",
					subject_doctype="CRM Lead", subject_name="CRM-LEAD-DOES-NOT-EXIST",
					journey="CRM-RUN-DOES-NOT-EXIST",
				)
				self.assertFalse(
					visibility.scoped_has_permission(doc, "read", USER),
					"an unresolvable parent must DENY, fail-closed — never raise",
				)

	def test_parent_readable_is_the_one_rule_both_callers_ask(self):
		self.assertFalse(visibility.parent_readable("CRM Lead", "CRM-LEAD-DOES-NOT-EXIST", USER))
		self.assertFalse(visibility.parent_readable("CRM Lead", None, USER))
		self.assertFalse(visibility.parent_readable(None, "anything", USER))
