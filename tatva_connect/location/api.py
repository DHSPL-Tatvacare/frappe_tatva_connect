"""Location guard + Google Maps Platform helpers (Geocoding + Static Maps), server-side.

Two layers, one brain:
  • Doctype-agnostic helpers — reverse_geocode a coordinate, static_map (key-safe image proxy),
    write_location (map coords onto a record incl. the native Geolocation GeoJSON Point).
  • The activity location guard — `location_guard_applies` is the door-first probe (pure visit_mode
    == In-Person AND the lead grain is location-tracked, grain-scoped via taxonomy.grain), while
    `location_required` adds the conditional `location_when` branch enforced at save. The activity
    writer (compute_activity) and the validate backstop (tasks.enforce_location) key off
    `location_required`; `set_or_check_anchor` owns the clinic-anchor + radius rule. (Phase B consolidated
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

from tatva_connect import automation
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


DEFAULT_RADIUS_M = 100

# How the clinic anchor was established (custom_clinic_source on CRM Lead).
ANCHOR_ADDRESS = "Doctor Address"
ANCHOR_GPS = "First GPS Capture"
ANCHOR_MANAGER = "Manager Re-anchor"


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
	if not automation.is_enabled("location"):
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
	"""Pure visit_mode gate for the door-first client probe: returns the allowed radius (metres)
	when the type is ALWAYS an in-person visit (visit_mode == In-Person) AND the lead grain is
	tracked, else None. Upfront-certain types only — the conditional branch lives in
	location_required (enforced at save, since the visit/phone choice isn't known at the door)."""
	if not (task_type and lead):
		return None
	if frappe.db.get_value("CRM Task Type", task_type, "visit_mode") != "In-Person":
		return None
	return is_location_tracked(lead)


def _match_condition(when, values):
	"""Parse a location_when condition against the submitted values; True if it matches.
	Supports '<field>==<value>' and '<field> in v1|v2|v3'. Empty/None condition => False.
	No business field or value is hardcoded — both come from config + the submitted form."""
	if not when:
		return False
	when = when.strip()
	if "==" in when:
		field, _, value = when.partition("==")
		return str(values.get(field.strip()) or "") == value.strip()
	if " in " in when:
		field, _, opts = when.partition(" in ")
		choices = [o.strip() for o in opts.split("|")]
		return str(values.get(field.strip()) or "") in choices
	return False


def location_required(task_type, lead, values):
	"""The conditional gate, called by the activity writer AND the validate backstop: returns the
	allowed radius (metres) when this activity must capture+guard location for THESE submitted
	values (type visit_mode == In-Person OR its location_when condition matches), else None.
	Reuses is_location_tracked for the radius — one brain for both the always-visit and the
	conditional-visit branches."""
	if not (task_type and lead):
		return None
	tt = frappe.db.get_value("CRM Task Type", task_type, ["visit_mode", "location_when"], as_dict=True)
	if not tt:
		return None
	if tt.visit_mode != "In-Person" and not _match_condition(tt.location_when, values or {}):
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


def _read_anchor(ld):
	"""The lead's current clinic anchor as a dict, or None if unset."""
	if ld.custom_clinic_latitude and ld.custom_clinic_longitude:
		return {
			"lat": flt(ld.custom_clinic_latitude),
			"lng": flt(ld.custom_clinic_longitude),
			"source": ld.get("custom_clinic_source") or "",
			"address": ld.get("custom_clinic_address") or "",
		}
	return None


def _write_anchor(ld, lat, lng, source, address=None):
	ld.custom_clinic_latitude = flt(lat)
	ld.custom_clinic_longitude = flt(lng)
	ld.custom_clinic_geo = _geojson_point(lat, lng)
	if ld.meta.has_field("custom_clinic_source"):
		ld.custom_clinic_source = source
	if address and ld.meta.has_field("custom_clinic_address"):
		ld.custom_clinic_address = address
	ld.save(ignore_permissions=True)


def _resolve_anchor_address(ld, anchor):
	"""The anchor's human address: the stored one, else reverse-geocode ONCE and cache it back onto
	custom_clinic_address (db_set, no modified bump) so later prechecks / Desk views don't re-hit Google."""
	if anchor.get("address"):
		return anchor["address"]
	addr = _reverse_geocode(anchor["lat"], anchor["lng"])
	if addr and ld.meta.has_field("custom_clinic_address"):
		ld.db_set("custom_clinic_address", addr, update_modified=False)
	return addr or ""


def ensure_anchor(ld, here_lat, here_lng):
	"""Resolve the lead's clinic anchor (hybrid). Priority: existing anchor → the doctor's precise
	registered address (geocoded) → the rep's first in-person GPS fix. Returns (anchor, created):
	`created` is True only when THIS call set the anchor from the rep's position (the first-capture
	case that always passes the radius check). Address/manager anchors are authoritative locations,
	never derived from where the rep currently stands."""
	existing = _read_anchor(ld)
	if existing:
		return existing, False
	addr = ld.get("custom_clinic_address") if ld.meta.has_field("custom_clinic_address") else None
	if addr:
		geo = geocode(addr)
		if geo:
			_write_anchor(ld, geo[0], geo[1], ANCHOR_ADDRESS, address=addr)
			return _read_anchor(ld), False  # address anchor: rep must still be within radius of it
	# First in-person capture establishes the anchor at the rep's position.
	_write_anchor(ld, here_lat, here_lng, ANCHOR_GPS)
	return _read_anchor(ld), True


def set_or_check_anchor(lead, lat, lng, accuracy, radius):
	"""Authoritative guard (called by the one writer, save_activity). Resolves the anchor (hybrid),
	then — unless this call just set it from the rep's first fix — BLOCKS the save when the rep is
	beyond radius + the reading's accuracy.

	Returns a dict the caller logs to the audit trail (one source of the haversine — no recompute):
	  {"distance_m", "allowed_m", "anchor_lat", "anchor_lng", "first"}
	`first` is True on the first-capture case (anchor just set from the rep's fix — nothing to compare)."""
	ld = frappe.get_doc("CRM Lead", lead)
	anchor, created = ensure_anchor(ld, lat, lng)
	if created:
		return {"distance_m": None, "allowed_m": None,
				"anchor_lat": flt(lat), "anchor_lng": flt(lng), "first": True}
	dist = haversine(anchor["lat"], anchor["lng"], lat, lng)
	allowed = radius + flt(accuracy)
	if dist > allowed:
		frappe.throw(
			_("Visit blocked — you are {0} m from the doctor's location (allowed {1} m).").format(
				int(round(dist)), int(round(allowed))
			),
			title=_("Out of range"),
		)
	return {"distance_m": int(round(dist)), "allowed_m": int(round(allowed)),
			"anchor_lat": anchor["lat"], "anchor_lng": anchor["lng"], "first": False}


def log_visit_audit(lead, task_type, verdict, lat=None, lng=None, distance_m=None, allowed_m=None,
					anchor_lat=None, anchor_lng=None, task=None):
	"""Insert ONE CRM Visit Audit row — the fraud/quality trail. Every completion attempt records its
	location verdict: Accepted (in-person, in range), Rejected (in-person, out of range / blocked), or
	Not Required (phone/office). Rejected rows persist even though no CRM Task is saved (the block is the
	point). System-written: ignore_permissions, rep = the acting user, captured at now."""
	frappe.get_doc({
		"doctype": "CRM Visit Audit",
		"lead": lead,
		"task_type": task_type,
		"task": task,
		"rep": frappe.session.user,
		"captured_at": frappe.utils.now_datetime(),
		"verdict": verdict,
		"latitude": flt(lat) if lat is not None else None,
		"longitude": flt(lng) if lng is not None else None,
		"distance_m": cint(distance_m) if distance_m is not None else None,
		"allowed_m": cint(allowed_m) if allowed_m is not None else None,
		"anchor_latitude": flt(anchor_lat) if anchor_lat is not None else None,
		"anchor_longitude": flt(anchor_lng) if anchor_lng is not None else None,
	}).insert(ignore_permissions=True)


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
def location_needed(lead, task_type, values=None):
	"""Does this activity need a location for THESE submitted values? (In-Person OR a location_when
	branch that matches, on a tracked grain.) The client's one probe before capturing GPS."""
	if isinstance(values, str):
		values = frappe.parse_json(values) or {}
	return location_required(task_type, lead, values or {}) is not None


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


def geocode(address):
	"""address -> (lat, lng), or None on any failure / no key. Never raises. Used to anchor a
	clinic on the doctor's precise registered address (the hybrid anchor's first choice)."""
	key = _api_key()
	if not (key and address):
		return None
	try:
		r = requests.get(GEOCODE_URL, params={"address": address, "key": key}, timeout=_TIMEOUT)
		results = (r.json() or {}).get("results") or []
		if not results:
			return None
		loc = results[0]["geometry"]["location"]
		return flt(loc["lat"]), flt(loc["lng"])
	except Exception:
		frappe.log_error("location: forward-geocode failed")
		return None


@frappe.whitelist()
def reverse_geocode(lat, lng):
	"""Address for a coordinate — used by the capture confirmation modal before the record is
	saved (so it works pre-insert, with no record name yet)."""
	return {"address": _reverse_geocode(flt(lat), flt(lng))}


@frappe.whitelist()
def geocode_search(query):
	"""Search an address -> up to 5 candidate {address, lat, lng} via the Geocoding API (key stays
	server-side). Powers the Desk 'Set Clinic Location' picker — the operator types, picks a match,
	and we anchor on its resolved coordinates."""
	key = _api_key()
	if not (key and query):
		return []
	try:
		r = requests.get(GEOCODE_URL, params={"address": query, "key": key, "components": "country:in"},
						 timeout=_TIMEOUT)
		out = []
		for res in (r.json() or {}).get("results", [])[:5]:
			loc = res["geometry"]["location"]
			out.append({"address": res["formatted_address"], "lat": flt(loc["lat"]), "lng": flt(loc["lng"])})
		return out
	except Exception:
		frappe.log_error("location: geocode_search failed")
		return []


PLACES_AUTOCOMPLETE_URL = "https://places.googleapis.com/v1/places:autocomplete"
PLACES_DETAILS_URL = "https://places.googleapis.com/v1/places/"


@frappe.whitelist()
def place_autocomplete(query):
	"""Places API (New) autocomplete proxy (key server-side). Returns [{place_id, description}] for the
	Desk 'Set Clinic Location' type-ahead. India-biased."""
	key = _api_key()
	if not (key and query):
		return []
	try:
		r = requests.post(
			PLACES_AUTOCOMPLETE_URL,
			headers={"Content-Type": "application/json", "X-Goog-Api-Key": key},
			json={"input": query, "includedRegionCodes": ["in"]},
			timeout=_TIMEOUT,
		)
		out = []
		for s in (r.json() or {}).get("suggestions", []):
			pp = s.get("placePrediction") or {}
			if pp.get("placeId"):
				out.append({"place_id": pp["placeId"], "description": (pp.get("text") or {}).get("text", "")})
		return out
	except Exception:
		frappe.log_error("location: place_autocomplete failed")
		return []


@frappe.whitelist()
def place_details(place_id):
	"""Places API (New) details proxy: a placeId -> {lat, lng, address}. Key server-side."""
	key = _api_key()
	if not (key and place_id):
		return None
	try:
		r = requests.get(
			PLACES_DETAILS_URL + place_id,
			headers={"X-Goog-Api-Key": key, "X-Goog-FieldMask": "location,formattedAddress"},
			timeout=_TIMEOUT,
		)
		j = r.json() or {}
		loc = j.get("location") or {}
		if not loc:
			return None
		return {"lat": flt(loc.get("latitude")), "lng": flt(loc.get("longitude")),
				"address": j.get("formattedAddress", "")}
	except Exception:
		frappe.log_error("location: place_details failed")
		return None


@frappe.whitelist()
def set_clinic_location(lead, lat, lng, address=None):
	"""Set/MOVE a lead's clinic anchor from a searched+resolved address (source = Doctor Address).
	Overwrites any existing anchor — this is how ops corrects or relocates the geofence; the guard
	then measures every visit against this point. Requires write permission on the lead."""
	if not frappe.has_permission("CRM Lead", "write", doc=lead):
		frappe.throw(_("Not permitted to set this lead's clinic location."), frappe.PermissionError)
	ld = frappe.get_doc("CRM Lead", lead)
	_write_anchor(ld, flt(lat), flt(lng), ANCHOR_ADDRESS, address=address)
	return {"lat": flt(lat), "lng": flt(lng), "address": address or "", "source": ANCHOR_ADDRESS}


@frappe.whitelist()
def leads_near(lat, lng, radius_km=15):
	"""Doctor leads whose clinic anchor is within radius_km of (lat,lng), nearest first. The Near Me
	map source. Permission-scoped — frappe.get_list applies the user's read scope, so a rep sees only
	their own leads. A bounding-box SQL prefilter (indexed on the clinic lat/lng) narrows the set, then
	haversine refines to a true circle. No grain hardcoded; works for every product's leads."""
	lat, lng, radius_km = flt(lat), flt(lng), flt(radius_km) or 15.0
	dlat = radius_km / 111.0
	dlng = radius_km / (111.0 * max(0.15, math.cos(math.radians(lat))))
	rows = frappe.get_list(
		"CRM Lead",
		filters={
			"custom_clinic_latitude": ["between", [lat - dlat, lat + dlat]],
			"custom_clinic_longitude": ["between", [lng - dlng, lng + dlng]],
		},
		fields=["name", "lead_name", "mobile_no", "custom_clinic_latitude", "custom_clinic_longitude",
				"custom_clinic_address", "custom_stage", "status"],
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
				"lat": r.custom_clinic_latitude,
				"lng": r.custom_clinic_longitude,
				"address": r.custom_clinic_address or "",
				"stage": r.custom_stage or r.status or "",
				"distance_m": int(round(d)),
			})
	out.sort(key=lambda x: x["distance_m"])
	return out


