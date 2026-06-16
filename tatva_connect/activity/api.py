"""The activity engine's server brain.

An "activity" is a CRM Task of an activity type (a CRM Task Type that carries a grain
scope), logged complete on save. One writer (`save_activity`) splits the submitted form
into first-class columns + a JSON payload. One projection (`lead_timeline`) feeds BOTH
the SPA Activity timeline and the Desk Lead timeline. Availability is grain-scoped through
the single brain `taxonomy.grain.resolve_scoped` — nothing here hardcodes a type or scope.
Ships dormant: a CRM Task Type with no scope row never surfaces as an activity.
"""
import json

import frappe
from frappe import _
from frappe.utils import format_datetime, formatdate

from tatva_connect.taxonomy.grain import resolve_scoped

# The only CRM Task columns an activity field may write to (first_class_target).
# Anything else in the schema goes to the JSON payload.
FIRST_CLASS = ("custom_visit_status", "custom_next_visit_at", "custom_asm", "custom_demo_scheduled_at")


def _lead_axes(lead):
	v = frappe.db.get_value(
		"CRM Lead", lead, ["custom_vertical", "custom_group", "custom_current_program"], as_dict=True
	)
	if not v:
		frappe.throw(_("Lead {0} not found").format(lead))
	return (v.custom_vertical or ""), (v.custom_group or ""), (v.custom_current_program or "")


def _scope_rows(task_type):
	return frappe.get_all(
		"CRM Task Type Scope",
		filters={"parent": task_type, "parenttype": "CRM Task Type"},
		fields=["vertical", "group", "program"],
	)


def _scope_applies(task_type, vertical, group, program):
	"""True if this activity type is available to the lead's grain. Same brain as the
	checklist resolver; an ambiguous (redundant) scope still counts as available."""
	rows = _scope_rows(task_type)
	if not rows:
		return False
	try:
		return resolve_scoped(rows, vertical, group, program) is not None
	except frappe.ValidationError:
		return True


def _activity_type_names():
	"""Every CRM Task Type configured as an activity (has at least one scope row)."""
	return {r.parent for r in frappe.get_all(
		"CRM Task Type Scope", filters={"parenttype": "CRM Task Type"}, fields=["parent"], distinct=True
	)}


def _type_has_schema(task_type):
	"""True if the type carries a form schema (≥1 field) — i.e. completing it must log details."""
	return bool(task_type and frappe.db.exists(
		"CRM Task Type Field", {"parent": task_type, "parenttype": "CRM Task Type"}
	))


def activity_is_unlogged(doc):
	"""True if this is a form-activity task being marked Done with NO details captured. The single
	definition of 'an activity completed empty' — used by the validate backstop (one brain) so the
	rule holds on every save path, not just the Form-view controller."""
	if (doc.status or "") != "Done":
		return False
	if not _type_has_schema(doc.custom_task_type):
		return False
	# save_activity always writes a payload string (even "{}") + the first-class cols; a raw
	# set_value(status=Done) leaves the payload untouched (None/blank). So a present payload OR
	# any first-class value means the form was submitted -> logged.
	if (doc.custom_activity_payload or "").strip():
		return False
	return not any(doc.get(f) for f in FIRST_CLASS)


@frappe.whitelist()
def open_activity_tasks(lead):
	"""Open (not Done/Canceled) activity tasks on a lead — lets the client map a Tasks-tab row to
	the activity type/name so completing it opens the activity form instead of an empty status flip."""
	types = _activity_type_names()
	if not types:
		return []
	return frappe.get_all(
		"CRM Task",
		filters={
			"reference_docname": lead,
			"status": ["not in", ["Done", "Canceled"]],
			"custom_task_type": ["in", list(types)],
		},
		fields=["name", "title", "custom_task_type"],
		order_by="creation desc",
	)


@frappe.whitelist()
def list_types_for_lead(lead):
	"""Activity types available to this lead's grain — the searchable picker source. ONE indexed
	query (no per-type N+1): a type is available if ANY of its scope rows matches the grain, where a
	blank axis is a wildcard. (resolve_scoped's most-specific tie-break is only for radius/checklist;
	availability just needs a match.) Flat regardless of catalog size."""
	vertical, group, program = _lead_axes(lead)
	rows = frappe.db.sql(
		"""
		SELECT DISTINCT tt.name, tt.is_logged_complete, tt.visit_mode
		FROM `tabCRM Task Type` tt
		JOIN `tabCRM Task Type Scope` sc
		  ON sc.parent = tt.name AND sc.parenttype = 'CRM Task Type'
		WHERE (sc.vertical = '' OR sc.vertical IS NULL OR sc.vertical = %(v)s)
		  AND (sc.`group`  = '' OR sc.`group`  IS NULL OR sc.`group`  = %(g)s)
		  AND (sc.program  = '' OR sc.program  IS NULL OR sc.program  = %(p)s)
		ORDER BY tt.name
		""",
		{"v": vertical, "g": group, "p": program},
		as_dict=True,
	)
	return [
		{"name": r.name, "label": r.name,
		 "is_logged_complete": int(r.is_logged_complete or 0), "visit_mode": r.visit_mode or ""}
		for r in rows
	]


