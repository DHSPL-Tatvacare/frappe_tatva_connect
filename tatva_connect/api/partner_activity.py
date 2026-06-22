"""Gated partner ACTIVITY API — extends the lead partner contract to activities.

Activities are CRM Tasks of an activity type (a CRM Task Type carrying a grain scope).
This module is the partner-facing REST surface; it owns NO write logic. Every create/
update routes through the ONE activity brain (`tatva_connect.activity.api`):
  * `compute_activity` — grain-validates the type, splits promoted columns vs JSON
    payload, enforces required, runs the location guard. The ONLY computer.
  * `save_activity`    — the ONE writer (shell insert + compute + save). No second writer.
  * `list_types_for_lead` / `get_schema` — discovery of the grain-scoped type catalog.
  * `_task_values`     — re-keys a saved task back to its schema fieldnames for reads.

Enablement is IDENTICAL to leads — NO per-entity enablement. The SAME single enabled
`CRM Lead API Mapping` row (via `_resolve_caller`) + its grain governs activities: a
partner enabled for a grain reaches activities on that grain with the SAME API key.
Every endpoint resolves the lead through the shared `resolve_lead` (grain-scoped); an
out-of-scope or missing lead returns the SAME generic not-found (no probing). The
unified envelope, error codes, and 100/60s rate limit are all inherited via `@_api`.

`external_id` is the idempotency key, stored in `custom_lsq_activity_id`: re-sending the
same external_id updates that task, never doubles. Everything is generic over the 20-30
activity types across program grains — the type + the lead's grain drive everything
through the brain; nothing is hardcoded per type.

  GET    activity_schema  -> discovery BY LEAD: grain-scoped task types + each type's
                            field schema (what an integrator may send for this lead)
  GET    activity_get     -> one activity by CRM Task `name`, scoped to the grain
  POST   activity_create  -> create-or-upsert (dedup on external_id); returns the name
  PUT    activity_update  -> re-run compute on an existing activity by `name`
  DELETE activity_delete  -> delete one activity by `name`, scope-checked
  POST   activity_create_bulk -> {"activities":[...]} (<= 100), partial success
  POST   activity_get_bulk    -> {"names":[...]} or {"external_ids":[...]} (<= 100)
  GET    activity_list        -> by lead (+ optional task_type/status), paginated
"""
import frappe
from frappe import _
from frappe.utils import cint, get_datetime

from tatva_connect.activity import api as activity_brain
from tatva_connect.api._base import (
	BULK_MAX,
	_api,
	_fail,  # noqa: F401  (kept available for symmetry with partner.py)
	_norm_phone,  # noqa: F401  (re-exported convenience)
	_ok,
	_read_list,
	_resolve_caller,
	_run_bulk,
	resolve_lead,
)

# Pagination (mirrors partner.lead_list; BULK_MAX is the shared per-call cap from _base).
LIST_DEFAULT = 20       # default page size
LIST_MAX = 200          # max page size

DEDUP_FIELD = "custom_lsq_activity_id"   # the idempotency key column on CRM Task


# -- scope helpers -----------------------------------------------------------
# A CRM Task is "in scope" iff it references a CRM Lead the caller can resolve.
# We never trust the task's own fields for scope — we re-resolve through the lead,
# so the SAME grain gate (resolve_lead) guards reads, updates and deletes uniformly.

def _scoped_task(name, mp, is_sysmgr):
	"""Load an activity CRM Task by name, scope-checked through its lead. Missing AND
	out-of-scope both raise the SAME generic not-found (no probing which ids exist)."""
	if not name:
		frappe.throw(_("name (the CRM Task id) is required"))
	row = frappe.db.get_value(
		"CRM Task", name,
		["name", "reference_doctype", "reference_docname", "custom_task_type", "status"],
		as_dict=True,
	)
	if not row or row.reference_doctype != "CRM Lead" or not row.reference_docname:
		frappe.throw(_("Activity not found"), frappe.DoesNotExistError)
	# Re-resolve the lead under the caller's grain — out-of-line tasks vanish.
	# resolve_lead({"lead": X}) returns X if the caller can reach it, else throws; an
	# out-of-scope lead therefore surfaces as the SAME generic not-found (no probing).
	try:
		resolve_lead(mp, is_sysmgr, {"lead": row.reference_docname})
	except frappe.DoesNotExistError:
		frappe.throw(_("Activity not found"), frappe.DoesNotExistError)
	return row


def _activity_payload(name):
	"""Render one activity to the partner shape: name, lead, task_type, status, and
	`values` re-keyed to its schema fieldnames (reuse the brain's _task_values via the
	type config — one projection, identical to the SPA/timeline)."""
	r = frappe.db.get_value(
		"CRM Task", name,
		["name", "reference_docname", "custom_task_type", "status", "description",
		 "custom_activity_payload", *activity_brain.PROMOTED_COLUMNS,
		 "custom_location_latitude", "custom_location_longitude",
		 "custom_location_address", "custom_location_captured_at", DEDUP_FIELD],
		as_dict=True,
	)
	cfg = activity_brain._type_config(r.custom_task_type) if r.custom_task_type else None
	return {
		"name": r.name,
		"lead": r.reference_docname,
		"task_type": r.custom_task_type or "",
		"status": r.status,
		"external_id": r.get(DEDUP_FIELD) or None,
		"values": activity_brain._task_values(r, cfg),
	}


