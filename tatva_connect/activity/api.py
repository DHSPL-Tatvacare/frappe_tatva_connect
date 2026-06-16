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
from frappe.utils import flt, format_datetime, formatdate

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


def _validate_asm(asm):
	"""An ASM stamped onto an activity must hold the 'Sales Manager' role — keeps the audited ASM data
	clean to actual Sales Managers. No-op when no ASM is set."""
	if not asm:
		return
	if "Sales Manager" not in frappe.get_roles(asm):
		frappe.throw(
			_("{0} is not a Sales Manager and cannot be set as ASM.").format(
				frappe.db.get_value("User", asm, "full_name") or asm),
			title=_("Invalid ASM"),
		)


def compute_activity(lead, task_type, values, task=None):
	"""The ONE brain that turns a submitted activity form into CRM Task field values: validates
	grain + required, splits first-class columns vs the JSON payload, and runs the location guard
	(set/check the clinic anchor, resolve the address). Returns a flat dict of CRM Task fieldname ->
	value (status, payload, first-class cols, location cols). Raises on out-of-scope / missing / out
	of range. Used by BOTH save paths: save_activity (complete/update existing) and the native
	new-task create (the form script stamps these onto the doc before insert). No second writer.

	`task` is the CRM Task name being completed (or the freshly-inserted shell for a new punch) — it
	is stamped onto the location audit so every Accepted/Not Required row carries its exact task id."""
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

	# Keep the audited ASM data clean: an ASM must actually be a Sales Manager.
	_validate_asm(first_class.get("custom_asm"))

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
		location_required, set_or_check_anchor, location_fields, _reverse_geocode, log_visit_audit)

	radius = location_required(task_type, lead, values)
	if radius is None:
		# Phone / office activity — location was not required for this submission.
		log_visit_audit(lead, task_type, "Not Required", task=task)
		return fields

	lat, lng, accuracy = values.get("lat"), values.get("lng"), values.get("accuracy")
	if not (lat and lng):
		frappe.throw(_("A captured location is required for this in-person activity."),
					 title=_("Location required"))
	guard = set_or_check_anchor(lead, lat, lng, accuracy, radius)  # throws if out of range
	fields.update(location_fields(lat, lng, address=_reverse_geocode(lat, lng), accuracy=accuracy))
	# Accepted (in range, or the first capture that establishes the anchor). Distance is logged from
	# the guard's single haversine — no recompute. Commits with the task save (same txn — task succeeds).
	log_visit_audit(
		lead, task_type, "Accepted", lat=lat, lng=lng,
		distance_m=guard.get("distance_m"), allowed_m=guard.get("allowed_m"),
		anchor_lat=guard.get("anchor_lat"), anchor_lng=guard.get("anchor_lng"), task=task,
	)
	return fields


@frappe.whitelist()
def compute_activity_fields(lead, task_type, values):
	"""Whitelisted compute for the native new-task path: the CRM Task form script stamps the result
	onto the doc, then lets the native create save it ONCE (no double insert, no lingering popup)."""
	return compute_activity(lead, task_type, values)


@frappe.whitelist()
def save_activity(lead, task_type, values, task=None):
	"""THE one writer for completing/updating an activity (board completion + ad-hoc punch). For a new
	punch (no `task`) it inserts the task SHELL first, so the location audit logged inside
	compute_activity carries the new task's exact id — then both paths share one compute → update →
	save. Exact identity on every path, one audit brain. Returns the task name.

	The shell insert is in the same request transaction as the guard: an out-of-range throw rolls the
	shell back with everything else, so a blocked visit never leaves an orphan task."""
	if not task:
		shell = frappe.get_doc({
			"doctype": "CRM Task",
			"title": task_type,
			"custom_task_type": task_type,
			"assigned_to": frappe.session.user,
			"reference_doctype": "CRM Lead",
			"reference_docname": lead,
		})
		shell.insert()
		task = shell.name
	fields = compute_activity(lead, task_type, values, task=task)
	doc = frappe.get_doc("CRM Task", task)
	doc.update(fields)
	doc.save()
	return doc.name


