# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The WRITE side: a wildcard entitlement axis resolved to one concrete leaf on the create form.

Entitlement is a REGION; a lead is a POINT. A rep entitled to all of GoodFlip Care/Anaya must file each
lead under ONE of its programmes — before this, the stamp copied the region straight onto the lead and a
blank programme axis landed as a blank column, so that rep could not create a Nivolumab lead at all.

LAYER 1 of two. Self-made fixtures, rolled back, each fails first. No real tuple counts are asserted here
(the test DB carries no business data, so such an assertion passes by being empty) — "an Anaya rep really
creates a Sigrima lead" is LAYER 2, a live bench + browser run recorded as Gate 2 evidence.

The entitlement SOURCE is mocked, exactly as `test_lead_grain_stamp` does: it is the ONE brain and has its
own coverage. What is proved here is the stamp's own resolve / clamp / refuse decisions.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \
        --module tatva_connect.tests.lead.test_grain_write_pick
"""
import unittest
from unittest.mock import patch

import frappe

from tatva_connect.lead.leads import stamp_entitled_grain

V, G = "GoodFlip Care", "Anaya"
P1, P2 = "Nivolumab", "Sigrima"
OFF_TREE = "Niva Bupa"

WILDCARD = {(V, G, "")}          # entitled to the whole group — the axis that must be picked
CONCRETE = {(V, G, P1)}          # nothing to ask
PROGRAMLESS = {("GoodFlip", "Insurers", "")}   # a group the registry declares no programmes under

_ENABLED = "tatva_connect.lead.leads.automation.is_enabled"
_GRAINS = "tatva_connect.access.entitlement.entitled_grains"
_ENTITLED = "tatva_connect.access.entitlement.grain_entitled"
_PROGRAMS = "tatva_connect.access.entitlement.programs_under"
_GROUPS = "tatva_connect.access.entitlement.groups_under"


def _doc(vertical=None, group=None, program=None, ignore=False):
	"""A minimal stand-in for the CRM Lead doc the hook receives (frappe._dict gives attribute and
	.get access, exactly like a real Document) — the same shape test_lead_grain_stamp uses."""
	return frappe._dict(
		custom_vertical=vertical,
		custom_group=group,
		custom_current_program=program,
		flags=frappe._dict(ignore_permissions=ignore),
	)


class TestGrainWritePick(unittest.TestCase):
	def setUp(self):
		# Both switches on: the grain stamp itself, and the registry flag that arms wildcard resolution.
		patch(_ENABLED, return_value=True).start()
		patch(_PROGRAMS, return_value=[P1, P2, "Tukavo", "Ujvira"]).start()
		patch(_GROUPS, return_value=[G]).start()

	def tearDown(self):
		patch.stopall()

	def _entitlement(self, grains, entitled=True):
		patch(_GRAINS, return_value=grains).start()
		patch(_ENTITLED, return_value=entitled).start()

	def _axes(self, doc):
		return (doc.custom_vertical, doc.custom_group, doc.custom_current_program)

	# ---- the fix: a wildcard axis resolves to the picked leaf ------------------------------

	def test_picked_program_is_stamped_as_the_concrete_leaf(self):
		"""THE bug, fixed: the rep covers all of Anaya and files this lead under Nivolumab."""
		self._entitlement(WILDCARD)
		doc = _doc(program=P1)
		stamp_entitled_grain(doc)
		self.assertEqual(self._axes(doc), (V, G, P1))

	def test_a_second_programme_resolves_just_as_well(self):
		"""Sigrima — the programme that had 207 real leads and no contract of its own."""
		self._entitlement(WILDCARD)
		doc = _doc(program=P2)
		stamp_entitled_grain(doc)
		self.assertEqual(self._axes(doc), (V, G, P2))

	def test_the_region_axes_are_applied_even_when_the_form_omits_them(self):
		"""The form sends only the pick; vertical and group were never the user's to choose."""
		self._entitlement(WILDCARD)
		doc = _doc(program=P2)
		stamp_entitled_grain(doc)
		self.assertEqual(doc.custom_vertical, V)
		self.assertEqual(doc.custom_group, G)

	# ---- the refusals ---------------------------------------------------------------------

	def test_no_pick_with_options_is_refused(self):
		"""Previously this silently stamped a BLANK programme. It must now refuse."""
		self._entitlement(WILDCARD)
		with self.assertRaises(frappe.ValidationError):
			stamp_entitled_grain(_doc())

	def test_the_refusal_names_the_values_that_would_be_accepted(self):
		self._entitlement(WILDCARD)
		with self.assertRaises(frappe.ValidationError) as cm:
			stamp_entitled_grain(_doc())
		message = str(cm.exception)
		for programme in (P1, P2, "Tukavo", "Ujvira"):
			self.assertIn(programme, message)

	def test_off_tree_pick_is_refused_by_the_clamp(self):
		"""The pick is never trusted by the resolver — grain_entitled is the one that clamps it."""
		self._entitlement(WILDCARD, entitled=False)
		with self.assertRaises(frappe.PermissionError):
			stamp_entitled_grain(_doc(program=OFF_TREE))

	# ---- the unchanged cases --------------------------------------------------------------

	def test_a_fully_concrete_region_asks_nothing(self):
		self._entitlement(CONCRETE)
		doc = _doc()
		stamp_entitled_grain(doc)
		self.assertEqual(self._axes(doc), (V, G, P1))

	def test_a_group_with_no_declared_programmes_stays_blank(self):
		"""No options -> nothing to ask, so the blank programme is correct and must not refuse."""
		self._entitlement(PROGRAMLESS)
		patch(_PROGRAMS, return_value=[]).start()
		patch(_GROUPS, return_value=["Insurers"]).start()
		doc = _doc()
		stamp_entitled_grain(doc)
		self.assertEqual(self._axes(doc), ("GoodFlip", "Insurers", None))

	def test_flag_off_keeps_todays_behaviour(self):
		"""Disarmed, a wildcard region still stamps a blank programme — the old behaviour, byte-identical.
		This is what makes a disarm a true revert rather than a rebuild."""
		patch(_ENABLED, side_effect=lambda key: key != "Access::Grain::registry").start()
		self._entitlement(WILDCARD)
		doc = _doc()
		stamp_entitled_grain(doc)
		self.assertEqual(self._axes(doc), (V, G, None))

	def test_automated_paths_are_untouched(self):
		"""Partner API / intake / Facebook insert with ignore_permissions and pin their own leaf via
		their contract — the stamp returns before any of this runs, flag or no flag."""
		self._entitlement(WILDCARD)
		doc = _doc(ignore=True)
		stamp_entitled_grain(doc)
		self.assertEqual(self._axes(doc), (None, None, None))


if __name__ == "__main__":
	unittest.main()
