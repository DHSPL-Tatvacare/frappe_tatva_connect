# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Leg I sign-off (programmatic) - the describe endpoint returns watch_fields for a Field-Changed
rule, alongside the existing activity_fields / set_field_targets. The manual Playwright step
(watch_field dropdown populates on trigger switch) is gated on a running dev bench.
"""
import unittest

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.automation import describe
from tatva_connect.tests.authz.grains import GRAINS
from tatva_connect.tests.automation import field_allowlist

_LEAD_FIELD = "custom_stage"
_GRAIN = GRAINS[0]


class TestDescribeWatchFields(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		# Run as Administrator so the read-permission gate passes.
		frappe.set_user("Administrator")
		field_allowlist.seed_watchable("CRM Lead", _LEAD_FIELD)

	@classmethod
	def tearDownClass(cls):
		field_allowlist.clear("CRM Lead")
		frappe.set_user("Administrator")

	# (a) describe(watch_doctype="CRM Lead") returns watch_fields with the real CRM Lead field descriptor.
	def test_describe_returns_watch_fields(self):
		out = describe.describe(watch_doctype="CRM Lead")
		self.assertIn("watch_fields", out)
		keys = [f["key"] for f in out["watch_fields"]]
		self.assertIn(_LEAD_FIELD, keys)
		# The descriptor carries a real type + operators so the builder + validator share one vocabulary.
		# Don't hardcode the type - read it from the live meta so the test doesn't drift with fixtures.
		desc = next(f for f in out["watch_fields"] if f["key"] == _LEAD_FIELD)
		live_type = frappe.get_meta("CRM Lead").get_field(_LEAD_FIELD).fieldtype
		self.assertEqual(desc["type"], live_type)
		self.assertTrue(desc["operators"])

	# (b) describe with no watch_doctype returns an empty watch_fields list (no false offerings).
	def test_describe_no_watch_doctype_is_empty(self):
		out = describe.describe()
		self.assertEqual(out["watch_fields"], [])


if __name__ == "__main__":
	unittest.main()
