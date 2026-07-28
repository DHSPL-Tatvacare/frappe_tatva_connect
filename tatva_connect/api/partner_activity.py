"""Gated partner ACTIVITY API — extends the lead partner contract to activities.

Activities are CRM Tasks of an activity type (a CRM Task Type carrying a grain scope).
This module is the partner-facing REST surface; it owns NO write logic. Every create/
update routes through the ONE activity brain (`tatva_connect.activity.api`):
  * `compute_activity` — grain-validates the type, routes every answer by `field_target`,
    enforces required, runs the location guard. The ONLY computer.
  * `save_activity`    — the ONE writer (shell insert + compute + save). No second writer.
  * `list_types_for_lead` / `get_schema` — discovery of the grain-scoped type catalog.
  * `_task_values`     — re-keys a saved task back to its schema fieldnames for reads.

Enablement is IDENTICAL to leads — NO per-entity enablement. The SAME single enabled
`CRM Lead API Mapping` row (via `_resolve_caller`) + its grain governs activities: a
partner enabled for a grain reaches activities on that grain with the SAME API key.
Every endpoint resolves the lead through the shared `resolve_lead` (grain-scoped); an
out-of-scope or missing lead returns the SAME generic not-found (no probing). The
unified envelope, error codes, and rate limit are all inherited via `@_api`.

IDENTITY. An activity is addressed by `name`, the CRM Task primary key, returned when it was created.
That is the only address. `external_id` is the caller's own label (stored in `custom_external_id`):
echoed back on every read, never interpreted, never used to find an activity, never a dedup key. A POST
creates an activity; a PUT updates one by `name`. Retries are made safe with the Idempotency-Key header.

Everything is generic over the 20-30 activity types across program grains — the type + the lead's grain
drive everything through the brain; nothing is hardcoded per type.

  GET    activity_schema       -> discovery BY LEAD: grain-scoped task types + each type's field schema
  GET    activity_get          -> one activity by CRM Task `name`, scoped to the grain
  GET    activity_list         -> by lead (+ optional task_type/status), paginated
  POST   activity_create       -> create an activity; returns its `name`
  PUT    activity_update       -> re-run compute on an existing activity by `name`
  DELETE activity_delete       -> delete one activity by `name`, scope-checked
  POST   activity_get_bulk     -> {"names":[...]} (<= 100), partial success
  POST   activity_create_bulk  -> {"activities":[...]} (<= 100), partial success
  PUT    activity_update_bulk  -> {"updates":[{"name":..,..}]} (<= 100), partial success
  DELETE activity_delete_bulk  -> {"names":[...]} (<= 100), partial success
"""
import frappe
from frappe import _
from frappe.utils import get_datetime

from tatva_connect.activity import api as activity_brain
from tatva_connect.api._base import (
	ACTION_CREATED,
	ACTION_DELETED,
	ACTION_FETCHED,
	ACTION_UPDATED,
	EXTERNAL_ID_FIELD,
	_api,
	_bulk_read,
	_list_ok,
	_ok,
	_page,
	_read_required_list,
	_resolve_caller,
	_run_bulk,
	_schema_ok,
	field_descriptor,
	resolve_lead,
	scoped_by_lead,
	stamp_external_id,
	throw_field,
	trusted_permissions,
	validate_external_id,
)

# All numeric caps (bulk size, list page sizes) come from the CRM Partner API Settings
# Single via _cfg() — one source of truth, no module-local copy.


# -- scope helpers -----------------------------------------------------------
# A CRM Task is "in scope" iff it references a CRM Lead the caller can resolve.
# We never trust the task's own fields for scope — we re-resolve through the lead,
# so the SAME grain gate (resolve_lead) guards reads, updates and deletes uniformly.

def _scoped_task(name, mp, is_sysmgr):
	"""Load an activity CRM Task by name, scope-checked through its lead. Missing AND
	out-of-scope both raise the SAME generic not-found (no probing which ids exist)."""
	if not name:
		throw_field(_(
			"No activity was named. Send `name`, the CRM Task id returned when the activity was "
			"created; it is also carried by every row of an activity_list response."
		), ["name"])
	row = frappe.db.get_value(
		"CRM Task", name,
		["name", "reference_doctype", "reference_docname", "custom_task_type", "status"],
		as_dict=True,
	)
	if not row or row.reference_doctype != "CRM Lead":
		throw_field(_(
			"No activity on this API key's line has the id `{0}`. Check the value against an "
			"activity_list response for the lead it was created on."
		).format(name), ["name"], frappe.DoesNotExistError)
	scoped_by_lead(row.reference_docname, mp, is_sysmgr, "Activity")
	return row


