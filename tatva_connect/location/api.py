"""Location guard + Google Maps Platform helpers (Geocoding + Static Maps), server-side.

Two layers, one brain:
  • Doctype-agnostic helpers — reverse_geocode a coordinate, static_map (key-safe image proxy),
    write_location (map coords onto a record incl. the native Geolocation GeoJSON Point).
  • The activity location guard — `location_guard_applies` is the SINGLE gate (visit_mode ==
    In-Person AND the lead grain is location-tracked, grain-scoped via taxonomy.grain). Both the
    activity writer (activity.api.save_activity) and the validate backstop (tasks.enforce_location)
    key off it; `set_or_check_anchor` owns the clinic-anchor + radius rule. (Phase B consolidated
    the v1 task-type trigger — `location_required` + task_location.js — into this; see archive/.)

Ships dormant (CLAUDE.md #6): kill-switch OFF, blank key, or no tracked grains => nothing fires
and activities behave exactly like Phase A. The Google key lives in a Password field, never
committed (this repo is public), and never reaches the browser — both Google calls run here.
"""
import math

import requests

import frappe
from frappe import _
from frappe.utils import cint, flt, format_datetime

from tatva_connect.taxonomy.grain import resolve_scoped

GEOCODE_URL = "https://maps.googleapis.com/maps/api/geocode/json"
STATICMAP_URL = "https://maps.googleapis.com/maps/api/staticmap"
# Fixed sample point for the settings "Check connection" button (TatvaCare HSR, Bengaluru).
_SAMPLE = (12.9141, 77.6411)
_TIMEOUT = 8


def _settings():
	return frappe.get_cached_doc("CRM Maps Settings")


def _api_key():
	return _settings().get_password("google_maps_api_key", raise_exception=False)


DEFAULT_RADIUS_M = 150


def _lead_axes(lead):
	v = frappe.db.get_value(
		"CRM Lead", lead, ["custom_vertical", "custom_group", "custom_current_program"], as_dict=True
	)
	if not v:
		return "", "", ""
	return (v.custom_vertical or ""), (v.custom_group or ""), (v.custom_current_program or "")


def is_location_tracked(lead):
	"""Allowed radius (metres) if this lead's grain is location-tracked, else None. Grain-scoped
	via the shared brain (blank axis = wildcard). Dormant: kill-switch off or no grains -> None."""
	s = _settings()
	if not s.enabled:
		return None
	grains = [
		{"vertical": r.vertical, "group": r.group, "program": r.program, "radius_m": r.radius_m}
		for r in s.location_tracked_grains
	]
	if not grains:
		return None
	winner = resolve_scoped(grains, *_lead_axes(lead))
	if not winner:
		return None
	return cint(winner.get("radius_m")) or DEFAULT_RADIUS_M


def location_guard_applies(task_type, lead):
	"""The ONE gate, called by save_activity AND the validate backstop: returns the allowed radius
	(metres) when the activity must capture+guard location (type visit_mode == In-Person AND the
	lead grain is tracked), else None. No fork: both paths key off this single predicate."""
	if not (task_type and lead):
		return None
	if frappe.db.get_value("CRM Task Type", task_type, "visit_mode") != "In-Person":
		return None
	return is_location_tracked(lead)


def haversine(lat1, lng1, lat2, lng2):
	"""Great-circle distance between two coordinates, in metres."""
	r = 6371000.0
	p1, p2 = math.radians(flt(lat1)), math.radians(flt(lat2))
	dphi = math.radians(flt(lat2) - flt(lat1))
	dlmb = math.radians(flt(lng2) - flt(lng1))
	a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
	return 2 * r * math.asin(math.sqrt(a))


def _geojson_point(lat, lng):
	"""A GeoJSON FeatureCollection holding one Point — what Frappe's native Geolocation field
	renders as a read-only Leaflet marker (no API key, no proxy)."""
	return frappe.as_json({
		"type": "FeatureCollection",
		"features": [{
			"type": "Feature",
			"properties": {},
			"geometry": {"type": "Point", "coordinates": [flt(lng), flt(lat)]},
		}],
	})


def set_or_check_anchor(lead, lat, lng, accuracy, radius):
	"""First in-person fix for a doctor sets the clinic anchor on the Lead; every later fix must
	land within radius + the reading's accuracy, else the save is BLOCKED with the distance."""
	ld = frappe.get_doc("CRM Lead", lead)
	if not (ld.custom_clinic_latitude and ld.custom_clinic_longitude):
		ld.custom_clinic_latitude = flt(lat)
		ld.custom_clinic_longitude = flt(lng)
		ld.custom_clinic_geo = _geojson_point(lat, lng)
		ld.save(ignore_permissions=True)
		return
	dist = haversine(ld.custom_clinic_latitude, ld.custom_clinic_longitude, lat, lng)
	allowed = radius + flt(accuracy)
	if dist > allowed:
		frappe.throw(
			_("Visit blocked — you are {0} m from the clinic (allowed {1} m).").format(
				int(round(dist)), int(round(allowed))
			),
			title=_("Out of range"),
		)


def _require_manager():
	"""A manager (the configured override role, or System Manager) may re-anchor a clinic."""
	roles = frappe.get_roles()
	role = _settings().manager_override_role
	if "System Manager" in roles or (role and role in roles):
		return
	frappe.throw(_("Only a manager can re-anchor a clinic location."), frappe.PermissionError)


