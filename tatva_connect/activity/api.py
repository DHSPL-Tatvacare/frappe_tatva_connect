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
	"""Activity types available to this lead's grain — the searchable picker source."""
	vertical, group, program = _lead_axes(lead)
	out = []
	for tt in frappe.get_all("CRM Task Type", fields=["name", "is_logged_complete", "visit_mode"]):
		if _scope_applies(tt.name, vertical, group, program):
			out.append({
				"name": tt.name,
				"label": tt.name,
				"is_logged_complete": int(tt.is_logged_complete or 0),
				"visit_mode": tt.visit_mode or "",
			})
	out.sort(key=lambda t: t["label"].lower())
	return out


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
		}
		for f in doc.schema
	]


@frappe.whitelist()
def save_activity(lead, task_type, values, task=None):
	"""THE one writer. Validate required + grain, split values into first-class columns
	vs the JSON payload, then create (ad-hoc log) or complete (assigned task) the CRM Task.
	Returns the task name."""
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
		if target in FIRST_CLASS:
			first_class[target] = val
		else:
			payload[f.fieldname] = val

	# Location guard (Phase B): in-person activity on a tracked grain must capture + pass a fix.
	# One brain — the gate + anchor/radius rule live in location.api; this just feeds them.
	from tatva_connect.location.api import location_guard_applies, set_or_check_anchor, write_location, _reverse_geocode

	radius = location_guard_applies(task_type, lead)
	fix = None
	if radius is not None:
		lat, lng, accuracy = values.get("lat"), values.get("lng"), values.get("accuracy")
		if not (lat and lng):
			frappe.throw(_("A captured location is required for this in-person activity."),
						 title=_("Location required"))
		set_or_check_anchor(lead, lat, lng, accuracy, radius)
		fix = (lat, lng, accuracy, _reverse_geocode(lat, lng))

	is_done = int(tt.is_logged_complete or 0)
	fields = {
		"custom_activity_payload": json.dumps(payload, default=str),
		"status": "Done" if is_done else "Todo",
		**first_class,
	}

	if task:
		doc = frappe.get_doc("CRM Task", task)
		doc.update(fields)
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
	if fix:
		write_location(doc, fix[0], fix[1], address=fix[3], accuracy=fix[2])
	doc.save() if task else doc.insert()
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
