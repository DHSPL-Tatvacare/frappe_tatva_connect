# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""My day: today's open tasks as stops, ordered by the route-order seam the operator picks in CRM Maps Settings."""
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_days, now_datetime

from tatva_connect.automation import seed
from tatva_connect.location.route_order import google_routes, resolve, straight_line
from tatva_connect.near_me import api
from tatva_connect.schema_setup import _ensure_field_map_role

_USER = "my-day-tester@example.com"
# A remote anchor so nothing else on the bench lands in this test's day.
_HERE = {"lat": 2.5, "lng": 2.5}


def _pt(lat, lng, tag):
	return {"id": tag, "lat": lat, "lng": lng, "tag": tag}


def _scope():
	return f"test:{frappe.generate_hash(length=8)}"  # a fresh plan per test, so no test reads another's remembered order


class TestRouteOrder(FrappeTestCase):
	def setUp(self):
		seed.sync_catalog()
		self.settings = frappe.get_doc("CRM Maps Settings")
		self._was = (self.settings.route_order_provider, self.settings.get_password("google_maps_api_key", raise_exception=False))

	def tearDown(self):
		self.settings.reload()
		self.settings.route_order_provider, self.settings.google_maps_api_key = self._was[0], self._was[1] or ""
		self.settings.save(ignore_permissions=True)

	def _pick(self, provider):
		self.settings.route_order_provider = provider
		self.settings.google_maps_api_key = "test-server-key"
		self.settings.save(ignore_permissions=True)

	def test_straight_line_visits_the_nearest_next(self):
		a, b, c = _pt(2.5, 2.51, "a"), _pt(2.5, 2.53, "c"), _pt(2.5, 2.52, "b")
		ordered = straight_line.order(_HERE, [b, a, c])
		self.assertFalse(ordered.by_road)
		self.assertEqual([s["tag"] for s in ordered.stops], ["a", "b", "c"])
		self.assertEqual(ordered.provider, straight_line.LABEL)
		self.assertGreater(ordered.distance_m, 0)

	def test_the_setting_offers_exactly_the_providers_the_seam_has(self):
		"""The Select in CRM Maps Settings and the resolver's map are one list — a provider can't be offered without code behind it."""
		options = frappe.get_meta("CRM Maps Settings").get_field("route_order_provider").options.split("\n")
		self.assertEqual({o for o in options if o}, set(resolve.PROVIDERS))

	def test_google_is_never_called_unless_the_setting_picks_it(self):
		self._pick(straight_line.LABEL)
		stops = [_pt(2.5, 2.51, "a"), _pt(2.5, 2.52, "b"), _pt(2.5, 2.53, "c")]
		with patch.object(google_routes, "make_post_request") as http:
			ordered = resolve.order(_HERE, stops, _scope())
		http.assert_not_called()
		self.assertEqual(ordered.provider, straight_line.LABEL)

	def test_google_orders_the_day_when_picked_and_switched_on(self):
		self._pick(google_routes.LABEL)
		a, b, c = _pt(2.5, 2.51, "a"), _pt(2.5, 2.52, "b"), _pt(2.5, 2.53, "c")
		# c is farthest, so it is the destination; Google answers that b comes before a.
		reply = {"routes": [{"optimizedIntermediateWaypointIndex": [1, 0], "distanceMeters": 4321,
							 "polyline": {"encodedPolyline": "abc~"}}]}
		with patch.object(google_routes, "make_post_request", return_value=reply) as http:
			ordered = resolve.order(_HERE, [a, b, c], _scope())
		body = http.call_args.kwargs["json"]
		self.assertTrue(body["optimizeWaypointOrder"])
		self.assertEqual(body["destination"]["location"]["latLng"]["longitude"], 2.53)
		self.assertEqual([s["tag"] for s in ordered.stops], ["b", "a", "c"])
		self.assertEqual((ordered.provider, ordered.by_road, ordered.distance_m), (google_routes.LABEL, True, 4321))
		self.assertEqual(ordered.polyline, "abc~")  # the road path came in the same call

	def test_a_google_failure_still_orders_the_day(self):
		self._pick(google_routes.LABEL)
		stops = [_pt(2.5, 2.51, "a"), _pt(2.5, 2.52, "b"), _pt(2.5, 2.53, "c")]
		scope = _scope()
		with patch.object(google_routes, "make_post_request", side_effect=frappe.ValidationError("boom")) as http:
			ordered = resolve.order(_HERE, stops, scope)
			resolve.order(_HERE, stops, scope)
		self.assertEqual(ordered.provider, straight_line.LABEL)
		self.assertEqual(len(ordered.stops), 3)
		self.assertEqual(http.call_count, 2)  # a fallback is never remembered, so Google is asked again

	def test_one_plan_asks_google_once_and_a_moved_stop_asks_again(self):
		"""RED before: every My day read paid Google. The order is remembered per plan; a change to the stops is a new plan."""
		self._pick(google_routes.LABEL)
		scope, stops = _scope(), [_pt(2.5, 2.51, "a"), _pt(2.5, 2.52, "b"), _pt(2.5, 2.53, "c")]
		reply = {"routes": [{"optimizedIntermediateWaypointIndex": [1, 0], "distanceMeters": 10}]}
		with patch.object(google_routes, "make_post_request", return_value=reply) as http:
			first = resolve.order(_HERE, stops, scope)
			again = resolve.order({"lat": 9, "lng": 9}, list(reversed(stops)), scope)  # the rep moved; same plan
			self.assertEqual(http.call_count, 1)
			self.assertEqual([s["tag"] for s in again.stops], [s["tag"] for s in first.stops])
			moved = [_pt(2.5, 2.51, "a"), _pt(2.5, 2.52, "b"), _pt(2.6, 2.53, "c")]
			resolve.order(_HERE, moved, scope)
			self.assertEqual(http.call_count, 2)


