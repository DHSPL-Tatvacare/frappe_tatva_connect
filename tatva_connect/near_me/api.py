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
from frappe.utils import flt, get_datetime, getdate

from tatva_connect import automation
from tatva_connect.access import team
from tatva_connect.api.list_link_titles import titles_for
from tatva_connect.location.api import NEAR_MAX_ROWS, haversine, leads_within_radius
from tatva_connect.location.route_order import resolve as route_order
from tatva_connect.tasks.tasks import DONE_STATUS, completed_on, open_statuses
from tatva_connect.whatsapp.phone import lookup_e164

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


# `first_name` is the avatar's letter source (`_avatar_label` drops a Salutation typed into it); `lead_name` would draw "D" on every doctor.
_FIELDS = ["name", "lead_name", "first_name", "mobile_no", "image", "source",
		   "custom_clinic_latitude", "custom_clinic_longitude", "custom_clinic_address",
		   "custom_stage", "status", "custom_vertical", "custom_clinic_or_hospital_name",
		   "custom_address_line_1", "custom_address_line_2", "custom_city", "custom_pincode",
		   "custom_doctor_speciality"]


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


def _avatar_label(r, honorifics):
	"""The avatar's letter source: first_name without a salutation typed into it ("Dr. ABN" -> "ABN"), the honorifics read off Frappe's Salutation master."""
	name = (r.first_name or r.lead_name or r.name).strip()
	head, _sep, rest = name.partition(" ")
	return rest.strip() if rest.strip() and head.rstrip(".").lower() in honorifics else name


def _stage(r):
	"""The stage key and the doctype it links to: the grain stage when set, else the native status. The target is read off the schema, never typed."""
	field = "custom_stage" if r.custom_stage else "status"
	return r.get(field) or "", frappe.get_meta("CRM Lead").get_field(field).options


def _link_titles(near):
	"""`{doctype}::{pk}` -> title for every stage shown — the list transport (`titles_for`), so a composite `::` key never reaches the screen."""
	wanted = {}
	for r, _dist in near:
		value, target = _stage(r)
		if value:
			wanted.setdefault(target, set()).add(value)
	return {f"{dt}::{v}": t for dt, values in wanted.items() for v, t in titles_for(dt, values).items()}


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
	honorifics = _honorifics()
	return {
		# The MODE, said plainly. A ring and a name answer different questions, and the panel must not infer which from a radius of zero — that renders as "within 0 km".
		"scope": "search" if (q or "").strip() else "ring",
		"radius_km": used,
		"capped": capped,
		"_link_titles": _link_titles(near),
		"doctors": [_row(r, dist, honorifics) for r, dist in near],
	}


def _honorifics():
	return {s.lower() for s in frappe.get_all("Salutation", pluck="name")}


def _row(r, dist, honorifics):
	"""ONE registry row, the shape every Near Me list and the map read — the territory, the search and My day."""
	stage, stage_doctype = _stage(r)
	return {
		"name": r.name,
		"title": r.lead_name or r.name,
		"image_label": _avatar_label(r, honorifics),
		"mobile_no": r.mobile_no or "",
		# The stored form (`to_e164`), so a WhatsApp link never guesses a country; "" when the number has none.
		"mobile_e164": lookup_e164(r.mobile_no) if r.mobile_no else "",
		"image": r.image or "",
		# An unpinned clinic stores 0, not NULL; it goes out as no location, so no pin or directions land in the sea.
		"lat": r.custom_clinic_latitude or None,
		"lng": r.custom_clinic_longitude or None,
		# The anchor's own text when a pin carried one, else the lead's postal address — the one a rep keyed in.
		"address": r.custom_clinic_address or ", ".join(
			p for p in (r.custom_address_line_1, r.custom_address_line_2, r.custom_city, r.custom_pincode) if p
		),
		"clinic": r.custom_clinic_or_hospital_name or "",
		"speciality": (r.custom_doctor_speciality or "").strip(),
		# The key, kept for filtering; the client shows its title through `linkTitle.js`.
		"stage": stage,
		"stage_doctype": stage_doctype,
		"source": r.source or "",
		"grain": r.custom_vertical or "",  # business-line label (display only, never a filter)
		"distance_m": dist,
	}