def _find_by_external_id(external_id):
	"""The CRM Task name carrying this external_id (the dedup key), or None."""
	if not external_id:
		return None
	return frappe.db.get_value("CRM Task", {DEDUP_FIELD: external_id}, "name")


def _backdate(name, created_at):
	"""Backdate the task's `creation` from a partner-supplied timestamp (migration /
	historical load). No-op on a blank/unparseable value, so live creates keep `now`."""
	if not created_at:
		return
	dt = get_datetime(created_at)
	if dt:
		frappe.db.set_value("CRM Task", name, "creation", dt, update_modified=False)


# -- per-record core (shared by singular + bulk) -----------------------------

def _upsert_one(item, mp, is_sysmgr):
	"""Create-or-upsert ONE activity. Resolves the lead (grain-scoped), runs the brain's
	compute → save (the ONLY writer), deduped on external_id. Returns the partner payload.

	external_id present and already on a task -> update THAT task (re-send is idempotent).
	external_id new (or absent) -> the brain inserts a fresh task. Then stamp the
	external_id and (optionally) backdate creation."""
	lead = resolve_lead(mp, is_sysmgr, item)
	task_type = item.get("task_type")
	if not task_type:
		frappe.throw(_("task_type is required"))
	values = item.get("values") or {}

	external_id = item.get("external_id")
	existing = _find_by_external_id(external_id)
	# The brain computes + writes; `task=existing` re-runs compute on the same task
	# (no duplicate insert). New external_id (or none) -> brain inserts the shell.
	name = activity_brain.save_activity(lead, task_type, values, task=existing)

	if external_id and not existing:
		frappe.db.set_value("CRM Task", name, DEDUP_FIELD, external_id, update_modified=False)
	if not existing:
		_backdate(name, item.get("created_at"))
	return _activity_payload(name)


def _update_one(name, item, mp, is_sysmgr):
	"""Re-run compute on an EXISTING activity by CRM Task name, scope-checked. Re-uses
	the brain (task=name -> no new insert). Returns the partner payload."""
	row = _scoped_task(name, mp, is_sysmgr)
	task_type = item.get("task_type") or row.custom_task_type
	if not task_type:
		frappe.throw(_("task_type is required"))
	values = item.get("values") or {}
	activity_brain.save_activity(row.reference_docname, task_type, values, task=name)
	return _activity_payload(name)


def _delete_one(name, mp, is_sysmgr):
	"""Delete one activity by CRM Task name, scope-checked (generic not-found)."""
	row = _scoped_task(name, mp, is_sysmgr)
	frappe.delete_doc("CRM Task", row.name, ignore_permissions=True)


# -- singular endpoints ------------------------------------------------------

@frappe.whitelist(methods=["GET"])
@_api
def activity_schema(**kwargs):
	"""DISCOVERY BY LEAD: given `?lead=<name>` or `?mobile_no=`, return the activity
	types available to that lead's grain, each with its field schema — how an integrator
	discovers exactly what to send for this patient. Grain-scoped through resolve_lead."""
	user, mp, is_sysmgr = _resolve_caller()
	lead = resolve_lead(mp, is_sysmgr, frappe.form_dict)

	types = activity_brain.list_types_for_lead(lead)
	out = []
	for t in types:
		schema = activity_brain.get_schema(t["name"])
		out.append({
			"name": t["name"],
			"is_logged_complete": int(t.get("is_logged_complete") or 0),
			"fields": [
				{
					"fieldname": f["fieldname"],
					"label": f["label"],
					"type": f["fieldtype"],
					"options": f.get("options") or None,
					"reqd": int(f.get("reqd") or 0),
				}
				for f in schema
			],
		})
	_ok(action="fetched", data={"lead": lead, "task_types": out})


@frappe.whitelist(methods=["GET"])
@_api
def activity_get(**kwargs):
	"""Read one activity by CRM Task `name`, scoped to the caller's grain. Returns the
	activity with `values` re-keyed to its schema fieldnames."""
	user, mp, is_sysmgr = _resolve_caller()
	row = _scoped_task(frappe.form_dict.get("name"), mp, is_sysmgr)
	_ok(action="fetched", data=_activity_payload(row.name))


@frappe.whitelist(methods=["POST"])
@_api
def activity_create(**kwargs):
	"""Create-or-upsert an activity. Body: {lead|mobile_no, task_type, external_id,
	values:{fieldname:value}, created_at?}. Deduped on external_id; re-send updates."""
	user, mp, is_sysmgr = _resolve_caller()
	data = _upsert_one(frappe.form_dict, mp, is_sysmgr)
	_ok(action="upserted", data=data)