class TestMyDay(FrappeTestCase):
	def setUp(self):
		seed.sync_catalog()
		_ensure_field_map_role()
		self._docs = []
		if not frappe.db.exists("User", _USER):
			frappe.get_doc({"doctype": "User", "email": _USER, "first_name": "My Day", "send_welcome_email": 0}).insert(ignore_permissions=True)
		user = frappe.get_doc("User", _USER)
		user.add_roles(api.ROLE, "Sales User")
		frappe.db.set_value("CRM Tatva Automation", api.SWITCH, "enabled", 1)

	def tearDown(self):
		frappe.set_user("Administrator")
		for doctype, name in reversed(self._docs):
			frappe.delete_doc(doctype, name, force=True, ignore_permissions=True)
		frappe.db.set_value("CRM Tatva Automation", api.SWITCH, "enabled", 0)

	def _lead(self, lat=None, lng=None):
		doc = frappe.get_doc({"doctype": "CRM Lead", "first_name": "Dr", "lead_name": "Dr Day", "status": "New",
							  "custom_clinic_latitude": lat, "custom_clinic_longitude": lng}).insert(ignore_permissions=True)
		self._docs.append(("CRM Lead", doc.name))
		return doc.name

	def _task(self, lead, due, status="Todo", user=_USER):
		doc = frappe.get_doc({"doctype": "CRM Task", "title": f"Visit {lead}", "status": status, "assigned_to": user,
							  "due_date": due, "reference_doctype": "CRM Lead", "reference_docname": lead}).insert(ignore_permissions=True)
		self._docs.append(("CRM Task", doc.name))
		return doc.name

	def test_only_my_open_tasks_due_today_become_stops_in_order(self):
		today = now_datetime()
		far, near, unpinned = self._lead(2.5, 2.52), self._lead(2.5, 2.51), self._lead()
		self._task(far, today)
		self._task(near, today)
		self._task(unpinned, today)
		self._task(near, add_days(today, 1))  # tomorrow
		self._task(far, today, status="Done")  # closed
		self._task(near, today, user="Administrator")  # not mine

		frappe.set_user(_USER)
		day = api.my_day(_HERE["lat"], _HERE["lng"])
		self.assertEqual([s["name"] for s in day["stops"]], [near, far])
		self.assertEqual([len(s["tasks"]) for s in day["stops"]], [1, 1])
		self.assertEqual([u["name"] for u in day["unlocated"]], [unpinned])
		self.assertEqual(day["provider"], straight_line.LABEL)
		self.assertTrue(day["is_today"])

	def test_a_past_day_is_not_planned(self):
		frappe.set_user(_USER)
		with self.assertRaises(frappe.ValidationError):
			api.my_day(_HERE["lat"], _HERE["lng"], add_days(now_datetime(), -1).date())
