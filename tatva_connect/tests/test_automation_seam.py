# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Tests for the Automation Control Plane seam (tatva_connect/automation/).

Covers the read accessor, idempotent catalog seed, guard/infra lock, the
migrate-time drift check, and that the registry exactly mirrors hooks.py +
the cutover enable SQL. See docs/plans/2026-06-17-automation-control-plane.md.
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
		refreshes a structural field (control)."""
		# Operator turns a switch ON and we scribble a stale control onto it.
		frappe.db.set_value(
			"CRM Tatva Automation",
			"Storage::Azure::offload",
			{"enabled": 1, "control": "STALE CONTROL"},
		)

		seed.sync_catalog()

		doc = frappe.get_doc("CRM Tatva Automation", "Storage::Azure::offload")
		# operator-set enabled survives ...
		self.assertEqual(doc.enabled, 1)
		# ... structural field is refreshed from the registry.
		self.assertEqual(doc.control, _BY_KEY["Storage::Azure::offload"].control)

	# --- guard / infra lock ----------------------------------------------

	def test_guard_infra_locked(self):
		"""Every Always-on row is enabled=1 after sync; the controller forces
		it back to 1 even if someone sets 0."""
		for auto in AUTOMATIONS:
			if auto.control != "Always-on":
				continue
			self.assertEqual(
				frappe.db.get_value("CRM Tatva Automation", auto.key, "enabled"),
				1,
				f"{auto.key} ({auto.control}) must seed enabled=1",
			)

			doc = frappe.get_doc("CRM Tatva Automation", auto.key)
			doc.enabled = 0
			doc.save(ignore_permissions=True)
			doc.reload()
			self.assertEqual(
				doc.enabled, 1, f"controller must force {auto.key} back to enabled=1"
			)

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
		"""Every switch whose behaviour is unconditional-today must be accounted
		for in the cutover enable SQL (the active UPDATE or the documented PROD
		operator note). Guards/infra need no SQL — they always run."""
		# Switches that fire unconditionally today (plan §1d / cutover runbook).
		unconditional = {
			"Task::CRM Task::transitions",
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

		for key in sorted(unconditional):
			auto = _BY_KEY[key]
			self.assertEqual(
				auto.control, "Toggle", f"{key} is expected to be a toggle"
			)
			# The key is named somewhere in the enable file — either the active
			# UPDATE (dev live state) or the PROD-operator note (e.g. template_sync,
			# documented but not enabled on dev because WATI is off here).
			self.assertIn(
				key,
				sql_text,
				f"unconditional-today switch '{key}' is missing from the cutover "
				f"enable SQL ({os.path.basename(_ENABLE_SQL)})",
			)

		# And no always-on row leaked into the enable SQL (they must never be
		# toggled from there).
		for auto in AUTOMATIONS:
			if auto.control == "Always-on":
				self.assertNotIn(
					f"'{auto.key}'",
					sql_text,
					f"always-on '{auto.key}' must not appear in the enable SQL",
				)


if __name__ == "__main__":
	unittest.main()
