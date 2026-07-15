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
import frappe
from frappe import _
from frappe.utils import flt

from tatva_connect import automation
from tatva_connect.location.api import leads_within_radius

SWITCH = "Location::NearMe::directory"
ROLE = "Field Map User"
# First load asks for no radius and the server walks this ladder, stopping at the first ring that holds
# a doctor. A fixed default cannot be right in both a dense city and a sparse territory: 15 km showed an
# empty panel on a rep whose nearest doctor was further out, and widening the default to fit that rep
# would scan a needless disc everywhere else. The rung that answered is returned, and the dropdown
# (RADIUS_CHOICES) remains the user's override.
RADIUS_LADDER = (15.0, 30.0, 60.0, 120.0)
DEFAULT_RADIUS_KM = RADIUS_LADDER[0]


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


_FIELDS = ["name", "lead_name", "mobile_no", "image", "source",
		   "custom_clinic_latitude", "custom_clinic_longitude", "custom_clinic_address",
		   "custom_stage", "status", "custom_vertical"]


def _search(lat, lng, radius_km):
	"""One ring. get_all (NOT get_list): the territory view deliberately bypasses per-lead read scope —
	access is owned by _assert_access. Box+haversine math is the shared location.leads_within_radius."""
	return leads_within_radius(lat, lng, radius_km, fields=_FIELDS, query=frappe.get_all)


@frappe.whitelist()
def doctors_in_territory(lat, lng, radius_km=None):
	"""Every doctor lead with a clinic anchor near (lat,lng), nearest first — cross-grain, all owners.

	`radius_km` given (the user picked one) => that ring, exactly. Omitted (first load) => walk
	RADIUS_LADDER and stop at the first ring that holds a doctor, so the page never opens on an empty
	list where doctors merely sit further out than one hardcoded default. Either way the radius that
	answered comes back with the rows — the panel and the map circle both read it, so what the user is
	told matches what was searched."""
	_assert_access()
	rungs = [flt(radius_km)] if flt(radius_km) > 0 else list(RADIUS_LADDER)
	used, near = rungs[-1], []
	for rung in rungs:
		near = _search(lat, lng, rung)
		if near:
			used = rung
			break
	return {
		"radius_km": used,
		"doctors": [
			{
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
				"distance_m": dist,
			}
			for r, dist in near
		],
	}
