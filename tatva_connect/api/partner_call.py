"""Gated partner CALL API — create / read / update / list / delete call logs on a lead.

Shares the ONE brain in `tatva_connect.api._base`: the SAME `_resolve_caller` enablement gate (the
single enabled `CRM Lead API Mapping` row + its grain governs calls just like leads/activities/files —
there is NO per-entity enablement), the SAME `resolve_lead` grain-scoped resolver, the SAME `_ok`/`_fail`
envelope, error codes, rate limit, `_run_bulk` / `_bulk_read` partial-success engines and `_list_ok`
list envelope.

Calls land in frappe/crm's native `CRM Call Log` — the SAME doctype the Acefone webhook adapter writes
(`tatva_connect.telephony.adapters.acefone`). No parallel call store.

IDENTITY. A call is addressed by `name`, the CRM Call Log primary key, returned when it was created.
That is the only address. `external_id` is the caller's own label (stored in `custom_external_id`):
echoed back on every read, never interpreted, never used to find a call, never a dedup key. A POST
creates a call; a PUT updates one by `name`. Retries are made safe with the Idempotency-Key header.

Lead attribution (Invariant #16 — NO best-guess):
  * `lead`/`mobile_no` given -> the shared grain-scoped `resolve_lead`.
  * else -> STRICT last-10 phone match on the customer number, SCOPED to the caller's grain
    (vertical+group). Exactly one match links; no match or ambiguous (2+) -> leave UNLINKED, never
    attach to the wrong lead.

  GET    call_schema       -> discovery: the call payload contract (fields, types, required)
  GET    call_get          -> one call by `name`, grain-scoped, generic not-found
  GET    call_list         -> a lead's calls (+ direction/status), paginated
  POST   call_create       -> create a call log; returns its `name`
  PUT    call_update       -> update a call by `name`, scope-checked
  DELETE call_delete       -> delete a call by `name`, scope-checked
  POST   call_get_bulk     -> {"names":[...]} (<= 100), partial success
  POST   call_create_bulk  -> {"calls":[...]} (<= 100), partial success
  PUT    call_update_bulk  -> {"updates":[{"name":..,..}]} (<= 100), partial success
  DELETE call_delete_bulk  -> {"names":[...]} (<= 100), partial success
"""
import frappe
from frappe import _
from frappe.utils import cint, get_datetime

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
	_read_list,
	_read_required_list,
	_resolve_caller,
	_run_bulk,
	_schema_ok,
	resolve_lead,
	scoped_by_lead,
	stamp_external_id,
	validate_external_id,
)
from tatva_connect.api.field_spec import FieldSpec, collect, describe

# All numeric caps (bulk size, list page sizes) come from the CRM Partner API Settings
# Single via _cfg() — one source of truth, no module-local copy.

# Partner direction vocab -> CRM Call Log `type` vocab.
_DIRECTION_TYPE = {"Inbound": "Incoming", "Outbound": "Outgoing"}

# The call's payload contract — declared ONCE, read by `describe` (what call_schema advertises) and by
# `collect` (what the write path accepts). Discovery equals ingestion because neither owns a field list.
# `direction` declares its own vocabulary: the column holds Incoming/Outgoing and partners speak
# Inbound/Outbound, so the internal values are suppressed. Translating them stays in _apply_fields.
CALL_FIELDS = (
	FieldSpec("lead",          "Lead",          "reference_docname"),
	FieldSpec("mobile_no",     "Mobile No"),
	FieldSpec("external_id",   "External ID",   EXTERNAL_ID_FIELD),
	FieldSpec("direction",     "Direction",     "type", required=True,
	          allowed_values=tuple(_DIRECTION_TYPE)),
	FieldSpec("from_number",   "From Number",   "from"),
	FieldSpec("to_number",     "To Number",     "to"),
	FieldSpec("status",        "Status",        "status"),
	FieldSpec("duration",      "Duration",      "duration"),
	FieldSpec("recording_url", "Recording URL", "recording_url"),
	FieldSpec("started_at",    "Started At",    "start_time"),
)


# -- helpers -----------------------------------------------------------------

def _last10(raw):
	"""The last 10 digits of a phone number — the strict-match anchor (parity with the
	telephony adapter's last-10 LIKE). None if there aren't 10 digits to anchor on."""
	if not raw:
		return None
	digits = "".join(c for c in str(raw) if c.isdigit())
	return digits[-10:] if len(digits) >= 10 else None


