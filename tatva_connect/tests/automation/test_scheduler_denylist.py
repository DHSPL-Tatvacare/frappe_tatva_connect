# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The scheduler denylist (docs/investigations/scheduled-jobs-audit.md) and the two ways it rots.

Bench-run, NOT tests/static: that directory is the CI gate and is pure-python by contract (no frappe
import, no site — see .github/workflows/static.yml). These checks need get_hooks and the real
Scheduled Job Type table, so they live here with the rest of the bench lane.

A denylist that names a method nobody declares any more is WORSE than no denylist: it reads as "that
job is handled" while the job it was meant to stop either no longer exists (dead weight) or was renamed
upstream and is quietly running again. `apply()` cannot throw on a missing row — that would fail the
whole migrate on any site that drops an app — so the drift has to be caught HERE.

The second rot is a collision with the automation control plane. `CRM Tatva Automation` toggles own
their own scheduled jobs (registry `Auto.backs` + `Auto.activator` -> `Scheduled Job Type.stopped`,
e.g. observability/rollup.apply_rollup). Those jobs are OPERATOR-controlled: the toggle decides. If a
method ever appears in BOTH, the operator flips the toggle on and the next `bench migrate` silently
stops it again — a live feature that dies on deploy with nothing in the logs. The two mechanisms answer
different questions (is this toggle on? vs is this third-party job usable here at all?) and their
method sets must stay disjoint.
"""
import unittest

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect import scheduler_denylist
from tatva_connect.automation.registry import AUTOMATIONS

_JOB = "Scheduled Job Type"


def _declared_methods():
	"""Every method any installed app declares in `scheduler_events` (the cron key is a dict of
	cron-format -> methods; every other key is a plain list)."""
	declared = set()
	for events in (frappe.get_hooks("scheduler_events") or {}).values():
		if isinstance(events, dict):
			for methods in events.values():
				declared.update(methods)
		elif isinstance(events, list | tuple):
			for entry in events:
				if isinstance(entry, dict):
					for methods in entry.values():
						declared.update(methods)
				else:
					declared.add(entry)
	return declared


def _toggle_owned_methods():
	"""Scheduled jobs owned by a `CRM Tatva Automation` toggle — the operator arms these, not us."""
	owned = set()
	for auto in AUTOMATIONS:
		if auto.fires_on == "Schedule":
			owned.update(auto.backs or [])
	return owned


class TestSchedulerDenylist(FrappeTestCase):
	def test_every_denylisted_method_is_still_declared_by_some_app(self):
		"""A renamed or removed upstream method leaves an entry that stops nothing, forever, silently."""
		orphans = sorted(scheduler_denylist.DENYLISTED_METHODS - _declared_methods())
		self.assertEqual(
			orphans, [],
			"denylist entries no longer declared by any installed app's scheduler_events — either the "
			"method was renamed upstream (and that job is running again, unstopped) or the app is gone "
			"and the entry is dead weight. Re-audit, then fix the list: " + ", ".join(orphans),
		)

	def test_no_denylisted_method_is_owned_by_an_automation_toggle(self):
		"""Deny and toggle must never fight over one job — the toggle would lose, on every migrate."""
		collisions = sorted(scheduler_denylist.DENYLISTED_METHODS & _toggle_owned_methods())
		self.assertEqual(
			collisions, [],
			"these methods are BOTH denylisted and owned by a CRM Tatva Automation toggle: an operator "
			"enabling the toggle would have the job stopped again by the next migrate, with nothing in "
			"the logs. One mechanism per job: " + ", ".join(collisions),
		)

	def test_the_list_has_no_duplicates(self):
		methods = [method for method, _reason in scheduler_denylist.DENYLIST]
		self.assertEqual(len(methods), len(set(methods)), "the same method is denylisted twice")

	def test_every_entry_carries_a_reason(self):
		for method, reason in scheduler_denylist.DENYLIST:
			self.assertTrue(reason and reason.strip(), f"{method} is denylisted with no reason given")

	def test_apply_really_stops_every_job_it_names(self):
		"""Behavioural, against the real table: unstop them all, run apply(), and read the flag back."""
		named = frappe.get_all(
			_JOB, filters={"method": ("in", list(scheduler_denylist.DENYLISTED_METHODS))}, pluck="name"
		)
		self.assertTrue(named, "not one denylisted method has a Scheduled Job Type row — the list cannot be right")
		for name in named:
			frappe.db.set_value(_JOB, name, "stopped", 0)
		still_running = frappe.get_all(_JOB, filters={"name": ("in", named), "stopped": 0}, pluck="name")
		self.assertEqual(len(still_running), len(named), "setup failed: the rows were not all running")

		scheduler_denylist.apply()

		running = frappe.get_all(_JOB, filters={"name": ("in", named), "stopped": 0}, pluck="method")
		self.assertEqual(sorted(running), [], "apply() left denylisted jobs running: " + ", ".join(sorted(running)))

	def test_apply_leaves_every_other_job_alone(self):
		"""The blast radius is the list and nothing else — no toggle-owned or core job may be touched."""
		before = {
			row.name: row.stopped
			for row in frappe.get_all(
				_JOB,
				filters={"method": ("not in", list(scheduler_denylist.DENYLISTED_METHODS))},
				fields=["name", "stopped"],
			)
		}
		scheduler_denylist.apply()
		after = {
			row.name: row.stopped
			for row in frappe.get_all(
				_JOB,
				filters={"method": ("not in", list(scheduler_denylist.DENYLISTED_METHODS))},
				fields=["name", "stopped"],
			)
		}
		self.assertEqual(before, after, "apply() changed the stopped flag of a job it does not name")


if __name__ == "__main__":
	unittest.main()
