# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Task 12 - the switch catalog collapse (Part E of the v2 plan). The engine's authoring knobs are
exactly four `Task::Automation::*` rows: `rules` (master) / `sends` (dormant send gate) /
`run-log-sweep` (retention) / `resume` (Wait sweep). Integration kill-switches (WhatsApp/Telephony/
Storage/Intake/Partner) and the separate `Task::CRM Task::guards` backstop are untouched (Part E).

No DB writes: every assertion here reads `AUTOMATIONS` + `hooks.py` in memory, same as
`test_watchable_drift.py`'s pattern - the planted-bad temporarily monkeypatches `hooks.scheduler_events`
and restores it in `finally`. `FrappeTestCase` is only needed because `drift.assert_registered()` calls
`frappe.throw`, which needs a site context; nothing is ever inserted."""
import unittest

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect import hooks
from tatva_connect.automation import drift
from tatva_connect.automation.registry import AUTOMATIONS

_COLLAPSED = {
	"Task::Automation::rules",
	"Task::Automation::sends",
	"Task::Automation::run-log-sweep",
	"Task::Automation::resume",
}


class TestSwitchCatalogCollapse(FrappeTestCase):
	# (a) the collapse: exactly the four keys under Task::Automation:: - no stray fifth switch, none
	# of the four missing.
	def test_exactly_four_collapsed_keys(self):
		area_keys = {a.key for a in AUTOMATIONS if a.key.startswith("Task::Automation::")}
		self.assertEqual(area_keys, _COLLAPSED)

	# (b) the collapsed catalog, as shipped, satisfies the drift gate - every doc_event/scheduler path
	# wired in hooks.py (including the resume sweep's own cron entry) is backed by a registry row.
	def test_drift_gate_passes_with_collapsed_catalog(self):
		drift.assert_registered()  # no throw

	# (c) planted-bad (recall guard, S.6): a scheduler path with no registry `backs` entry must still
	# fail the gate - proves the gate BITES, not just that the happy-path catalog is quiet.
	def test_unregistered_scheduled_path_fails_drift(self):
		fake = "tatva_connect.automation.resume.not_a_real_registered_handler"
		patched = {bucket: dict(v) if isinstance(v, dict) else list(v) for bucket, v in hooks.scheduler_events.items()}
		patched["cron"]["*/5 * * * *"] = [fake]
		orig = hooks.scheduler_events
		hooks.scheduler_events = patched
		try:
			with self.assertRaises(frappe.exceptions.ValidationError):
				drift.assert_registered()
		finally:
			hooks.scheduler_events = orig

	# (d) regression: the resume sweep's real cron path is owned by exactly one row (resume, not
	# rules) - the collapse didn't leave it double-backed or orphaned.
	def test_resume_sweep_path_owned_by_resume_row_only(self):
		path = "tatva_connect.automation.resume.sweep_resume"
		owners = [a.key for a in AUTOMATIONS if path in a.backs]
		self.assertEqual(owners, ["Task::Automation::resume"])


if __name__ == "__main__":
	unittest.main()