@frappe.whitelist()
def precheck(lead, task_type, lat, lng, accuracy=None, values=None, task=None):
	"""The ONE location check the client calls on submit: for THESE values, does this activity need a
	location, and is the rep in range of the doctor's clinic anchor? An out-of-range result is AUDITED
	here (one place) as a Rejected attempt — the only way a blocked try (no task saved) reaches the
	trail. `task` (when completing an existing assigned task) is stamped onto that Rejected row so the
	block is tied to the exact task. Resolves/lazily sets an ADDRESS anchor (authoritative) but never a
	first-GPS anchor (that commits on a real save). compute_activity re-checks on save — UX + audit,
	not the boundary.

	Returns:
	  {"needed": False}                                          # no location for this submission
	  {"needed": True, "ok": True,  "first": True}               # first capture — nothing to compare
	  {"needed": True, "ok": True/False, "distance_m", "allowed_m", "anchor_lat/lng", "anchor_address"}
	"""
	if isinstance(values, str):
		values = frappe.parse_json(values) or {}
	radius = location_required(task_type, lead, values or {})
	if radius is None:
		return {"needed": False}
	lat, lng, accuracy = flt(lat), flt(lng), flt(accuracy)
	ld = frappe.get_doc("CRM Lead", lead)
	anchor = _read_anchor(ld)
	if not anchor:
		addr = ld.get("custom_clinic_address") if ld.meta.has_field("custom_clinic_address") else None
		geo = geocode(addr) if addr else None
		if geo:
			_write_anchor(ld, geo[0], geo[1], ANCHOR_ADDRESS, address=addr)
			anchor = _read_anchor(ld)
		else:
			return {"needed": True, "ok": True, "first": True, "allowed_m": radius}
	dist = int(round(haversine(anchor["lat"], anchor["lng"], lat, lng)))
	allowed = int(round(radius + accuracy))
	ok = dist <= allowed
	if not ok:
		log_visit_audit(lead, task_type, "Rejected", lat=lat, lng=lng, distance_m=dist,
						allowed_m=allowed, anchor_lat=anchor["lat"], anchor_lng=anchor["lng"], task=task)
	return {
		"needed": True, "ok": ok, "distance_m": dist, "allowed_m": allowed,
		"anchor_lat": anchor["lat"], "anchor_lng": anchor["lng"],
		"anchor_address": _resolve_anchor_address(ld, anchor),
	}


