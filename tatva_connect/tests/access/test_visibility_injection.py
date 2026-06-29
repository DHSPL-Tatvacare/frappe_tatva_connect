# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Injection + fail-closed coverage for the row-visibility brain (access/visibility.py),
complementing the Smart View composer DAST (test_sql_injection.py).

Proves three things the composer suite does NOT exercise:
  1. NO IDENTIFIER INJECTION — the user identity embedded in the permission_query_conditions
     is ESCAPED (frappe.db.escape), never concatenated raw, so a username carrying SQL
     metacharacters cannot break out of the clause.
  2. PQC ALWAYS APPLIED — a privileged user is unscoped ("") while a normal user is always
     scoped; injection therefore cannot widen a normal user's row set to a privileged one.
  3. ORPHANED-PARENT DENY — a child doc on a deleted/unknown parent is DENIED for a
     non-owner (the most important fail-closed branch, previously untested).

Run:
    bench --site dev.localhost run-tests --app tatva_connect \
        --module tatva_connect.tests.test_visibility_injection
"""
import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.access import visibility

_DOCTYPE = "CRM Task"
_SWITCH = "Task::CRM Task::visibility"      # the visibility kill-switch for CRM Task
INJECT = "x' OR '1'='1"                      # a SQLi-style identity; must be escaped, not executed


class TestVisibilityInjection(FrappeTestCase):
	def setUp(self):
		frappe.db.set_value("CRM Tatva Automation", _SWITCH, "enabled", 1)

	def test_user_value_is_escaped_not_injected(self):
		pqc = visibility.scoped_pqc(_DOCTYPE, INJECT)
		self.assertTrue(pqc, "a non-privileged user with the switch ON must get a scoping clause")
		self.assertIn(frappe.db.escape(INJECT), pqc, "user identity must be embedded ESCAPED")
		self.assertNotIn(f"={INJECT}", pqc, "raw unescaped injection value must never appear in SQL")

	def test_privileged_user_is_unscoped(self):
		# Administrator/System Manager see all by design — the point is a normal user CANNOT
		# reach this empty (unscoped) state via injection.
		self.assertEqual(visibility.scoped_pqc(_DOCTYPE, "Administrator"), "")

	def test_switch_off_is_dormant(self):
		frappe.db.set_value("CRM Tatva Automation", _SWITCH, "enabled", 0)
		self.assertEqual(visibility.scoped_pqc(_DOCTYPE, INJECT), "")

	def test_orphaned_parent_denied_for_non_owner(self):
		doc = frappe._dict(
			doctype=_DOCTYPE,
			owner="someone-else@example.com",
			assigned_to=None,
			reference_doctype="CRM Lead",
			reference_docname="NONEXISTENT-LEAD-ZZZ",
		)
		self.assertFalse(visibility.scoped_has_permission(doc, "read", "nobody@example.com"))

	def test_owner_allowed_on_orphaned_parent(self):
		doc = frappe._dict(
			doctype=_DOCTYPE,
			owner="owner@example.com",
			assigned_to=None,
			reference_doctype="CRM Lead",
			reference_docname="NONEXISTENT-LEAD-ZZZ",
		)
		self.assertTrue(visibility.scoped_has_permission(doc, "read", "owner@example.com"))
