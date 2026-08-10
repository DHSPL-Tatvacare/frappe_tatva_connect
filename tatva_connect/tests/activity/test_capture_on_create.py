# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The clinic anchor may now be set when the doctor is CREATED, not only at the first visit — for a tracked grain, and for nobody else."""
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.location import api as location

DOC = frappe._dict


def _lead(**kw):
	"""A stand-in for the inserted lead: capture_on_create only reads `name` and the two fix keys."""
	return DOC({"doctype": "CRM Lead", "name": "LEAD-TEST-1", "lat": None, "lng": None, **kw})


class TestCaptureOnCreate(FrappeTestCase):
	def test_a_tracked_grain_with_a_fix_pins_the_clinic(self):
		doc = _lead(lat=12.9141, lng=77.6411)
		with patch.object(location, "is_location_tracked", return_value=200), \
				patch.object(location, "ensure_anchor") as anchor:
			location.capture_on_create(doc)
		anchor.assert_called_once_with(doc, 12.9141, 77.6411)

	def test_an_untracked_grain_is_never_pinned(self):
		"""The gate is the grain, so a Goodflip lead carrying a fix is left exactly as it was."""
		doc = _lead(lat=12.9141, lng=77.6411)
		with patch.object(location, "is_location_tracked", return_value=None), \
				patch.object(location, "ensure_anchor") as anchor:
			location.capture_on_create(doc)
		anchor.assert_not_called()

	def test_no_fix_writes_nothing(self):
		"""A refused prompt must not reach ensure_anchor: with no coordinate it would pin the doctor at (0, 0)."""
		with patch.object(location, "is_location_tracked", return_value=200), \
				patch.object(location, "ensure_anchor") as anchor:
			location.capture_on_create(_lead())
		anchor.assert_not_called()

	def test_the_switch_alone_stops_it(self):
		"""`is_location_tracked` returns None while `Location::Google::capture` is dormant — one switch, and this path reads it through the same resolver every other location caller does."""
		doc = _lead(lat=12.9141, lng=77.6411)
		with patch("tatva_connect.automation.is_enabled", return_value=False), \
				patch.object(location, "ensure_anchor") as anchor:
			location.capture_on_create(doc)
		anchor.assert_not_called()

	def test_the_hook_runs_before_the_wildcard(self):
		"""Ordering is the reason this is a doctype hook: frappe composes doctype handlers before `*` (document.py:1598), so a Created-entry workflow reads a doctor that already has coordinates."""
		from tatva_connect import hooks

		lead_hooks = hooks.doc_events["CRM Lead"]["after_insert"]
		self.assertEqual(lead_hooks, ["tatva_connect.location.api.capture_on_create"])
		self.assertIn("tatva_connect.workflow_engine.triggers.on_created", hooks.doc_events["*"]["after_insert"])


class TestGrainProbe(FrappeTestCase):
	def test_the_form_probe_and_the_lead_gate_are_one_resolver(self):
		"""The browser asks by AXES before a lead exists; the hook asks by lead. Both land on `tracked_radius`, so the form can never offer a capture the server would refuse."""
		with patch.object(location, "tracked_radius", return_value=200) as resolver:
			self.assertTrue(location.grain_is_tracked("Tatvapractice", "India", "Field-Sales"))
		resolver.assert_called_once_with("Tatvapractice", "India", "Field-Sales")

	def test_an_unknown_grain_is_not_tracked(self):
		with patch.object(location, "tracked_radius", return_value=None):
			self.assertFalse(location.grain_is_tracked("Goodflip", "India", "Inside-Sales"))