@frappe.whitelist()
def lead_location_view(lead):
	"""The unified Desk 'Activity & Location' projection: the clinic anchor (source of truth) + EVERY
	completed activity on the lead (newest first). In-person captures carry a map + distance from the
	anchor; phone/office activities show as plain log lines (NOT fake (0,0) coordinates — `located` is
	keyed off custom_location_captured_at, which is set only on a real capture). One brain; map images
	stream through the key-safe static_map proxy."""
	from tatva_connect.activity.api import _activity_type_names

	ld = frappe.get_doc("CRM Lead", lead)
	anchor = _read_anchor(ld)
	anchor_out = None
	if anchor:
		anchor_out = {
			"lat": anchor["lat"],
			"lng": anchor["lng"],
			"address": _resolve_anchor_address(ld, anchor),
			"source": anchor["source"] or ANCHOR_GPS,
		}
	activities = []
	types = _activity_type_names()
	if types:
		rows = frappe.get_all(
			"CRM Task",
			filters={"reference_docname": lead, "custom_task_type": ["in", list(types)], "status": "Done"},
			fields=["name", "custom_task_type", "custom_visit_status", "status", "modified", "creation",
					"assigned_to", "owner", "custom_location_address", "custom_location_latitude",
					"custom_location_longitude", "custom_location_captured_at"],
			order_by="modified desc",
		)
		for t in rows:
			who = t.assigned_to or t.owner
			located = bool(t.custom_location_captured_at)  # the reliable marker — phone activities have none
			when = t.custom_location_captured_at or t.modified or t.creation
			dist = None
			if located and anchor:
				dist = int(round(haversine(
					anchor["lat"], anchor["lng"], t.custom_location_latitude, t.custom_location_longitude)))
			activities.append({
				"task": t.name,
				"type": t.custom_task_type or "",
				"status": t.custom_visit_status or t.status or "",
				"rep": (who and frappe.db.get_value("User", who, "full_name")) or who or "",
				"date": format_datetime(when, "d MMM, h:mm a"),
				"located": located,
				"address": (t.custom_location_address or "") if located else "",
				"lat": t.custom_location_latitude if located else None,
				"lng": t.custom_location_longitude if located else None,
				"distance_m": dist,
			})
	# Blocked attempts — Rejected CRM Visit Audit rows (no CRM Task exists for these). The Desk view
	# surfaces them as the fraud/quality trail below the activities.
	rejections = []
	for a in frappe.get_all(
		"CRM Visit Audit",
		filters={"lead": lead, "verdict": "Rejected"},
		fields=["rep", "captured_at", "distance_m", "allowed_m", "latitude", "longitude"],
		order_by="captured_at desc",
		limit_page_length=20,
	):
		rejections.append({
			"rep": (a.rep and frappe.db.get_value("User", a.rep, "full_name")) or a.rep or "",
			"date": format_datetime(a.captured_at, "d MMM, h:mm a"),
			"distance_m": a.distance_m,
			"allowed_m": a.allowed_m,
			"lat": a.latitude,
			"lng": a.longitude,
		})
	return {"anchor": anchor_out, "activities": activities, "rejections": rejections}