@frappe.whitelist()
def lead_task_board(lead):
	"""ONE render-ready payload for the native Tasks board (<TatvaTasks> in the CRM fork): the lead's
	tasks — each enriched with its saved field values + captured-location state — plus the deduped type
	configs they reference, plus the clinic anchor. The component renders entirely from this: one round
	trip, no per-card N+1. Plain tasks (no type schema) come through too, with a null config."""
	if not frappe.db.exists("CRM Lead", lead):
		frappe.throw(_("Lead {0} not found").format(lead))

	rows = frappe.get_all(
		"CRM Task",
		filters={"reference_doctype": "CRM Lead", "reference_docname": lead},
		fields=[
			"name", "title", "custom_task_type", "status", "priority", "due_date",
			"assigned_to", "owner", "creation", "description", "custom_activity_payload",
			*FIRST_CLASS,
			"custom_location_latitude", "custom_location_longitude",
			"custom_location_address", "custom_location_captured_at",
		],
		order_by="modified desc",
	)

	types = {}
	for tn in {r.custom_task_type for r in rows if r.custom_task_type}:
		cfg = _type_config(tn)
		if cfg:
			types[tn] = cfg

	tasks = []
	for r in rows:
		who = r.assigned_to or r.owner
		tasks.append({
			"name": r.name,
			"title": r.title,
			"task_type": r.custom_task_type or "",
			"status": r.status,
			"priority": r.priority,
			"due_date": str(r.due_date) if r.due_date else None,
			"rep": who,
			"rep_name": (who and frappe.db.get_value("User", who, "full_name")) or who,
			"creation": str(r.creation),
			"datetime": format_datetime(r.creation, "d MMM, h:mm a"),
			"values": _task_values(r, types.get(r.custom_task_type)),
			"location": _task_location(r),
		})

	from tatva_connect.location.api import _read_anchor
	return {"anchor": _read_anchor(frappe.get_doc("CRM Lead", lead)), "types": types, "tasks": tasks}


def _type_config(task_type):
	"""Render config for a task type: the ordered field schema, whether completing it logs Done, and
	whether it can capture location (visit_mode In-Person, or a conditional location_when). None for a
	type with no config row (a plain task)."""
	if not frappe.db.exists("CRM Task Type", task_type):
		return None
	doc = frappe.get_doc("CRM Task Type", task_type)
	return {
		"fields": [
			{
				"label": f.label,
				"fieldname": f.fieldname,
				"fieldtype": f.fieldtype,
				"options": f.options or "",
				"reqd": int(f.reqd or 0),
				"depends_on": (f.get("depends_on") or ""),
				"first_class_target": f.first_class_target or "",
			}
			for f in doc.schema
		],
		"is_logged_complete": int(doc.is_logged_complete or 0),
		"captures_location": bool((doc.visit_mode or "") == "In-Person" or (doc.location_when or "").strip()),
	}


@frappe.whitelist()
def task_detail(task):
	"""Render-ready detail for ONE task by name — the global Tasks list / sidebar opens TatvaTaskModal
	from this (same shape as a lead_task_board task + its type config). `config` is null for a plain
	(non-activity) task, so the client falls back to the native doctype modal. One brain reused
	(_type_config / _task_values / _task_location)."""
	r = frappe.db.get_value(
		"CRM Task", task,
		["name", "title", "custom_task_type", "status", "priority", "due_date",
		 "assigned_to", "owner", "creation", "description", "custom_activity_payload",
		 *FIRST_CLASS,
		 "custom_location_latitude", "custom_location_longitude",
		 "custom_location_address", "custom_location_captured_at",
		 "reference_doctype", "reference_docname"],
		as_dict=True,
	)
	if not r:
		frappe.throw(_("Task {0} not found").format(task))
	cfg = _type_config(r.custom_task_type) if r.custom_task_type else None
	who = r.assigned_to or r.owner
	return {
		"lead": r.reference_docname if r.reference_doctype == "CRM Lead" else None,
		"config": cfg,
		"task": {
			"name": r.name,
			"title": r.title,
			"task_type": r.custom_task_type or "",
			"status": r.status,
			"priority": r.priority,
			"due_date": str(r.due_date) if r.due_date else None,
			"rep": who,
			"rep_name": (who and frappe.db.get_value("User", who, "full_name")) or who,
			"creation": str(r.creation),
			"datetime": format_datetime(r.creation, "d MMM, h:mm a"),
			"values": _task_values(r, cfg),
			"location": _task_location(r),
		},
	}


@frappe.whitelist()
def type_config(task_type):
	"""Render config (fields + is_logged_complete + captures_location) for ONE task type — the
	create-mode modal's source when the chosen type has no existing task seeding it into
	lead_task_board. Same brain (_type_config) the board uses, so card/modal/create stay consistent."""
	cfg = _type_config(task_type)
	if cfg is None:
		frappe.throw(_("Task type {0} not found").format(task_type))
	return cfg


def _task_values(r, cfg):
	"""Saved values keyed by SCHEMA fieldname: the JSON payload (non-first-class fields) merged with the
	first-class columns mapped back to their schema fieldname. So the renderer just reads values[fieldname]."""
	vals = {}
	if (r.custom_activity_payload or "").strip():
		try:
			vals.update(frappe.parse_json(r.custom_activity_payload) or {})
		except Exception:
			pass
	if cfg:
		for f in cfg["fields"]:
			fct = f.get("first_class_target")
			if fct and r.get(fct) not in (None, ""):
				vals[f["fieldname"]] = str(r.get(fct))
	if r.description:
		vals.setdefault("notes", r.description)
	return vals


def _task_location(r):
	"""Captured-location state for a task card — None if no fix was recorded."""
	if not (r.custom_location_latitude and r.custom_location_longitude):
		return None
	return {
		"lat": flt(r.custom_location_latitude),
		"lng": flt(r.custom_location_longitude),
		"address": r.custom_location_address or "",
		"captured_at": str(r.custom_location_captured_at) if r.custom_location_captured_at else None,
	}


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
