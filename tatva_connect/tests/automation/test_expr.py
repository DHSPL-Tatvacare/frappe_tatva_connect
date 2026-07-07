# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Leg D sign-off - automation/expr.py: the shared expression resolver + the save-time syntax check.

Two security plants (S.6 recall): `__import__` and `open` must raise - safe_eval strips builtins,
so the trust boundary does not widen. No mocked verdicts - real frappe.safe_eval is the oracle.
"""
import datetime
import unittest

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.automation import expr


class TestExpr(FrappeTestCase):
	# (a) date math - the canonical realistic use case (Scenario 1 in the plan).
	def test_add_days_off_context_field(self):
		out = expr.resolve_expression("add_days(ctx['d'], 3)", {"d": "2026-07-06"})
		self.assertEqual(frappe.utils.getdate(out), datetime.date(2026, 7, 9))

	# (b) a bare context read returns the value as-is (no coercion).
	def test_context_passthrough(self):
		self.assertEqual(expr.resolve_expression("ctx['x']", {"x": 5}), 5)

	# (c) SECURITY plant - builtin `__import__` must NOT be reachable. safe_eval strips builtins.
	def test_import_is_blocked(self):
		with self.assertRaises(Exception):
			expr.resolve_expression("__import__('os')", {})

	# (d) SECURITY plant - `open` must NOT be reachable.
	def test_open_is_blocked(self):
		with self.assertRaises(Exception):
			expr.resolve_expression("open('/etc/passwd')", {})

	# (e) assert_parses - syntax-only gate used at rule save time. A real eval expr passes;
	# a statement (def/for/...) is not an eval-mode expression and must raise.
	def test_assert_parses_eval_only(self):
		expr.assert_parses("add_days(ctx['d'], 3)")  # passes - no raise
		expr.assert_parses("ctx['x'] + 1")  # passes
		with self.assertRaises(SyntaxError):
			expr.assert_parses("def f(): pass")  # a statement, not an eval expression
		with self.assertRaises(SyntaxError):
			expr.assert_parses("for x in y: pass")


if __name__ == "__main__":
	unittest.main()