# Every column the partner shape needs. Projected ONCE — by the single read and by the list alike —
# so a page never re-reads per row.
_PAYLOAD_FIELDS = [
	"name", "reference_docname", "custom_task_type", "status", "description",
	*activity_brain.COMMON_COLUMNS,
	"custom_location_latitude", "custom_location_longitude",
	"custom_location_address", "custom_location_captured_at", EXTERNAL_ID_FIELD,
]


def _render(row, cfg, answers=None):
	"""One activity row -> the partner shape. The single projection both the single read and the list
	use, so a get and a page cannot render differently. `values` is re-keyed by the brain.

	`name` is stringified: CRM Task is autoincrement-named so its PK is an int, while every other
	entity's address is a string. One address, one type — a typed client cannot be asked to handle
	both. Frappe resolves the string form of an autoincrement PK, so the round-trip still works."""
	return {
		"name": str(row.name),
		"lead": row.reference_docname,
		"task_type": row.custom_task_type or "",
		"status": row.status,
		"external_id": row.get(EXTERNAL_ID_FIELD) or None,
		"values": activity_brain._task_values(row, cfg, answers),
	}


def _activity_payload(name):
	"""One activity by name -> the partner shape. The single-record path."""
	row = frappe.db.get_value("CRM Task", name, _PAYLOAD_FIELDS, as_dict=True)
	cfg = activity_brain._type_config(row.custom_task_type) if row.custom_task_type else None
	return _render(row, cfg)


def _resolve_task_type(lead, task_type):
	"""A partner's `task_type` -> this lead's grain-scoped composite type PK. The one resolver create,
	update and the list filter all call, so discovery, ingestion and query speak one vocabulary.
	Accepts the human name or the composite PK. An unavailable type is refused, never coerced."""
	with trusted_permissions():  # authz-ok: caller pre-gated by _resolve_caller + resolve_lead (mapping+grain)
		resolved = activity_brain.resolve_type_for_lead(lead, task_type)
	if not resolved:
		throw_field(_(
			"`{0}` is not an activity type this lead's grain runs. Call activity_schema for this lead "
			"and send one of the `name` values it lists."
		).format(task_type), ["task_type"])
	return resolved


def _backdate(name, created_at):
	"""Backdate the task's `creation` from a partner-supplied timestamp (historical load).
	No-op on a blank/unparseable value, so live creates keep `now`."""
	if not created_at:
		return
	dt = get_datetime(created_at)
	if dt:
		frappe.db.set_value("CRM Task", name, "creation", dt, update_modified=False)


# -- per-record core (shared by singular + bulk) -----------------------------

def _create_one(item, mp, is_sysmgr):
	"""Create ONE activity through the brain (the only writer). A create creates: there is no upsert on
	a caller key, so a re-POST yields a second task. Retries are made safe with Idempotency-Key."""
	lead = resolve_lead(mp, is_sysmgr, item)
	task_type = item.get("task_type")
	if not task_type:
		throw_field(_(
			"No activity type was named. Send `task_type`; call activity_schema for this lead to see "
			"the types its grain runs and the fields each one takes."
		), ["task_type"])
	validate_external_id("CRM Task", item.get("external_id"))
	values = item.get("values") or {}

	resolved = _resolve_task_type(lead, task_type)
	with trusted_permissions():  # authz-ok: caller pre-gated by _resolve_caller + resolve_lead (mapping+grain)
		name = activity_brain.save_activity(lead, resolved, values, task=None)

	stamp_external_id("CRM Task", name, item.get("external_id"))
	_backdate(name, item.get("created_at"))
	return _activity_payload(name)


def _update_one(name, item, mp, is_sysmgr):
	"""Re-run compute on an EXISTING activity by CRM Task name, scope-checked. Re-uses
	the brain (task=name -> no new insert). Returns the partner payload."""
	row = _scoped_task(name, mp, is_sysmgr)
	task_type = item.get("task_type") or row.custom_task_type
	if not task_type:
		throw_field(_(
			"No activity type was named. Send `task_type`; call activity_schema for this lead to see "
			"the types its grain runs and the fields each one takes."
		), ["task_type"])
	validate_external_id("CRM Task", item.get("external_id"))
	values = item.get("values") or {}

	resolved = _resolve_task_type(row.reference_docname, task_type)
	with trusted_permissions():  # authz-ok: caller pre-gated by _resolve_caller + resolve_lead (mapping+grain)
		activity_brain.save_activity(row.reference_docname, resolved, values, task=name)
	if item.get("external_id") is not None:
		stamp_external_id("CRM Task", name, item.get("external_id"))
	return _activity_payload(name)