@frappe.whitelist()
def my_team():
	"""The reps whose day a manager may look at — their Sales Hierarchy team (everyone, outside it) who can open Near Me; empty for anyone else."""
	_assert_access()
	viewer = frappe.session.user
	if not team.is_manager(viewer):
		return []
	members = team.team_of(viewer)
	users = frappe.get_all(  # authz-ok: tier-b — a manager's own team, gated by team.is_manager above
		"Has Role", filters={"role": ROLE, "parenttype": "User"}, pluck="parent", distinct=True
	)
	users = [u for u in users if u != viewer and (members is None or u in members)]
	if not users:
		return []
	return frappe.get_all(  # authz-ok: tier-b — names of the users listed above, nothing more
		"User", filters={"name": ["in", users], "enabled": 1}, fields=["name", "full_name"], order_by="full_name asc"
	)


@frappe.whitelist()
def my_day(lat, lng, date=None, user=None):
	"""Tasks due on `date` (default today) for `user` (default me): doctors still to visit as stops in visiting order from (lat,lng), leads with no clinic pin apart, and doctors whose tasks are all completed, in the order they were completed. Past days are not planned; another rep's day needs `team.may_view`."""
	_assert_access()
	viewer = frappe.session.user
	user = user or viewer
	if not team.may_view(viewer, user):
		frappe.throw(_("You can see only your own day, or your team's."), frappe.PermissionError)
	today = getdate()
	day = getdate(date) if date else today
	if day < today:
		frappe.throw(_("Pick today or a later day."))
	# Another rep's tasks are read for a viewer `may_view` already admitted; their own are read under their permissions.
	query = frappe.get_list if user == viewer else frappe.get_all
	tasks = query(  # authz-ok: tier-b — get_all only after team.may_view admitted this viewer to this rep
		"CRM Task",
		filters={
			"assigned_to": user,
			"status": ["in", [*open_statuses(), DONE_STATUS]],
			"due_date": ["between", [day, day]],
			"reference_doctype": "CRM Lead",
		},
		fields=["name", "title", "custom_task_type", "due_date", "reference_docname", "status", "custom_completed_on", "modified"],
		order_by="due_date asc",
		limit_page_length=0,
	)
	by_lead = {}
	for t in tasks:
		by_lead.setdefault(t.reference_docname, []).append(t)
	# A doctor is completed once every task of theirs that day is Done, at the moment the last one closed.
	completed_at = {
		lead: max(get_datetime(completed_on(t)) for t in day_tasks)
		for lead, day_tasks in by_lead.items()
		if all(t.status == DONE_STATUS for t in day_tasks)
	}
	leads = frappe.get_all(  # authz-ok: tier-b — _assert_access owns the boundary, exactly as the ring query does
		"CRM Lead", filters={"name": ["in", list(by_lead)]}, fields=_FIELDS
	) if by_lead else []
	to_visit = [r for r in leads if r.name not in completed_at]
	here = {"lat": flt(lat), "lng": flt(lng)}
	# An unpinned clinic stores 0 in a Float, not NULL, and (0, 0) is open sea — so a zero is "no location".
	located = [{"id": r.name, "lat": r.custom_clinic_latitude, "lng": r.custom_clinic_longitude, "lead": r}
			   for r in to_visit if r.custom_clinic_latitude and r.custom_clinic_longitude]
	# A look at someone else's day never makes or pays for their plan: it reads theirs, or previews it free.
	ordered = route_order.order(here, located, scope=f"{user}:{day}", peek=user != viewer)
	honorifics = _honorifics()

	def task_cards(lead, done):
		return [{"name": t.name, "title": t.title, "type": t.custom_task_type}
				for t in by_lead[lead] if (t.status == DONE_STATUS) == done]

	def stop(r, dist):
		return {**_row(r, dist, honorifics), "tasks": task_cards(r.name, done=False)}

	stops, previous = [], here
	for s in ordered.stops:
		# Each stop says how far it is from the one before it — the leg the rep is about to drive.
		stops.append(stop(s["lead"], round(haversine(previous["lat"], previous["lng"], s["lat"], s["lng"]))))
		previous = s
	pinned = {s["lead"].name for s in located}
	return {
		"date": str(day),
		"user": user,
		# Start route needs both: the plan starts from where the viewer stands only on their own day, today.
		"is_mine": user == viewer,
		"is_today": day == today,
		"provider": ordered.provider,
		"by_road": ordered.by_road,
		"distance_m": ordered.distance_m,
		"polyline": ordered.polyline,
		"stops": stops,
		"unlocated": [stop(r, None) for r in to_visit if r.name not in pinned],
		"completed": [
			{**_row(r, None, honorifics), "tasks": task_cards(r.name, done=True), "completed_at": str(completed_at[r.name])}
			for r in sorted((r for r in leads if r.name in completed_at), key=lambda r: completed_at[r.name])
		],
		"_link_titles": _link_titles([(r, None) for r in leads]),
	}
