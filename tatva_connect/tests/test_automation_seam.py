# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Tests for the Automation Control Plane seam (tatva_connect/automation/).

Covers the read accessor, idempotent catalog seed, operator-owned `enabled`
(never rewritten by sync), the migrate-time drift check, and that the registry
exactly mirrors hooks.py + the cutover enable SQL.
"""
import os
import unittest

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect import hooks
from tatva_connect.automation import drift, seed, settings
from tatva_connect.automation.registry import AUTOMATIONS

_BY_KEY = {a.key: a for a in AUTOMATIONS}

# The cutover enable SQL ships in the app's db-seeds/ (operator runs it once).
_ENABLE_SQL = os.path.join(
	os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
	"db-seeds",
	"2026-06-17-automation-enable.sql",
)


class TestAutomationSeam(FrappeTestCase):
	def setUp(self):
		# Rows must exist for the accessor / lock tests; sync is idempotent.
		seed.sync_catalog()

	# --- read accessor ---------------------------------------------------

	def test_settings_accessor(self):
		"""Flipping a row's `enabled` is reflected by is_enabled() and the
		module wrapper that delegates to it (whatsapp.api.is_enabled)."""
		from tatva_connect.whatsapp import api as wati

		frappe.db.set_value("CRM Tatva Automation", "WhatsApp::WATI::messaging", "enabled", 0)
		self.assertFalse(settings.is_enabled("WhatsApp::WATI::messaging"))
		self.assertFalse(wati.is_enabled())

		frappe.db.set_value("CRM Tatva Automation", "WhatsApp::WATI::messaging", "enabled", 1)
		self.assertTrue(settings.is_enabled("WhatsApp::WATI::messaging"))
		self.assertTrue(wati.is_enabled())

		# Unknown key is fail-closed dormant.
		self.assertFalse(settings.is_enabled("does_not_exist"))

	# --- idempotent seed -------------------------------------------------

	def test_seed_idempotent(self):
		"""A second sync_catalog() preserves an operator-set switch value but
		refreshes the descriptive/structural fields from the registry."""
		# Operator turns a switch ON; we scribble a stale label onto it.
		frappe.db.set_value(
			"CRM Tatva Automation",
			"Storage::Azure::offload",
			{"enabled": 1, "trigger_detail": "STALE"},
		)

		seed.sync_catalog()

		doc = frappe.get_doc("CRM Tatva Automation", "Storage::Azure::offload")
		# operator-set enabled survives ...
		self.assertEqual(doc.enabled, 1)
		# ... structural field is refreshed from the registry.
		self.assertEqual(doc.trigger_detail, _BY_KEY["Storage::Azure::offload"].trigger_detail)

	# --- enabled is operator-owned ---------------------------------------

	def test_enabled_is_operator_owned(self):
		"""sync_catalog seeds a missing row dormant, then NEVER rewrites `enabled`
		again — the operator's choice survives every migrate, both directions and
		uniformly for every row (no locked/always-on class). Asserted on a former
		always-on key, which under the old model would have been forced back to 1."""
		key = "Lead::CRM Lead::dedup"

		frappe.db.set_value("CRM Tatva Automation", key, "enabled", 1)
		seed.sync_catalog()
		self.assertEqual(frappe.db.get_value("CRM Tatva Automation", key, "enabled"), 1)

		frappe.db.set_value("CRM Tatva Automation", key, "enabled", 0)
		seed.sync_catalog()
		self.assertEqual(frappe.db.get_value("CRM Tatva Automation", key, "enabled"), 0)

	# --- drift check -----------------------------------------------------

	def test_drift_throws(self):
		"""A doc_event dotted-path not covered by any registry `backs` makes
		assert_registered raise."""
		fake = "tatva_connect.does_not_exist.handler"
		patched = dict(hooks.doc_events)
		patched["CRM Lead"] = dict(patched.get("CRM Lead", {}))
		patched["CRM Lead"]["on_update"] = [fake]

		orig = hooks.doc_events
		hooks.doc_events = patched
		try:
			with self.assertRaises(frappe.exceptions.ValidationError):
				drift.assert_registered()
		finally:
			hooks.doc_events = orig

	def test_drift_ignores_overrides(self):
		"""assert_registered walks ONLY doc_events + scheduler_events. The
		override_whitelisted_methods / permission_query_conditions targets in
		hooks.py are not registry-backed, yet must never trip the drift check."""
		# Sanity: those hooks really do point at paths absent from the registry.
		registered = {p for a in AUTOMATIONS for p in a.backs}
		override_targets = set(getattr(hooks, "override_whitelisted_methods", {}).values())
		perm_targets = set(getattr(hooks, "permission_query_conditions", {}).values())
		self.assertTrue(override_targets, "expected override_whitelisted_methods entries")
		self.assertFalse(
			override_targets & registered,
			"override targets must not be in the registry (test premise)",
		)
		self.assertFalse(perm_targets & registered)

		# Real hooks (which include those overrides) pass the drift check.
		drift.assert_registered()

	# --- coverage --------------------------------------------------------

	def test_coverage_exactly_once(self):
		"""Every dotted path wired in hooks.doc_events + scheduler_events maps to
		EXACTLY one registry entry's `backs`."""
		all_backs = [p for a in AUTOMATIONS for p in a.backs]
		for path in drift._hooked_paths():
			owners = [a.key for a in AUTOMATIONS if path in a.backs]
			self.assertEqual(
				len(owners),
				1,
				f"'{path}' must be backed by exactly one registry row, got {owners}",
			)
			self.assertEqual(
				all_backs.count(path),
				1,
				f"'{path}' appears in more than one row's backs: duplicated coverage",
			)

	def test_cutover_completeness(self):
		"""Everything ships dormant now; the operator enables what they want at
		cutover. This guards the cutover enable SQL against a forgotten integration
		switch — each must be named in the active UPDATE or the documented PROD note."""
		# Integration switches the operator turns on at cutover (cutover runbook).
		cutover = {
			"Task::Automation::rules",
			"Storage::File::draft-cleanup",
			"WhatsApp::WATI::templates",
			"Storage::Azure::offload",
			"Location::Google::capture",
			"Task::Assignment::followup",
		}

		if not os.path.exists(_ENABLE_SQL):
			self.skipTest("cutover enable SQL absent (db-seeds/ is gitignored, operator-run)")

		with open(_ENABLE_SQL, encoding="utf-8") as fh:
			sql_text = fh.read()

		for key in sorted(cutover):
			self.assertIn(key, _BY_KEY, f"{key} not in registry")
			# The key is named somewhere in the enable file — either the active
			# UPDATE (dev live state) or the PROD-operator note (e.g. template_sync,
			# documented but not enabled on dev because WATI is off here).
			self.assertIn(
				key,
				sql_text,
				f"cutover switch '{key}' is missing from the enable SQL "
				f"({os.path.basename(_ENABLE_SQL)})",
			)


if __name__ == "__main__":
	unittest.main()