DEFAULT_OSM_TILES = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"


def _resolve_provider(pref, default, google_ok):
	"""Effective display provider for one map surface: the operator's choice (or the code default when
	the field is blank — no baked config value), downgraded to OSM whenever Google isn't usable (Maps
	disabled / no key) so a Google choice can never render a broken image. Returns 'osm' or 'google'."""
	p = (pref or default or "").strip().lower()
	p = "google" if p.startswith("google") else "osm"
	return "google" if (p == "google" and google_ok) else "osm"


@frappe.whitelist()
def map_config():
	"""The in-app map display config for the SPA: which provider renders each surface, already resolved
	for availability. ONE source so the card thumbnail, the detail-modal map, and the block/receipt
	dialog maps all obey the same operator switches (CRM Maps Settings → Map Display). Blank field =
	code default (thumbnails OSM, dialogs Google). Geocoding + Desk history stay Google regardless."""
	s = _settings()
	google_ok = bool(automation.is_enabled("location") and _api_key())
	return {
		"thumbnail": _resolve_provider(s.get("thumbnail_map_provider"), "osm", google_ok),
		"dialog": _resolve_provider(s.get("dialog_map_provider"), "google", google_ok),
		"zoom": cint(s.static_map_zoom) or 16,
		"tile_url": (s.get("osm_tile_url") or "").strip() or DEFAULT_OSM_TILES,
		"google_available": google_ok,
	}


