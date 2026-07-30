# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The row-visibility brain must actually COVER every doctype it claims to.

The registry listed the three workflow tables, and the app's docstrings, hooks and `permissions.py`
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

	def test_every_scoped_doctype_declares_at_least_one_strategy(self):
		"""A doctype registered with no way to reach it is not "locked down" — `_compose` answers `1=0`
		and the list is empty for everyone but an operator, which reads as data loss, not as a gate."""
		empty = sorted(dt for dt, scope in visibility.SCOPED.items() if not scope.by)
		self.assertEqual(empty, [], f"registered with no strategy at all: {empty}")

	def test_every_hooked_doctype_is_declared_and_every_declared_one_is_hooked(self):
		"""THE drift lock, both directions. A doctype wired in `hooks.py` but absent from `SCOPED` is
		unscoped while looking scoped — the exact way three of these tables sat open for months. One
		declared but never hooked is a rule nothing consults.

		Read out of `hooks.py` itself, so adding a hook without a declaration cannot pass.
		"""
		from tatva_connect import hooks

		hooked = set(hooks.permission_query_conditions) & set(hooks.has_permission)
		# Leads/Deals are scoped by crm's own hierarchy, and the picklist clamp is a grain filter on a
		# master, not row visibility. Neither belongs to this registry.
		outside = {"CRM Picklist Value"}
		missing = sorted((hooked - outside) - set(visibility.SCOPED))
		self.assertEqual(missing, [], f"hooked in hooks.py but never declared: {missing}")
		unhooked = sorted(set(visibility.SCOPED) - hooked)
		self.assertEqual(unhooked, [], f"declared but no hook consults it: {unhooked}")

	def test_every_switch_is_a_declared_automation(self):
		"""A switch key the registry does not declare gets no row from the seed, and `is_enabled`
		answers False for ever — the scoping is decoration, not enforcement."""
		declared = {auto.key for auto in AUTOMATIONS}
		switches = {s.switch for s in visibility.SCOPED.values() if s.switch}
		undeclared = sorted(switches - declared)
		self.assertEqual(undeclared, [], f"switch declared nowhere in AUTOMATIONS: {undeclared}")

	def test_a_strategy_names_the_columns_its_own_predicate_reads(self):
		"""`fields()` is what every row-fetching caller asks for. A strategy that reads a column it does
		not declare gets `None` from `row.get` and silently denies — a gate that fails closed for the
		wrong reason is indistinguishable from one that works."""
		for doctype, scope in visibility.SCOPED.items():
			with self.subTest(doctype=doctype):
				for strategy in scope.by:
					for column in strategy.fields:
						self.assertIn(column, scope.fields())

	# ------------------------------------------------------------------ the SQL really runs

	def test_every_scoped_doctype_produces_runnable_sql(self):
		"""ONE entry point for every doctype, whatever its strategies — that there is only one is the
		point. Each clause is then really executed: defect 3 in this file's header was a clause naming a
		column that does not exist, which no amount of reading catches."""
		for doctype in visibility.SCOPED:
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
					doctype=doctype,
					owner="someone-else@example.test",
					subject_doctype="CRM Lead",
					subject_name="CRM-LEAD-DOES-NOT-EXIST",
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
