"""Near Me — a cross-business-line geographic directory of doctor leads, for field reps.

The page lives in the CRM fork (a left-menu item → a full screen: map + doctor list); this
module is its ONLY brain. Two gates, both required (CLAUDE.md #6, ships dormant):
  • the `Location::NearMe::directory` automation switch (operator flips it on), and
  • the `Field Map User` role on the user (operator assigns it; seeded by schema_setup).
Neither defaults on, so the feature is invisible until an operator deliberately enables it.

Unlike `location.api.leads_near` (permission-scoped to a rep's OWN leads), this is a deliberate
CROSS-GRAIN territory read: every doctor lead with a pinned clinic, across all business lines —
the user's explicit choice. The role+switch IS the boundary; `_assert_access` enforces it
server-side (fail-closed) before any data leaves, so the client can never widen scope.
"""
import math

import frappe
from frappe import _
from frappe.utils import flt

from tatva_connect import automation
from tatva_connect.location.api import haversine

SWITCH = "Location::NearMe::directory"
ROLE = "Field Map User"
DEFAULT_RADIUS_KM = 15.0


def _can_access() -> bool:
	"""Both gates: the switch is on AND the user holds the Field Map User role."""
	return bool(automation.is_enabled(SWITCH) and ROLE in frappe.get_roles())


def _assert_access():
	"""Server-side fail-closed backstop — the page is gated client-side too, but the data read
	never trusts that. Mirrors the discipline in location.api (defense in depth)."""
	if not _can_access():
		frappe.throw(_("Near Me is not available for your account."), frappe.PermissionError)


@frappe.whitelist()
def near_me_access():
	"""What the SPA asks once on load to decide whether to show the Near Me menu item. Cheap, no
	data — just the gate state. Whether the desktop call button routes through telephony is a
	frontend concern (the CRM's own `callEnabled`, which actually wires `makeCall`), so it's not
	duplicated here where it could drift."""
	return {"visible": _can_access()}


@frappe.whitelist()
def doctors_in_territory(lat, lng, radius_km=DEFAULT_RADIUS_KM):
	"""Every doctor lead with a clinic anchor within `radius_km` of (lat,lng), nearest first —
	cross-grain, all owners. A bounding-box prefilter (indexed clinic lat/lng) narrows the set,
	then haversine refines to a true circle. `frappe.get_all` (NOT get_list) is deliberate: the
	territory view bypasses per-lead read scope — access is owned by `_assert_access` above."""
	_assert_access()
	lat, lng, radius_km = flt(lat), flt(lng), flt(radius_km) or DEFAULT_RADIUS_KM
	dlat = radius_km / 111.0
	dlng = radius_km / (111.0 * max(0.15, math.cos(math.radians(lat))))
	rows = frappe.get_all(
		"CRM Lead",
		filters={
			"custom_clinic_latitude": ["between", [lat - dlat, lat + dlat]],
			"custom_clinic_longitude": ["between", [lng - dlng, lng + dlng]],
		},
		fields=["name", "lead_name", "mobile_no", "image", "source",
				"custom_clinic_latitude", "custom_clinic_longitude", "custom_clinic_address",
				"custom_stage", "status", "custom_vertical"],
		limit_page_length=0,
	)
	out = []
	for r in rows:
		d = haversine(lat, lng, r.custom_clinic_latitude, r.custom_clinic_longitude)
		if d <= radius_km * 1000:
			out.append({
				"name": r.name,
				"title": r.lead_name or r.name,
				"mobile_no": r.mobile_no or "",
				"image": r.image or "",
				"lat": r.custom_clinic_latitude,
				"lng": r.custom_clinic_longitude,
				"address": r.custom_clinic_address or "",
				"stage": r.custom_stage or r.status or "",
				"source": r.source or "",
				"grain": r.custom_vertical or "",  # business-line label (display only, never a filter)
				"distance_m": int(round(d)),
			})
	out.sort(key=lambda x: x["distance_m"])
	return out