@frappe.whitelist()
def static_map(lat, lng, here_lat=None, here_lng=None):
	"""Key-safe proxy: stream the Google Static Maps PNG so the API key never reaches the browser.
	One marker (a single captured spot) by default. If `here_lat/here_lng` are given, render TWO
	markers — the clinic (red) and 'You' (blue) — auto-fitted, for the out-of-range block dialog."""
	key = _api_key()
	lat, lng = flt(lat), flt(lng)
	if not (key and lat and lng):
		frappe.throw(_("No map available."))
	params = {"size": "600x300", "scale": 2, "key": key}
	if here_lat and here_lng:
		params["markers"] = [
			f"color:red|label:C|{lat},{lng}",
			f"color:blue|label:U|{flt(here_lat)},{flt(here_lng)}",
		]  # two points, no center/zoom -> Google auto-fits both
	else:
		params["center"] = f"{lat},{lng}"
		params["zoom"] = _settings().static_map_zoom or 16
		params["markers"] = f"color:red|{lat},{lng}"
	resp = requests.get(STATICMAP_URL, params=params, timeout=_TIMEOUT)
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


def location_fields(lat, lng, address=None, accuracy=None):
	"""The SINGLE mapping of a captured fix -> CRM Task custom_location_* field values (as a dict),
	incl. the GeoJSON Point for the native Geolocation field. Used by compute_activity (stamped onto
	the doc by either save path)."""
	return {
		"custom_location_latitude": flt(lat),
		"custom_location_longitude": flt(lng),
		"custom_location_address": address or None,
		"custom_location_accuracy_m": flt(accuracy) if accuracy else None,
		"custom_location_captured_at": frappe.utils.now_datetime(),
		"custom_location_geo": _geojson_point(lat, lng),
	}


def write_location(doc, lat, lng, address=None, accuracy=None):
	"""Map a captured fix onto a record's custom_location_* fields (the dict from location_fields).
	Kept for any caller holding a doc; compute_activity uses location_fields directly."""
	for k, v in location_fields(lat, lng, address=address, accuracy=accuracy).items():
		if k != "custom_location_geo" or doc.meta.has_field("custom_location_geo"):
			doc.set(k, v)