def _delete_one(name, mp, is_sysmgr):
	"""Delete one activity by CRM Task name, scope-checked (generic not-found)."""
	row = _scoped_task(name, mp, is_sysmgr)
	frappe.delete_doc("CRM Task", row.name, ignore_permissions=True)  # authz-ok: tier-b — gated by _resolve_caller + resolve_lead, before the save


def _read_one(name, mp, is_sysmgr):
	"""Load one activity by `name` -> the partner payload. The per-record loader both activity_get
	and activity_get_bulk call, so a single read and a bulk read can never diverge."""
	row = _scoped_task(name, mp, is_sysmgr)
	return _activity_payload(row.name)


# -- discovery ---------------------------------------------------------------

@frappe.whitelist(methods=["GET"])
@_api(read=True)
def activity_schema(**_kwargs):
	"""DISCOVERY BY LEAD: given `?lead=<name>` or `?mobile_no=`, return the activity
	types available to that lead's grain, each with its field schema — how an integrator
	discovers exactly what to send for this patient. Grain-scoped through resolve_lead."""
	_user, mp, is_sysmgr = _resolve_caller()
	lead = resolve_lead(mp, is_sysmgr, frappe.form_dict)

	types = []
	with trusted_permissions():  # authz-ok: caller pre-gated by _resolve_caller + resolve_lead (mapping+grain)
		for t in activity_brain.list_types_for_lead(lead):
			schema = activity_brain.get_schema(t["name"])
			types.append({
				# `name` is the composite key the caller POSTs back; `label` is the same clean type_name
				# the picker already resolved. Without it the integrator's menu is a list of `::` strings.
				"name": t["name"],
				"label": t.get("label") or t["name"],
				"is_logged_complete": int(t.get("is_logged_complete") or 0),
				"fields": [
					field_descriptor(f["fieldname"], f["label"], f["fieldtype"], f.get("reqd"), f.get("options"))
					for f in schema
				],
			})
	_schema_ok(
		"activity",
		dedup=(
			"None. Every POST creates a new activity and returns a new `name`. Retries are made safe "
			"with the Idempotency-Key header; `external_id` does not deduplicate."
		),
		lead=lead,
		task_types=types,
		values=(
			"Data is sent and read under `values`, keyed by the `fieldname`s of the chosen task_type. "
			"The available types and their fields vary by the lead's grain, so this endpoint is called "
			"per lead and a field list is never hardcoded."
		),
	)


# -- singular endpoints ------------------------------------------------------

@frappe.whitelist(methods=["GET"])
@_api(read=True)
def activity_get(**_kwargs):
	"""Read one activity by CRM Task `name`, scoped to the caller's grain. Returns the
	activity with `values` re-keyed to its schema fieldnames."""
	_user, mp, is_sysmgr = _resolve_caller()
	_ok(action=ACTION_FETCHED, data=_read_one(frappe.form_dict.get("name"), mp, is_sysmgr))


@frappe.whitelist(methods=["POST"])
@_api
def activity_create(**_kwargs):
	"""Create an activity. Body: {lead|mobile_no, task_type, external_id?, values:{fieldname:value},
	created_at?}. Returns the `name` to address the activity by from now on."""
	_user, mp, is_sysmgr = _resolve_caller()
	_ok(action=ACTION_CREATED, data=_create_one(frappe.form_dict, mp, is_sysmgr))


@frappe.whitelist(methods=["PUT"])
@_api
def activity_update(**_kwargs):
	"""Update an activity by CRM Task `name` — re-run compute with new values. Scope-checked."""
	_user, mp, is_sysmgr = _resolve_caller()
	_ok(action=ACTION_UPDATED, data=_update_one(frappe.form_dict.get("name"), frappe.form_dict, mp, is_sysmgr))


@frappe.whitelist(methods=["DELETE"])
@_api
def activity_delete(**_kwargs):
	"""Delete an activity by CRM Task `name`, scope-checked (generic not-found)."""
	_user, mp, is_sysmgr = _resolve_caller()
	name = frappe.form_dict.get("name")
	_delete_one(name, mp, is_sysmgr)
	_ok(action=ACTION_DELETED, data={"name": name})


