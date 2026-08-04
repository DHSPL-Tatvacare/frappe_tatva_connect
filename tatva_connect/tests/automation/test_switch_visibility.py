# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Gate: the switch read names the ONE state an operator cannot otherwise see.

Every automation ships dormant, and a dormant automation does nothing and logs nothing — so a switch
ticked on above a dormant parent is invisible from every angle: the desk row reads armed, `is_enabled`
answers off, and there is no error, no log line and no missing record to notice. This is the read that
says it out loud, and these are the assertions that keep it honest.

The pairs are PARAMETERISED OVER `AUTOMATIONS`. A hand-typed list of parents and children goes stale the
day a link is added, so the registry is the only source, and the non-vacuity assertion at the top of each
sweep makes an emptied registry fail loudly instead of passing silently.

Bench hygiene: every switch flipped here is restored in `tearDown`. A switch left armed by a test is an
automation running on a bench nobody believes is armed.
"""
import unittest

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.automation import seed, status
from tatva_connect.automation.registry import AUTOMATIONS
from tatva_connect.automation.settings import is_enabled

_DOCTYPE = "CRM Tatva Automation"

_PARENT = {auto.key: auto.requires for auto in AUTOMATIONS}
_PAIRS = [(key, parent) for key, parent in _PARENT.items() if parent]


def _ancestors(key):
	chain, parent = [], _PARENT.get(key, "")
	while parent:
		chain.append(parent)
		parent = _PARENT.get(parent, "")
	return chain


class TestSwitchVisibility(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		if any(not frappe.db.exists(_DOCTYPE, auto.key) for auto in AUTOMATIONS):
			seed.sync_catalog()

	def setUp(self):
		super().setUp()
		self._before = {
			row.name: int(row.enabled or 0) for row in frappe.get_all(_DOCTYPE, fields=["name", "enabled"])
		}

	def tearDown(self):
		for name, was in self._before.items():
			if frappe.db.exists(_DOCTYPE, name) and int(frappe.db.get_value(_DOCTYPE, name, "enabled") or 0) != was:
				frappe.db.set_value(_DOCTYPE, name, "enabled", was, update_modified=False)
		super().tearDown()

	def _set(self, key, value):
		# Straight to the column — the controller cascade is exactly what this state escapes in the wild.
		frappe.db.set_value(_DOCTYPE, key, "enabled", 1 if value else 0, update_modified=False)

	def _by_key(self):
		return {row["key"]: row for row in status.switch_state()}

	def test_01_every_registry_switch_is_reported(self):
		"""A switch the read omits is a switch the operator still cannot see. Red here means the read grew
		a filter of its own instead of answering for the whole catalog."""
		self.assertTrue(AUTOMATIONS, "the registry is empty — this sweep would be vacuous")
		reported = self._by_key()
		missing = [auto.key for auto in AUTOMATIONS if auto.key not in reported]
		self.assertEqual(missing, [], f"the read omits {missing}")
		for auto in AUTOMATIONS:
			self.assertEqual(reported[auto.key]["area"], auto.key.split("::")[0])

	def test_02_a_switch_armed_above_a_dormant_parent_reads_broken(self):
		"""THE SILENT STATE. The row is ticked, the gate ignores it, nothing is logged. Red here means the
		operator is told the automation is on while it has never once run."""
		self.assertTrue(_PAIRS, "the registry declares no `requires` at all — this sweep would be vacuous")
		for child, parent in _PAIRS:
			with self.subTest(child=child, parent=parent):
				for ancestor in _ancestors(child):
					self._set(ancestor, 1)
				self._set(child, 1)
				self._set(parent, 0)
				row = self._by_key()[child]
				self.assertEqual(row["status"], status.BROKEN, f"{child} is armed above dormant {parent}")
				self.assertTrue(row["enabled"], "the stored tick must still read on — that is the whole point")
				self.assertEqual(
					row["blocked_by"], parent, f"{child} was flagged without naming what to switch on"
				)

	def test_03_a_dormant_switch_is_off_not_broken(self):
		"""Dormant is the shipped state, not a fault. Red here means the read cries wolf on every switch an
		operator has simply not reached yet, and the real broken chain is lost in the noise."""
		self.assertTrue(_PAIRS, "the registry declares no `requires` at all — this sweep would be vacuous")
		for child, parent in _PAIRS:
			with self.subTest(child=child, parent=parent):
				self._set(child, 0)
				self._set(parent, 0)
				row = self._by_key()[child]
				self.assertEqual(row["status"], status.OFF)
				self.assertEqual(row["blocked_by"], "")

	def test_04_the_status_never_disagrees_with_is_enabled(self):
		"""`is_enabled` is the one brain that decides whether an automation runs. Red here means this read
		grew a second opinion, and a screen that contradicts the gate is worse than no screen."""
		self.assertTrue(_PAIRS, "the registry declares no `requires` at all — this sweep would be vacuous")
		child, parent = _PAIRS[0]
		for ancestor in _ancestors(child):
			self._set(ancestor, 1)
		self._set(child, 1)
		self._set(parent, 0)
		for key, row in self._by_key().items():
			with self.subTest(key=key):
				self.assertEqual(row["status"] == status.ON, is_enabled(key))

	def test_05_the_read_is_permission_gated(self):
		"""It lists every automation in the business and which of them are running. Red here means anyone
		who can reach the RPC path reads the operator's whole control surface."""
		self.addCleanup(frappe.set_user, "Administrator")
		frappe.set_user("Guest")
		with self.assertRaises(frappe.PermissionError):
			status.switch_state()


if __name__ == "__main__":
	unittest.main()