def _attribute_lead(data, mp, is_sysmgr):
	"""Resolve the lead this call attaches to, fail-closed (Invariant #16).

	`lead`/`mobile_no` -> the shared grain-scoped resolver (raises generic not-found if the
	caller can't reach it). Otherwise STRICT last-10 match on the customer number, scoped to
	the caller's grain: exactly one routed lead links; no/ambiguous match -> None (unlinked).
	"""
	if data.get("lead") or data.get("mobile_no"):
		return resolve_lead(mp, is_sysmgr, data)

	# Strict phone+grain attribution. The customer number is the OTHER party: the from_number
	# on an inbound call, the to_number on an outbound call.
	direction = data.get("direction")
	customer = data.get("from_number") if direction == "Inbound" else data.get("to_number")
	anchor = _last10(customer)
	if not anchor:
		return None
	filters = {"mobile_no": ["like", f"%{anchor}"]}
	if mp:
		filters["custom_vertical"] = mp.vertical
		filters["custom_group"] = mp.crm_group
	matches = frappe.get_all("CRM Lead", filters=filters, pluck="name", limit=2)
	return matches[0] if len(matches) == 1 else None


def _call_view(doc):
	"""The partner-facing shape of a CRM Call Log row."""
	return {
		"name": doc.name,
		"external_id": doc.get(EXTERNAL_ID_FIELD),
		"lead": doc.reference_docname if doc.reference_doctype == "CRM Lead" else None,
		"direction": "Inbound" if doc.type == "Incoming" else "Outbound",
		"from_number": doc.get("from"),
		"to_number": doc.to,
		"status": doc.status,
		"duration": doc.duration,
		"recording_url": doc.recording_url,
		"start_time": str(doc.start_time) if doc.start_time else None,
	}


def _scoped_call(name, mp, is_sysmgr):
	"""Load a CRM Call Log by name, grain-scoped through its linked lead. Missing AND
	out-of-scope both raise the SAME generic not-found (no probing which ids exist)."""
	if not name:
		frappe.throw(_("name (the CRM Call Log id) is required"))
	doc = frappe.db.exists("CRM Call Log", name) and frappe.get_doc("CRM Call Log", name)
	if not doc:
		frappe.throw(_("Call not found"), frappe.DoesNotExistError)
	# An UNLINKED call is never visible to a partner; a trusted sysmgr (no mapping) still sees it.
	if mp:
		lead = doc.reference_docname if doc.reference_doctype == "CRM Lead" else None
		scoped_by_lead(lead, mp, is_sysmgr, "Call")
	return doc


def _apply_fields(doc, data, lead_name):
	"""Overlay the partner payload onto a CRM Call Log doc (create or update path).

	`collect` decides WHAT may land and on which column, so this is keyed by column and no longer
	restates the mapping. Two targets are never taken from the caller: `type` carries the partner's
	vocabulary and is translated below, and `reference_docname` is resolved by `_attribute_lead` (writing
	the raw value would attach the call to a lead off the caller's line)."""
	fields = collect(CALL_FIELDS, data)
	direction = data.get("direction")
	if direction and direction not in _DIRECTION_TYPE:
		frappe.throw(_("direction must be Inbound or Outbound"))
	if direction:
		doc.type = _DIRECTION_TYPE[direction]

	if fields.get("from") is not None:
		setattr(doc, "from", str(fields.get("from") or ""))
	if fields.get("to") is not None:
		doc.to = str(fields.get("to") or "")
	if fields.get("status"):
		doc.status = fields.get("status")
	if fields.get("duration") not in (None, ""):
		doc.duration = cint(fields.get("duration"))
	if fields.get("recording_url") is not None:
		doc.recording_url = fields.get("recording_url")
	if fields.get("start_time"):
		dt = get_datetime(fields.get("start_time"))
		if dt:
			doc.start_time = dt

	# Link the lead via BOTH reference_* (the Calls-tab feed) and the links child table —
	# the SAME dual-write the Acefone adapter does (crm's get_linked_calls filters on
	# reference_docname; the links table is crm/Exotel parity).
	if lead_name:
		doc.reference_doctype = "CRM Lead"
		doc.reference_docname = lead_name
		doc.link_with_reference_doc("CRM Lead", lead_name)


# -- per-record core (shared by singular + bulk) -----------------------------

