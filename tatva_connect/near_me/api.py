"""Near Me — a cross-business-line geographic directory of leads with a clinic anchor, for field reps.

HONESTY (NM-07): nothing here filters to doctors. The row set is "every CRM Lead with a pinned clinic
location" — for the practice grains that IS the doctor book, but the words say what the query does.

The page lives in the CRM fork (a left-menu item → a full screen: map + list); this module is its ONLY
brain — the ungated Desk page and its `leads_near` endpoint were retired (NM-06). Two gates, both
required (CLAUDE.md #6, ships dormant):
  • the `Location::NearMe::directory` automation switch (operator flips it on), and
  • the `Field Map User` role on the user (operator assigns it; seeded by schema_setup).
Neither defaults on, so the feature is invisible until an operator deliberately enables it.

This is a deliberate CROSS-GRAIN territory read: every anchored lead, across all business lines — the
user's explicit choice. The role+switch IS the boundary; `_assert_access` enforces it server-side
(fail-closed) before any data leaves, so the client can never widen scope.
"""
import frappe
from frappe import _
from frappe.utils import flt

from tatva_connect import automation
from tatva_connect.location.api import NEAR_MAX_ROWS, haversine, leads_within_radius

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


# `first_name` rides along for one reason: it is what the CRM labels an avatar with (`Leads.vue:451`
# image_label: lead.first_name). `lead_name` carries the salutation, so labelling an avatar with it draws
# "D" on every doctor in the territory — seventeen identical letters and no information.
_FIELDS = ["name", "lead_name", "first_name", "mobile_no", "image", "source",
		   "custom_clinic_latitude", "custom_clinic_longitude", "custom_clinic_address",
		   "custom_stage", "status", "custom_vertical"]


def _search(lat, lng, radius_km):
	"""One ring, as (rows, capped). get_all (NOT get_list): the territory view deliberately bypasses
	per-lead read scope — access is owned by _assert_access. Box+haversine math AND the row ceiling are
	the shared location.leads_within_radius (NM-04): nearest-first, truncated after the distance sort."""
	return leads_within_radius(lat, lng, radius_km, fields=_FIELDS, query=frappe.get_all)


def _by_name(q, lat, lng):
	"""Anchored leads whose name or number matches, ANYWHERE — a name is not a place, so a rep looking one up must not be answered "not within 120 km of you". Same rows, same ceiling, same nearest-first order as a ring; distance is still measured so the panel can say how far away the match is."""
	rows = frappe.get_all(  # authz-ok: tier-b — _assert_access owns the boundary, exactly as the ring query does
		"CRM Lead",
		filters={"custom_clinic_latitude": ["is", "set"], "custom_clinic_longitude": ["is", "set"]},
		or_filters={"lead_name": ["like", f"%{q.strip()}%"], "mobile_no": ["like", f"%{q.strip()}%"]},
		fields=_FIELDS,
		limit_page_length=NEAR_MAX_ROWS + 1,
	)
	near = [(r, round(haversine(flt(lat), flt(lng), r.custom_clinic_latitude, r.custom_clinic_longitude))) for r in rows]
	near.sort(key=lambda t: t[1])
	capped = len(near) > NEAR_MAX_ROWS
	return (near[:NEAR_MAX_ROWS] if capped else near), capped


@frappe.whitelist()
def doctors_in_territory(lat, lng, radius_km=None, q=None):
	"""Every anchored lead near (lat,lng), nearest first — cross-grain, all owners.

	`radius_km` given (the user picked one) => that ring, exactly. Omitted (first load) => walk
	RADIUS_LADDER and stop at the first ring that holds a row, so the page never opens on an empty
	list where results merely sit further out than one hardcoded default. Either way the radius that
	answered comes back with the rows — the panel and the map circle both read it, so what the user is
	told matches what was searched. `capped` says the ring held more than NEAR_MAX_ROWS and only the
	nearest were returned, so the client can render the count honestly (C7)."""
	_assert_access()
	if (q or "").strip():
		near, capped = _by_name(q, lat, lng)
		used = 0  # no ring was searched; `scope` is what the panel reads, never this number.
	else:
		rungs = [flt(radius_km)] if flt(radius_km) > 0 else list(RADIUS_LADDER)
		used, near, capped = rungs[-1], [], False
		for rung in rungs:
			near, capped = _search(lat, lng, rung)
			if near:
				used = rung
				break
	return {
		# The MODE, said plainly. A ring and a name answer different questions, and the panel must not infer which from a radius of zero — that renders as "within 0 km".
		"scope": "search" if (q or "").strip() else "ring",
		"radius_km": used,
		"capped": capped,
		"doctors": [
			{
				"name": r.name,
				"title": r.lead_name or r.name,
				# The avatar's label, under the key and by the rule the CRM's own lists already use.
				"image_label": r.first_name or r.lead_name or r.name,
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
