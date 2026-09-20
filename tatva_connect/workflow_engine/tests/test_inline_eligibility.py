# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""`must_be_durable` keeps a row-locking verb out of the inline lane, and a lost transaction out of a success response.

A pool draw holds `SELECT ... FOR UPDATE` on the Assignment Rule row until its transaction commits; run inline
that transaction is the caller's whole save, so a conflict discards the save, savepoints and all."""

import inspect
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect import utils
from tatva_connect.automation import actions
from tatva_connect.lead import assignment
from tatva_connect.workflow_engine import interpreter, triggers

_POOL_DRAW = assignment.draw_from_pool.__name__  # read off the function, so a rename cannot leave this lock matching nothing


def _graph(*node_types):
	return frappe._dict(nodes=[frappe._dict(node_type=nt) for nt in node_types])


class TestInlineEligibility(FrappeTestCase):
	def test_every_verb_that_can_draw_from_a_pool_declares_its_own_transaction(self):
		"""Auto-discovered from the handlers, never an allowlist: a new verb that reaches the pool lock without
		declaring it goes red here rather than losing a lead in production."""
		for verb, declared in actions.VERBS.items():
			draws = _POOL_DRAW in inspect.getsource(declared["handler"])
			self.assertEqual(
				draws, actions.needs_own_transaction(verb),
				f"{verb!r} {'draws from a pool but does not declare' if draws else 'declares'} own_transaction",
			)

	def test_a_wait_free_graph_that_distributes_is_still_durable(self):
		self.assertTrue(interpreter.must_be_durable(_graph("Trigger", "Distribute", "Terminal")))

	def test_a_wait_free_graph_that_locks_nothing_stays_inline(self):
		self.assertFalse(interpreter.must_be_durable(_graph("Trigger", "Update Field", "Create Task", "Terminal")))

	def test_a_parking_graph_is_durable_as_it_always_was(self):
		self.assertTrue(interpreter.must_be_durable(_graph("Trigger", "Wait", "Terminal")))

	def test_undo_to_is_silent_when_the_transaction_took_the_savepoint_with_it(self):
		"""The inline lane used to raise 1305 out of its own error handler, burying the failure that caused it."""
		interpreter._undo_to(f"tc_wf_never_{frappe.generate_hash(length=8)}")

	def test_a_lost_transaction_reaches_the_caller_instead_of_a_success_it_cannot_honour(self):
		for error in utils.TRANSACTION_LOST:
			with self.subTest(error=error.__name__), patch.object(triggers.interpreter, "run_inline", side_effect=error):
				with self.assertRaises(error):
					triggers._run_ephemeral("v", "LEAD-1", None, {})

	def test_every_other_failure_is_still_logged_and_never_denies_the_save(self):
		with patch.object(triggers.interpreter, "run_inline", side_effect=ValueError("a broken graph")):
			with patch.object(frappe, "log_error") as logged:
				triggers._run_ephemeral("v", "LEAD-1", None, {})
		self.assertTrue(logged.called)