def _create_one(data, mp, is_sysmgr):
	"""Create ONE call log. Returns (call_view, "created"). A create creates: there is no upsert on a
	caller key, so a re-POST yields a second call. Retries are made safe with Idempotency-Key."""
	direction = data.get("direction")
	if not direction or direction not in _DIRECTION_TYPE:
		frappe.throw(_("direction (Inbound or Outbound) is required"))
	validate_external_id("CRM Call Log", data.get("external_id"))

	lead_name = _attribute_lead(data, mp, is_sysmgr)

	doc = frappe.new_doc("CRM Call Log")
	# The autoname is field:id, so the row NAME is `id` — a fresh hash that cannot collide.
	doc.id = f"PARTNER-{frappe.generate_hash(length=10)}"
	# Sensible required-field floors so a sparse payload still inserts (status defaults to
	# "Completed" only if the caller sent none; from/to default to empty strings).
	doc.status = data.get("status") or "Completed"
	setattr(doc, "from", "")
	doc.to = ""
	_apply_fields(doc, data, lead_name)
	doc.insert(ignore_permissions=True)  # authz-ok: tier-b — gated by _resolve_caller + _attribute_lead, before the save

	stamp_external_id("CRM Call Log", doc.name, data.get("external_id"))
	# Backdate creation from started_at (historical load), mirroring the activity API.
	if data.get("started_at"):
		dt = get_datetime(data.get("started_at"))
		if dt:
			frappe.db.set_value("CRM Call Log", doc.name, "creation", dt, update_modified=False)
	return _call_view(frappe.get_doc("CRM Call Log", doc.name)), ACTION_CREATED


def _update_one(name, data, mp, is_sysmgr):
	"""Update ONE call log by `name`, scope-checked. Only the fields present in the payload change.
	Returns (call_view, "updated")."""
	doc = _scoped_call(name, mp, is_sysmgr)
	validate_external_id("CRM Call Log", data.get("external_id"))
	# Re-attribution is allowed only when the caller explicitly names a lead; a payload that omits
	# lead/mobile_no leaves the existing link alone (it never silently re-attributes by phone).
	lead_name = resolve_lead(mp, is_sysmgr, data) if (data.get("lead") or data.get("mobile_no")) else None
	_apply_fields(doc, data, lead_name)
	doc.save(ignore_permissions=True)  # authz-ok: tier-b — gated by _resolve_caller + _attribute_lead, before the save
	if data.get("external_id") is not None:
		stamp_external_id("CRM Call Log", doc.name, data.get("external_id"))
	return _call_view(frappe.get_doc("CRM Call Log", doc.name)), ACTION_UPDATED


def _delete_one(name, mp, is_sysmgr):
	"""Delete one call by `name`, scope-checked (generic not-found)."""
	doc = _scoped_call(name, mp, is_sysmgr)
	frappe.delete_doc("CRM Call Log", doc.name, ignore_permissions=True)  # authz-ok: tier-b — gated by _resolve_caller + _attribute_lead, before the save


def _read_one(name, mp, is_sysmgr):
	"""Load one call by `name` -> the partner view. The per-record loader both call_get and
	call_get_bulk call, so a single read and a bulk read can never diverge."""
	return _call_view(_scoped_call(name, mp, is_sysmgr))


# -- discovery ---------------------------------------------------------------

@frappe.whitelist(methods=["GET"])
@_api(read=True)
def call_schema(**_kwargs):
	"""Discovery: the call payload contract — every field a caller may send, its type, and whether it
	is required. The shape is fixed (it does not vary by partner or grain), but it is discoverable, so
	an integrator never hardcodes a field list."""
	_resolve_caller()
	_schema_ok(
		"call",
		dedup=(
			"None. Every POST creates a new call log and returns a new `name`. Retries are made safe "
			"with the Idempotency-Key header; `external_id` does not deduplicate."
		),
		fields=describe(CALL_FIELDS, "CRM Call Log"),
		attribution=(
			"`lead` or `mobile_no` attaches the call explicitly, scoped to the caller's line. When both "
			"are omitted, the customer number is strict-matched within the line (from_number on Inbound, "
			"to_number on Outbound). No match, or an ambiguous match, leaves the call unlinked and `lead` "
			"reads null — a call is never attached to a wrong lead."
		),
	)


# -- singular endpoints ------------------------------------------------------

@frappe.whitelist(methods=["GET"])
@_api(read=True)
def call_get(**_kwargs):
	"""Read one call by `name`, grain-scoped (own line only). Out-of-scope/missing ->
	the SAME generic not-found."""
	_user, mp, is_sysmgr = _resolve_caller()
	_ok(action=ACTION_FETCHED, data=_read_one(frappe.form_dict.get("name"), mp, is_sysmgr))


@frappe.whitelist(methods=["POST"])
@_api
def call_create(**_kwargs):
	"""Create a call log. Body: {lead|mobile_no?, external_id?, direction (Inbound/Outbound),
	from_number, to_number, status, duration, recording_url?, started_at?}. Returns the `name` to
	address the call by from now on."""
	_user, mp, is_sysmgr = _resolve_caller()
	view, action = _create_one(frappe.form_dict, mp, is_sysmgr)
	_ok(action=action, data=view)


