# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""stamp_entitled_grain — the CRM Lead before_validate grain clamp.

Pins every BRANCH of the hook in isolation: the automation gate, the ignore_permissions skip
(partner/intake carry their own clamp), the System-Manager pass-through, and the
single->auto / multi->reject / out-of-entitlement->reject entitlement rules.

The entitlement SOURCE (access.entitlement.entitled_grains / grain_entitled) is mocked — it is
the ONE brain and has its own coverage under tatva_connect/tests/authz (the permission-test
framework). Here we prove only the stamp's own decision logic. End-to-end grain scoping with
real users/leads lives in that framework, not here.
"""
import unittest
from unittest.mock import patch

import frappe

from tatva_connect.access.entitlement import ALL_GRAINS
from tatva_connect.lead.leads import stamp_entitled_grain

ONE = ("GoodFlip Care", "Anaya", "Nivolumab")
TWO = {("GoodFlip Care", "Anaya", "Nivolumab"), ("TatvaPractice", "India", "FieldSales")}

_ENABLED = "tatva_connect.lead.leads.automation.is_enabled"
_GRAINS = "tatva_connect.access.entitlement.entitled_grains"
_ENTITLED = "tatva_connect.access.entitlement.grain_entitled"


def _doc(ignore=False, vertical=None, group=None, program=None):
	"""A minimal stand-in for the CRM Lead doc the hook receives (frappe._dict gives both
	attribute and .get access, exactly like a real Document)."""
	return frappe._dict(
		custom_vertical=vertical,
		custom_group=group,
		custom_current_program=program,
		flags=frappe._dict(ignore_permissions=ignore),
	)


class TestLeadGrainStamp(unittest.TestCase):
	def setUp(self):
		# the hook only runs when the operator has armed the automation
		patch(_ENABLED, return_value=True).start()

	def tearDown(self):
		patch.stopall()

	def _entitlement(self, grains, entitled=True):
		patch(_GRAINS, return_value=grains).start()
		patch(_ENTITLED, return_value=entitled).start()

	def test_switch_off_is_noop(self):
		patch(_ENABLED, return_value=False).start()
		self._entitlement(TWO)
		doc = _doc()  # blank + multi would normally throw; the gate short-circuits first
		stamp_entitled_grain(doc)
		self.assertIsNone(doc.custom_vertical)

	def test_ignore_permissions_is_skipped(self):
		self._entitlement(TWO)
		doc = _doc(ignore=True)  # partner/intake path — they force their own grain
		stamp_entitled_grain(doc)
		self.assertIsNone(doc.custom_vertical)

	def test_system_manager_passes_through(self):
		self._entitlement(ALL_GRAINS)
		doc = _doc(vertical="Anything", group="Goes", program="Here")
		stamp_entitled_grain(doc)
		self.assertEqual(doc.custom_vertical, "Anything")  # untouched, any grain allowed

	def test_no_entitlement_is_rejected(self):
		self._entitlement(set())
		with self.assertRaises(frappe.PermissionError):
			stamp_entitled_grain(_doc())

	def test_single_grain_is_auto_stamped(self):
		self._entitlement({ONE})
		doc = _doc()
		stamp_entitled_grain(doc)
		self.assertEqual(
			(doc.custom_vertical, doc.custom_group, doc.custom_current_program), ONE
		)

	def test_multi_grain_blank_is_rejected(self):
		self._entitlement(TWO)
		with self.assertRaises(frappe.ValidationError):  # "Select a grain"
			stamp_entitled_grain(_doc())

	def test_provided_grain_in_entitlement_passes(self):
		self._entitlement(TWO, entitled=True)
		doc = _doc(vertical="GoodFlip Care", group="Anaya", program="Nivolumab")
		stamp_entitled_grain(doc)
		self.assertEqual(doc.custom_vertical, "GoodFlip Care")  # kept as picked

	def test_provided_grain_out_of_entitlement_is_rejected(self):
		self._entitlement(TWO, entitled=False)
		with self.assertRaises(frappe.PermissionError):
			stamp_entitled_grain(_doc(vertical="Other", group="X", program="Y"))
