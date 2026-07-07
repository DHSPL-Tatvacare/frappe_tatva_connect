# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Leg H sign-off - the wildcard-router drift gate (one more assertion in assert_registered).

TATVA v2 (Task 4): the engine has no per-doctype hook any more - EVERY grain-resolvable subject is
covered by the wildcard router (router.on_created/on_updated on doc_events["*"]). The old "every
SUBJECT doctype must be hooked" check is retired with it (there is no longer a per-subject hook to
check) - the one thing that CAN silently break automation for every doctype at once is the wildcard
registration itself, so that's what this gate now asserts.

Also re-runs the existing seam suite's regression assertions to prove the new gate didn't break
the existing path-coverage / one-owner / enforced-mirror checks.
"""
import unittest

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect import hooks
from tatva_connect.automation import drift, seed
from tatva_connect.automation.registry import AUTOMATIONS


class TestRouterDrift(FrappeTestCase):
	"""The wildcard-router drift gate: router.on_created/on_updated MUST be wired on doc_events["*"]."""

	def setUp(self):
		seed.sync_catalog()

	# (a) the default wildcard registration (router.on_created/on_updated, both hooked) passes.
	def test_wired_router_passes(self):
		drift.assert_registered()  # no throw

	# (b) the wildcard router's on_updated hook missing -> assert_registered throws (simulates the one
	# hook that can silently break automation for EVERY doctype at once being dropped). Restore after.
	def test_missing_wildcard_router_hook_throws(self):
		patched = dict(hooks.doc_events)
		patched["*"] = dict(patched.get("*", {}))
		patched["*"]["on_update"] = []
		orig = hooks.doc_events
		hooks.doc_events = patched
		try:
			with self.assertRaises(frappe.exceptions.ValidationError):
				drift.assert_registered()
		finally:
			hooks.doc_events = orig


class TestSeamRegressions(FrappeTestCase):
	"""Re-run the existing seam suite's invariants under the new gate - prove no regression."""

	def setUp(self):
		seed.sync_catalog()

	# (c) REGRESSION: every hooked path is still backed by EXACTLY one registry row (the new
	# fire_field_change_rules path is backed by Task::Automation::rules - one owner, not two).
	def test_coverage_exactly_once(self):
		all_backs = [p for a in AUTOMATIONS for p in a.backs]
		for path in drift._hooked_paths():
			owners = [a.key for a in AUTOMATIONS if path in a.backs]
			self.assertEqual(len(owners), 1, f"'{path}' must be backed by exactly one row, got {owners}")
			self.assertEqual(all_backs.count(path), 1, f"'{path}' duplicated across rows' backs")

	# (d) REGRESSION: the Task::Automation::rules key is still enforced in app source (router.py
	# references it via is_enabled - the new entry point made the toggle drive real new code).
	def test_task_automation_rules_is_enforced(self):
		app_dir = frappe.get_app_path("tatva_connect")
		registry_file = "automation/registry.py"
		chunks = []
		for root, _dirs, files in __import__("os").walk(app_dir):
			if "/tests" in root or "__pycache__" in root:
				continue
			for fn in files:
				if not fn.endswith(".py"):
					continue
				full = root + "/" + fn
				if full.endswith(registry_file):
					continue
				with open(full, encoding="utf-8") as fh:
					chunks.append(fh.read())
		source = "\n".join(chunks)
		self.assertIn("Task::Automation::rules", source, "the kill switch literal must appear in app source")

	# (e) REGRESSION: the existing path-drift check still throws on an unbacked path, and still
	# ignores overrides.
	def test_path_drift_still_throws_on_unbacked(self):
		fake = "tatva_connect.does_not_exist.handler"
		patched = dict(hooks.doc_events)
		patched["CRM Lead"] = dict(patched.get("CRM Lead", {}))
		patched["CRM Lead"]["on_update"] = patched["CRM Lead"].get("on_update", []) + [fake]
		orig = hooks.doc_events
		hooks.doc_events = patched
		try:
			with self.assertRaises(frappe.exceptions.ValidationError):
				drift.assert_registered()
		finally:
			hooks.doc_events = orig


if __name__ == "__main__":
	unittest.main()