@frappe.whitelist()
def get_schema(task_type):
	"""The activity type's per-field schema, in order — for the client form."""
	doc = frappe.get_doc("CRM Task Type", task_type)
	return [
		{
			"label": f.label,
			"fieldname": f.fieldname,
			"fieldtype": f.fieldtype,
			"options": f.options or "",
			"reqd": int(f.reqd or 0),
			"first_class_target": f.first_class_target or "",
			"depends_on": (f.get("depends_on") or ""),
		}
		for f in doc.schema
	]


@frappe.whitelist()
def get_type_meta(task_type):
	"""The type's location config (visit_mode + location_when) — lets the client decide whether a
	conditional activity needs a location capture once the rep picks the branch field. Sibling of
	get_schema so get_schema's shape stays stable for existing callers."""
	tt = frappe.db.get_value("CRM Task Type", task_type, ["visit_mode", "location_when"], as_dict=True) or {}
	return {"visit_mode": tt.get("visit_mode") or "", "location_when": tt.get("location_when") or ""}


def compute_activity(lead, task_type, values):
	"""The ONE brain that turns a submitted activity form into CRM Task field values: validates
	grain + required, splits first-class columns vs the JSON payload, and runs the location guard
	(set/check the clinic anchor, resolve the address). Returns a flat dict of CRM Task fieldname ->
	value (status, payload, first-class cols, location cols). Raises on out-of-scope / missing / out
	of range. Used by BOTH save paths: save_activity (complete/update existing) and the native
	new-task create (the form script stamps these onto the doc before insert). No second writer."""
	if isinstance(values, str):
		values = frappe.parse_json(values) or {}
	vertical, group, program = _lead_axes(lead)
	if not _scope_applies(task_type, vertical, group, program):
		frappe.throw(_("This activity is not available for this lead."), title=_("Out of scope"))

	tt = frappe.get_doc("CRM Task Type", task_type)
	first_class, payload = {}, {}
	for f in tt.schema:
		val = values.get(f.fieldname)
		if f.reqd and (val is None or val == ""):
			frappe.throw(_("{0} is required.").format(f.label), title=_("Missing field"))
		target = f.first_class_target or ""
		(first_class if target in FIRST_CLASS else payload)[target or f.fieldname] = val

	fields = {
		"custom_activity_payload": json.dumps(payload, default=str),
		"status": "Done" if int(tt.is_logged_complete or 0) else "Todo",
		**first_class,
	}
	notes = values.get("notes")
	if notes and frappe.get_meta("CRM Task").has_field("description"):
		fields["description"] = notes

	# Location guard: in-person activity on a tracked grain must carry an in-range fix. The gate +
	# anchor/radius rule live once in location.api (one brain); this just feeds them.
	from tatva_connect.location.api import (
		location_required, set_or_check_anchor, location_fields, _reverse_geocode)

	radius = location_required(task_type, lead, values)
	if radius is not None:
		lat, lng, accuracy = values.get("lat"), values.get("lng"), values.get("accuracy")
		if not (lat and lng):
			frappe.throw(_("A captured location is required for this in-person activity."),
						 title=_("Location required"))
		set_or_check_anchor(lead, lat, lng, accuracy, radius)  # throws if out of range
		fields.update(location_fields(lat, lng, address=_reverse_geocode(lat, lng), accuracy=accuracy))
	return fields


@frappe.whitelist()
def compute_activity_fields(lead, task_type, values):
	"""Whitelisted compute for the native new-task path: the CRM Task form script stamps the result
	onto the doc, then lets the native create save it ONCE (no double insert, no lingering popup)."""
	return compute_activity(lead, task_type, values)


@frappe.whitelist()
def save_activity(lead, task_type, values, task=None):
	"""THE one writer for completing/updating an activity (assigned-task completion, list/modal
	paths). Computes the field values (compute_activity) then writes them onto an existing task, or
	inserts a fresh one. Returns the task name."""
	fields = compute_activity(lead, task_type, values)
	if task:
		doc = frappe.get_doc("CRM Task", task)
		doc.update(fields)
		doc.save()
	else:
		doc = frappe.get_doc({
			"doctype": "CRM Task",
			"title": task_type,
			"custom_task_type": task_type,
			"assigned_to": frappe.session.user,
			"reference_doctype": "CRM Lead",
			"reference_docname": lead,
			**fields,
		})
		doc.insert()
	return doc.name


@frappe.whitelist()
def lead_timeline(lead):
	"""The single activity projection for a lead — used by BOTH the SPA timeline and the
	Desk Activity Timeline section. Newest first; one entry per activity task."""
	activity_types = _activity_type_names()
	if not activity_types:
		return []
	tasks = frappe.get_all(
		"CRM Task",
		filters={"reference_docname": lead, "custom_task_type": ["in", list(activity_types)]},
		fields=["name", "creation", "modified", "status", "custom_task_type",
				"custom_visit_status", "custom_location_address", "assigned_to", "owner"],
		order_by="creation desc",
	)
	out = []
	for t in tasks:
		who = t.assigned_to or t.owner
		out.append({
			"name": t.name,
			"creation": str(t.creation),
			"date": formatdate(t.creation, "d MMM"),
			"datetime": format_datetime(t.creation, "d MMM, h:mm a"),
			"owner": who,
			"owner_name": (who and frappe.db.get_value("User", who, "full_name")) or who,
			"activity_type": t.custom_task_type,
			"status": t.custom_visit_status or t.status,
			"address": t.custom_location_address or "",
		})
	return out
