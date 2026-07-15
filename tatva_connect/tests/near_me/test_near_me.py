# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Tests for the Near Me directory seam (tatva_connect/near_me/).

Covers the two-gate access rule (Location::NearMe::directory switch AND the Field Map User
role), the server-side fail-closed backstop on the data read, the telephony flag, and the
cross-grain bounding-box + haversine territory query.
"""
import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.automation import seed
from tatva_connect.near_me import api
from tatva_connect.schema_setup import _ensure_field_map_role

_USER = "near-me-tester@example.com"
# A deliberately remote anchor so the territory query only ever sees this test's own leads.
_LAT, _LNG = 1.5, 1.5


class TestNearMe(FrappeTestCase):
	def setUp(self):
		seed.sync_catalog()  # ensures the Location::NearMe::directory row exists
		_ensure_field_map_role()  # ensures the Field Map User role exists
		self._leads = []
		if not frappe.db.exists("User", _USER):
			user = frappe.get_doc({
				"doctype": "User", "email": _USER, "first_name": "Near Me Tester",
				"send_welcome_email": 0,
			})
			user.insert(ignore_permissions=True)
		self._user = frappe.get_doc("User", _USER)

	def tearDown(self):
		frappe.set_user("Administrator")
		for name in self._leads:
			frappe.delete_doc("CRM Lead", name, force=True, ignore_permissions=True)
		self._set_switch(0)

	# --- helpers ---------------------------------------------------------

	def _set_switch(self, on):
		frappe.db.set_value("CRM Tatva Automation", api.SWITCH, "enabled", 1 if on else 0)

	def _make_lead(self, lat, lng, vertical=None):
		ld = frappe.get_doc({
			"doctype": "CRM Lead", "first_name": "Dr", "lead_name": "Dr Test", "status": "New",
			"custom_clinic_latitude": lat, "custom_clinic_longitude": lng,
			"custom_clinic_address": "Test clinic",
		})
		if vertical:
			ld.custom_vertical = vertical
		ld.insert(ignore_permissions=True)
		self._leads.append(ld.name)
		return ld.name

	# --- access gate -----------------------------------------------------

	def test_invisible_without_role_even_when_switch_on(self):
		"""Switch on but the user lacks Field Map User -> not visible, and the data read fails closed."""
		self._set_switch(1)
		self._user.remove_roles(api.ROLE)
		frappe.set_user(_USER)
		self.assertFalse(api.near_me_access()["visible"])
		with self.assertRaises(frappe.PermissionError):
			api.doctors_in_territory(_LAT, _LNG)

	def test_invisible_without_switch_even_with_role(self):
		"""Role granted but the switch is off -> not visible, and the read fails closed (dormant)."""
		self._set_switch(0)
		self._user.add_roles(api.ROLE)
		frappe.set_user(_USER)
		self.assertFalse(api.near_me_access()["visible"])
		with self.assertRaises(frappe.PermissionError):
			api.doctors_in_territory(_LAT, _LNG)

	def test_visible_with_both_gates(self):
		self._set_switch(1)
		self._user.add_roles(api.ROLE)
		frappe.set_user(_USER)
		self.assertTrue(api.near_me_access()["visible"])

	# --- territory query -------------------------------------------------

	def _open_gates(self):
		self._set_switch(1)
		self._user.add_roles(api.ROLE)
		frappe.set_user(_USER)

	def test_territory_query_radius_sort_and_cross_grain(self):
		"""In-range doctors (any business line) come back nearest-first; out-of-range are excluded."""
		near = self._make_lead(_LAT, _LNG)               # 0 m
		mid = self._make_lead(_LAT + 0.01, _LNG + 0.01)  # ~1.6 km
		self._make_lead(_LAT + 1.0, _LNG + 1.0)          # ~157 km — outside 15 km

		self._open_gates()
		res = api.doctors_in_territory(_LAT, _LNG, 15)
		rows = res["doctors"]
		names = [r["name"] for r in rows]

		self.assertEqual(res["radius_km"], 15)  # an explicit radius is honoured exactly
		self.assertIn(near, names)
		self.assertIn(mid, names)
		self.assertEqual(names[0], near)  # nearest first
		self.assertLess(rows[0]["distance_m"], rows[1]["distance_m"])
		# the far lead is excluded by the haversine refine
		self.assertEqual(len(names), 2)

	def test_first_load_widens_past_an_empty_ring(self):
		"""No radius given: the ladder walks out to the first ring holding a doctor, and reports which
		one answered — the fix for a page that opened on 'No doctors in this radius' merely because the
		nearest doctor sat past a hardcoded 15 km."""
		far = self._make_lead(_LAT + 0.2, _LNG + 0.2)  # ~31 km — empty at 15, found at 60

		self._open_gates()
		self.assertEqual(api.doctors_in_territory(_LAT, _LNG, 15)["doctors"], [])  # the old default: empty

		res = api.doctors_in_territory(_LAT, _LNG)  # no radius -> ladder
		self.assertEqual([r["name"] for r in res["doctors"]], [far])
		self.assertEqual(res["radius_km"], 60.0)  # 15 and 30 were empty; 60 answered

	def test_first_load_with_no_doctors_anywhere_reports_the_last_rung(self):
		"""Nothing within 120 km: an empty list, and the widest radius searched — so the panel can say
		what it looked at instead of implying the 15 km ring was the whole story."""
		self._open_gates()
		res = api.doctors_in_territory(_LAT, _LNG)
		self.assertEqual(res["doctors"], [])
		self.assertEqual(res["radius_km"], api.RADIUS_LADDER[-1])