@frappe.whitelist()
def reanchor(lead, lat, lng, accuracy=None):
	"""Manager-only: move a lead's clinic anchor to a new fix (override a stale/wrong anchor)."""
	_require_manager()
	ld = frappe.get_doc("CRM Lead", lead)
	ld.custom_clinic_latitude = flt(lat)
	ld.custom_clinic_longitude = flt(lng)
	ld.custom_clinic_geo = _geojson_point(lat, lng)
	ld.save(ignore_permissions=True)
	return {"latitude": ld.custom_clinic_latitude, "longitude": ld.custom_clinic_longitude}


@frappe.whitelist()
def location_needed(lead, task_type):
	"""Client probe: does this activity require a location capture? (In-Person + tracked grain.)"""
	return location_guard_applies(task_type, lead) is not None


@frappe.whitelist()
def lead_captures(lead):
	"""Newest-first projection of every in-person location capture (a CRM Task carrying coordinates)
	on a lead. The SINGLE captures projection — feeds BOTH the Desk 'Location Captures' listing and
	the SPA timeline map link. Read-only; no map key leaves the server (the thumbnail hits static_map)."""
	rows = frappe.get_all(
		"CRM Task",
		filters={
			"reference_doctype": "CRM Lead",
			"reference_docname": lead,
			"custom_location_latitude": ["is", "set"],
		},
		fields=["name", "custom_location_captured_at", "creation", "assigned_to", "owner",
				"custom_location_address", "custom_location_latitude", "custom_location_longitude"],
		order_by="custom_location_captured_at desc",
	)
	out = []
	for t in rows:
		who = t.assigned_to or t.owner
		when = t.custom_location_captured_at or t.creation
		out.append({
			"task": t.name,
			"when": str(when),
			"date": format_datetime(when, "d MMM, h:mm a"),
			"owner": who,
			"rep": (who and frappe.db.get_value("User", who, "full_name")) or who,
			"address": t.custom_location_address or "",
			"lat": t.custom_location_latitude,
			"lng": t.custom_location_longitude,
		})
	return out


def _reverse_geocode(lat, lng):
	"""lat/lng -> formatted address, or None on any failure / no key. Never raises."""
	key = _api_key()
	if not key:
		return None
	try:
		r = requests.get(GEOCODE_URL, params={"latlng": f"{lat},{lng}", "key": key}, timeout=_TIMEOUT)
		results = (r.json() or {}).get("results") or []
		return results[0]["formatted_address"] if results else None
	except Exception:
		frappe.log_error("location: reverse-geocode failed")
		return None


@frappe.whitelist()
def reverse_geocode(lat, lng):
	"""Address for a coordinate — used by the capture confirmation modal before the record is
	saved (so it works pre-insert, with no record name yet)."""
	return {"address": _reverse_geocode(flt(lat), flt(lng))}


@frappe.whitelist()
def static_map(lat, lng):
	"""Key-safe proxy: stream the Google Static Maps PNG for a coordinate so the API key never
	reaches the browser. Auth-gated (whitelist requires a logged-in user)."""
	key = _api_key()
	lat, lng = flt(lat), flt(lng)
	if not (key and lat and lng):
		frappe.throw(_("No map available."))
	zoom = _settings().static_map_zoom or 16
	resp = requests.get(
		STATICMAP_URL,
		params={
			"center": f"{lat},{lng}",
			"zoom": zoom,
			"size": "600x300",
			"scale": 2,
			"markers": f"color:red|{lat},{lng}",
			"key": key,
		},
		timeout=_TIMEOUT,
	)
	frappe.response["type"] = "binary"
	frappe.response["filecontent"] = resp.content
	frappe.response["filename"] = "map.png"
	frappe.response["content_type"] = "image/png"


@frappe.whitelist()
def test_connection():
	"""Settings 'Check connection' button: reverse-geocode a fixed sample point and report the
	resolved address — proves the key, billing, and the Geocoding API are all live."""
	if not _api_key():
		frappe.throw(_("Set a Google Maps API key first, then save."))
	addr = _reverse_geocode(*_SAMPLE)
	if not addr:
		frappe.throw(
			_("Google returned no result. Check the key, that billing is enabled, and that the "
			  "Geocoding API is enabled for this key.")
		)
	return _("Connected. Sample lookup resolved to: {0}").format(addr)


def write_location(doc, lat, lng, address=None, accuracy=None):
	"""The SINGLE place coordinates are mapped onto a record's fields. Any doctype carrying the
	custom_location_* fields can be passed. Also mirrors a GeoJSON Point into custom_location_geo
	so Frappe's native Geolocation field renders a read-only Leaflet map in Desk (no key, no proxy)."""
	doc.custom_location_latitude = flt(lat)
	doc.custom_location_longitude = flt(lng)
	doc.custom_location_address = address or None
	doc.custom_location_accuracy_m = flt(accuracy) if accuracy else None
	doc.custom_location_captured_at = frappe.utils.now_datetime()
	if doc.meta.has_field("custom_location_geo"):
		doc.custom_location_geo = _geojson_point(lat, lng)