# -- bulk / query endpoints --------------------------------------------------

@frappe.whitelist(methods=["POST"])
@_api(bulk=True, read=True)
def activity_get_bulk(**_kwargs):
	"""Read many activities by `names` (<= 100). Input-ordered; out-of-scope/unknown names are
	reported not_found in place."""
	_user, mp, is_sysmgr = _resolve_caller()
	names = _read_required_list(frappe.form_dict, "names")
	return _bulk_read(names, lambda name: _read_one(name, mp, is_sysmgr))


def bulk_creator(mp, is_sysmgr):
	"""The per-record create closure, shared by the sync bulk endpoint and the async worker (one brain)."""
	def one(i, item):
		return {"index": i, "status": "success", "action": ACTION_CREATED,
		        "data": _create_one(item, mp, is_sysmgr)}
	return one


@frappe.whitelist(methods=["POST"])
@_api(bulk=True)
def activity_create_bulk(**_kwargs):
	"""Create many activities. Body: {"activities":[{...}, ...]} (<= 100).
	Each record is enforced in its own savepoint -> partial success."""
	_user, mp, is_sysmgr = _resolve_caller()
	activities = _read_required_list(frappe.form_dict, "activities")
	return _run_bulk(activities, bulk_creator(mp, is_sysmgr))


@frappe.whitelist(methods=["PUT"])
@_api(bulk=True)
def activity_update_bulk(**_kwargs):
	"""Update many activities. Body: {"updates":[{"name":.., ...}, ...]} (<= 100). Partial success."""
	_user, mp, is_sysmgr = _resolve_caller()
	updates = _read_required_list(frappe.form_dict, "updates")

	def one(i, item):
		return {"index": i, "status": "success", "action": ACTION_UPDATED,
		        "data": _update_one((item or {}).get("name"), item, mp, is_sysmgr)}

	return _run_bulk(updates, one)


@frappe.whitelist(methods=["DELETE"])
@_api(bulk=True)
def activity_delete_bulk(**_kwargs):
	"""Delete many activities. Body: {"names":[...]} (<= 100). Partial success."""
	_user, mp, is_sysmgr = _resolve_caller()
	names = _read_required_list(frappe.form_dict, "names")

	def one(i, name):
		_delete_one(name, mp, is_sysmgr)
		return {"index": i, "status": "success", "action": ACTION_DELETED, "data": {"name": name}}

	return _run_bulk(names, one)


@frappe.whitelist(methods=["GET"])
@_api(bulk=True, read=True)
def activity_list(**_kwargs):
	"""List activities on a lead, paginated. `?lead=` (or `?mobile_no=`) is required and
	grain-scoped through resolve_lead; optional `task_type` / `status` filters."""
	_user, mp, is_sysmgr = _resolve_caller()
	data = frappe.form_dict
	lead = resolve_lead(mp, is_sysmgr, data)
	limit, offset = _page(data)

	# Only activity-typed tasks (a configured CRM Task Type with a scope row) — plain
	# tasks are not partner activities. Scope to this lead's referenced tasks.
	activity_types = activity_brain._activity_type_names()
	if not activity_types:
		_list_ok("activities", [], 0, offset, limit)
		return
	filters = {"reference_doctype": "CRM Lead", "reference_docname": lead}
	if data.get("task_type"):
		# The filter resolves through the same brain the create does, so both speak one vocabulary and
		# an unavailable type is refused rather than silently matching nothing.
		filters["custom_task_type"] = _resolve_task_type(lead, data.get("task_type"))
	else:
		filters["custom_task_type"] = ["in", list(activity_types)]
	if data.get("status"):
		filters["status"] = data.get("status")

	total = frappe.db.count("CRM Task", filters)
	rows = frappe.get_all(
		"CRM Task", filters=filters, fields=_PAYLOAD_FIELDS,
		limit_page_length=limit, limit_start=offset, order_by="creation desc",
	)
	# One config per DISTINCT type, not per row: _type_config costs a db.exists plus a get_doc pulling
	# two child tables. Same batching the SPA's task page uses.
	cfgs = {
		tt: activity_brain._type_config(tt)
		for tt in {r.custom_task_type for r in rows if r.custom_task_type}
	}
	# The saved answers for the page in one query per section, not one read per row.
	answers = activity_brain.section_rows([r.name for r in rows])
	activities = [_render(r, cfgs.get(r.custom_task_type), answers.get(str(r.name), {})) for r in rows]
	_list_ok("activities", activities, total, offset, limit)