@frappe.whitelist(methods=["PUT"])
@_api
def call_update(**_kwargs):
	"""Update a call by `name`. Only the fields present in the body change. Scope-checked."""
	_user, mp, is_sysmgr = _resolve_caller()
	view, action = _update_one(frappe.form_dict.get("name"), frappe.form_dict, mp, is_sysmgr)
	_ok(action=action, data=view)


@frappe.whitelist(methods=["DELETE"])
@_api
def call_delete(**_kwargs):
	"""Delete one call by `name`, scope-checked (own line only). Out-of-scope/missing ->
	the SAME generic not-found."""
	_user, mp, is_sysmgr = _resolve_caller()
	name = frappe.form_dict.get("name")
	_delete_one(name, mp, is_sysmgr)
	_ok(action=ACTION_DELETED, data={"name": name})


# -- bulk / query endpoints --------------------------------------------------

@frappe.whitelist(methods=["POST"])
@_api(bulk=True, read=True)
def call_get_bulk(**_kwargs):
	"""Read many calls by `names` (<= 100). Input-ordered; out-of-scope/unknown names are
	reported not_found in place."""
	_user, mp, is_sysmgr = _resolve_caller()
	names = _read_required_list(frappe.form_dict, "names")
	return _bulk_read(names, lambda name: _read_one(name, mp, is_sysmgr))


@frappe.whitelist(methods=["POST"])
@_api(bulk=True)
def call_create_bulk(**_kwargs):
	"""Create many call logs. Body: {"calls":[{...}, ...]} (<= 100). Each record is enforced in its
	own savepoint -> partial success."""
	_user, mp, is_sysmgr = _resolve_caller()
	calls = _read_required_list(frappe.form_dict, "calls")

	def one(i, item):
		view, action = _create_one(item, mp, is_sysmgr)
		return {"index": i, "status": "success", "action": action, "data": view}

	return _run_bulk(calls, one)


@frappe.whitelist(methods=["PUT"])
@_api(bulk=True)
def call_update_bulk(**_kwargs):
	"""Update many calls. Body: {"updates":[{"name":.., ...}, ...]} (<= 100). Partial success."""
	_user, mp, is_sysmgr = _resolve_caller()
	updates = _read_required_list(frappe.form_dict, "updates")

	def one(i, item):
		view, action = _update_one((item or {}).get("name"), item, mp, is_sysmgr)
		return {"index": i, "status": "success", "action": action, "data": view}

	return _run_bulk(updates, one)


@frappe.whitelist(methods=["DELETE"])
@_api(bulk=True)
def call_delete_bulk(**_kwargs):
	"""Delete many calls. Body: {"names":[...]} (<= 100). Partial success."""
	_user, mp, is_sysmgr = _resolve_caller()
	names = _read_required_list(frappe.form_dict, "names")

	def one(i, name):
		_delete_one(name, mp, is_sysmgr)
		return {"index": i, "status": "success", "action": ACTION_DELETED, "data": {"name": name}}

	return _run_bulk(names, one)


@frappe.whitelist(methods=["GET"])
@_api(bulk=True, read=True)
def call_list(**_kwargs):
	"""List a lead's calls, paginated. Query: lead|mobile_no (grain-scoped), optional
	direction (Inbound/Outbound) / status, limit (<=200, default 20), offset."""
	_user, mp, is_sysmgr = _resolve_caller()
	data = frappe.form_dict
	lead = resolve_lead(mp, is_sysmgr, data)

	filters = {"reference_doctype": "CRM Lead", "reference_docname": lead}
	direction = data.get("direction")
	if direction:
		if direction not in _DIRECTION_TYPE:
			frappe.throw(_("direction must be Inbound or Outbound"))
		filters["type"] = _DIRECTION_TYPE[direction]
	if data.get("status"):
		filters["status"] = data.get("status")

	limit, offset = _page(data)
	total = frappe.db.count("CRM Call Log", filters)
	rows = frappe.get_all(
		"CRM Call Log", filters=filters,
		fields=["name", EXTERNAL_ID_FIELD, "reference_doctype", "reference_docname", "type",
		        "from", "to", "status", "duration", "recording_url", "start_time"],
		limit_page_length=limit, limit_start=offset, order_by="creation desc",
	)
	_list_ok("calls", [_call_view(frappe._dict(r)) for r in rows], total, offset, limit)
