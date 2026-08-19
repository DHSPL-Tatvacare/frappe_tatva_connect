# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""`context.for_node` and the canvas's `contextFor` are the SAME slice, written twice.

The canvas holds one answer per graph and cuts each node's share of it client-side, so `nodeContext.js`
mirrors `for_node` key for key — its own header says so. Nothing held the two together, and on 2026-08-17
that cost a released defect: `targets` and `writes_to` were added to the Python half only, so the Target
dropdown rendered EMPTY and the Field Map offered every reachable record instead of the node's own. Every
backend test passed; only a browser could see it.

Reading the JS by regex rather than executing it, for the reason `test_step_log_truthfulness` reads the
colour maps that way: the assertion is about what the FILE says, and a bundler in the loop would only add
a way for the lock to go quiet.
"""
import pathlib
import re

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.workflow_engine import context as wf_context

_JS = (
	pathlib.Path(frappe.get_app_path("crm")).parent
	/ "frontend" / "src" / "tatva" / "workflows" / "nodeContext.js"
)


def _python_keys():
	"""What `for_node` really returns, asked of the function rather than read off its source."""
	answer = {
		"subject": "CRM Lead", "grain": {}, "working_set": [], "subject_fields": [],
		"settable": [], "targets": [], "operators_by_type": {}, "operator_shapes": {},
		"nodes": {"n1": {"emitted": [], "emitters": [], "writes_to": ""}},
	}
	return set(wf_context.for_node(answer, "n1"))


def _js_keys():
	source = _JS.read_text()
	body = re.search(r"return\s*\{(.*?)\n  \}", source, re.S)
	if not body:
		return None
	return set(re.findall(r"^\s{4}([A-Za-z_][A-Za-z0-9_]*)\s*:", body.group(1), re.M))


class TestNodeContextTwins(FrappeTestCase):
	def setUp(self):
		if not _JS.exists():
			self.skipTest("the crm frontend is not checked out beside this app")

	def test_the_two_halves_carry_the_same_keys(self):
		"""A key on one side and not the other is a control reading `undefined` — silently, and only in a
		browser. Whichever side gained it, this is where it is noticed."""
		js, py = _js_keys(), _python_keys()
		self.assertIsNotNone(js, "contextFor's return object did not parse — the lock is matching a shape that moved")
		self.assertEqual(
			py - js, set(), f"nodeContext.js is missing {sorted(py - js)} — the control reading it renders empty",
		)
		self.assertEqual(
			js - py, set(), f"nodeContext.js invents {sorted(js - py)}, which for_node never sends",
		)

	def test_the_slice_still_answers_for_a_node_the_graph_does_not_hold(self):
		"""Both halves return the same shape for an unknown node, so a box just dropped on the canvas does
		not crash the panel before the reload lands."""
		answer = {
			"subject": "", "grain": {}, "working_set": [], "subject_fields": [], "settable": [],
			"targets": [], "operators_by_type": {}, "operator_shapes": {}, "nodes": {},
		}
		self.assertEqual(set(wf_context.for_node(answer, "nope")), _python_keys())