@frappe.whitelist(methods=["PUT"])
@_api
def activity_update(**kwargs):
	"""Update an activity by CRM Task `name` — re-run compute with new values. Scope-checked."""
	user, mp, is_sysmgr = _resolve_caller()
	data = _update_one(frappe.form_dict.get("name"), frappe.form_dict, mp, is_sysmgr)
	_ok(action="updated", data=data)


@frappe.whitelist(methods=["DELETE"])
@_api
def activity_delete(**kwargs):
	"""Delete an activity by CRM Task `name`, scope-checked (generic not-found)."""
	user, mp, is_sysmgr = _resolve_caller()
	name = frappe.form_dict.get("name")
	_delete_one(name, mp, is_sysmgr)
	_ok(action="deleted", data={"name": name})


# -- bulk / query endpoints --------------------------------------------------

@frappe.whitelist(methods=["POST"])
@_api
def activity_create_bulk(**kwargs):
	"""Create-or-upsert many activities. Body: {"activities":[{...}, ...]} (<= 100).
	Each record is enforced in its own savepoint -> partial success."""
	user, mp, is_sysmgr = _resolve_caller()
	activities = _read_list(frappe.form_dict, "activities") or []

	def one(i, item):
		data = _upsert_one(item, mp, is_sysmgr)
		return {"index": i, "status": "success", "action": "upserted",
				"name": data["name"], "external_id": data["external_id"]}

	return _run_bulk(activities, one)


@frappe.whitelist(methods=["POST"])
@_api
def activity_get_bulk(**kwargs):
	"""Read many activities by `names` OR `external_ids` (<= 100). Out-of-scope/unknown
	ids are reported not_found in place — input-ordered (results[i] = the i-th id)."""
	user, mp, is_sysmgr = _resolve_caller()
	data = frappe.form_dict
	names = _read_list(data, "names")
	external_ids = _read_list(data, "external_ids")
	if not names and not external_ids:
		frappe.throw(_("names or external_ids is required"))

	by = "name" if names else "external_id"
	requested = list(names) if names else list(external_ids)
	if len(requested) > BULK_MAX:
		frappe.throw(_("Max {0} per call; received {1}. Page the rest.").format(BULK_MAX, len(requested)))

	results, found = [], 0
	for i, ident in enumerate(requested):
		name = ident if by == "name" else _find_by_external_id(ident)
		try:
			if not name:
				raise frappe.DoesNotExistError(_("Activity not found"))
			row = _scoped_task(name, mp, is_sysmgr)
			results.append({"index": i, "status": "success", "data": _activity_payload(row.name)})
			found += 1
		except frappe.DoesNotExistError:
			results.append({"index": i, "status": "error",
				"error": {"code": "not_found", "message": _("Activity not found")}})
	_ok(summary={"requested": len(requested), "found": found, "not_found": len(requested) - found},
		results=results)


@frappe.whitelist(methods=["GET"])
@_api
def activity_list(**kwargs):
	"""List activities on a lead, paginated. `?lead=` (or `?mobile_no=`) is required and
	grain-scoped through resolve_lead; optional `task_type` / `status` filters. Returns
	{total, count, offset, limit, has_more, activities:[...]}."""
	user, mp, is_sysmgr = _resolve_caller()
	data = frappe.form_dict
	lead = resolve_lead(mp, is_sysmgr, data)

	# Only activity-typed tasks (a configured CRM Task Type with a scope row) — plain
	# tasks are not partner activities. Scope to this lead's referenced tasks.
	activity_types = activity_brain._activity_type_names()
	filters = {"reference_doctype": "CRM Lead", "reference_docname": lead}
	if not activity_types:
		_ok(action="fetched", data={"total": 0, "count": 0, "offset": 0, "limit": LIST_DEFAULT,
			"has_more": False, "activities": []})
		return
	if data.get("task_type"):
		tt = data.get("task_type")
		filters["custom_task_type"] = tt if tt in activity_types else "__none__"
	else:
		filters["custom_task_type"] = ["in", list(activity_types)]
	if data.get("status"):
		filters["status"] = data.get("status")

	limit = min(cint(data.get("limit")) or LIST_DEFAULT, LIST_MAX)
	offset = cint(data.get("offset") or data.get("limit_start"))

	total = frappe.db.count("CRM Task", filters)
	rows = frappe.get_all(
		"CRM Task", filters=filters, fields=["name"],
		limit_page_length=limit, limit_start=offset, order_by="creation desc",
	)
	activities = [_activity_payload(r.name) for r in rows]
	_ok(action="fetched", data={
		"total": total, "count": len(activities), "offset": offset, "limit": limit,
		"has_more": (offset + len(activities)) < total, "activities": activities,
	})
