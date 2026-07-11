"""Gated partner CALL API — create / read / update / list / delete call logs on a lead.

Shares the ONE brain in `tatva_connect.api._base`: the SAME `_resolve_caller` enablement gate (the
single enabled `CRM Lead API Mapping` row + its grain governs calls just like leads/activities/files —
there is NO per-entity enablement), the SAME `resolve_lead` grain-scoped resolver, the SAME `_ok`/`_fail`
envelope, error codes, rate limit, `_run_bulk` / `_bulk_read` partial-success engines and `_list_ok`
list envelope.

Calls land in frappe/crm's native `CRM Call Log` — the SAME doctype the Acefone webhook adapter writes
(`tatva_connect.telephony.adapter`). No parallel call store.

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
	_resolve_caller,
	_run_bulk,
	_schema_ok,
	field_descriptor,
	resolve_lead,
	stamp_external_id,
)

# All numeric caps (bulk size, list page sizes) come from the CRM Partner API Settings
# Single via _cfg() — one source of truth, no module-local copy.

# Partner direction vocab -> CRM Call Log `type` vocab.
_DIRECTION_TYPE = {"Inbound": "Incoming", "Outbound": "Outgoing"}

# The call's payload contract — the ONE source of truth for what a caller may send, what it maps to on
# CRM Call Log, and what `call_schema` advertises. Discovery equals ingestion because both read THIS.
#   partner fieldname -> (label, CRM Call Log fieldname or None, required)
CALL_FIELDS = (
	("lead",          "Lead",          "reference_docname", False),
	("mobile_no",     "Mobile No",     None,                False),
	("external_id",   "External ID",   EXTERNAL_ID_FIELD,   False),
	("direction",     "Direction",     "type",              True),
	("from_number",   "From Number",   "from",              False),
	("to_number",     "To Number",     "to",                False),
	("status",        "Status",        "status",            False),
	("duration",      "Duration",      "duration",          False),
	("recording_url", "Recording URL", "recording_url",     False),
	("started_at",    "Started At",    "start_time",        False),
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
	# A call the caller can see is one whose lead is on their line. An UNLINKED call (no
	# lead) is never visible to a partner — only a trusted sysmgr (no mapping) sees it.
	if mp:
		if doc.reference_doctype != "CRM Lead" or not doc.reference_docname:
			frappe.throw(_("Call not found"), frappe.DoesNotExistError)
		try:
			resolve_lead(mp, is_sysmgr, {"lead": doc.reference_docname})
		except frappe.DoesNotExistError:
			frappe.throw(_("Call not found"), frappe.DoesNotExistError)
	return doc


def _apply_fields(doc, data, lead_name):
	"""Overlay the partner payload onto a CRM Call Log doc (create or update path)."""
	direction = data.get("direction")
	if direction and direction not in _DIRECTION_TYPE:
		frappe.throw(_("direction must be Inbound or Outbound"))
	if direction:
		doc.type = _DIRECTION_TYPE[direction]

	if data.get("from_number") is not None:
		setattr(doc, "from", str(data.get("from_number") or ""))
	if data.get("to_number") is not None:
		doc.to = str(data.get("to_number") or "")
	if data.get("status"):
		doc.status = data.get("status")
	if data.get("duration") not in (None, ""):
		doc.duration = cint(data.get("duration"))
	if data.get("recording_url") is not None:
		doc.recording_url = data.get("recording_url")
	if data.get("started_at"):
		dt = get_datetime(data.get("started_at"))
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
	"""Create ONE call log. Returns (call_view, "created").

	A create CREATES: there is no upsert on a caller-supplied key. `external_id`, if sent, is stamped
	as a label and nothing more. A caller that re-POSTs the same call gets a second call log — that is
	correct, and the Idempotency-Key header is how a retry is made safe."""
	direction = data.get("direction")
	if not direction or direction not in _DIRECTION_TYPE:
		frappe.throw(_("direction (Inbound or Outbound) is required"))

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
	doc.insert(ignore_permissions=True)

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
	# Re-attribution is allowed only when the caller explicitly names a lead; a payload that omits
	# lead/mobile_no leaves the existing link alone (it never silently re-attributes by phone).
	lead_name = resolve_lead(mp, is_sysmgr, data) if (data.get("lead") or data.get("mobile_no")) else None
	_apply_fields(doc, data, lead_name)
	doc.save(ignore_permissions=True)
	if data.get("external_id") is not None:
		stamp_external_id("CRM Call Log", doc.name, data.get("external_id"))
	return _call_view(frappe.get_doc("CRM Call Log", doc.name)), ACTION_UPDATED


def _delete_one(name, mp, is_sysmgr):
	"""Delete one call by `name`, scope-checked (generic not-found)."""
	doc = _scoped_call(name, mp, is_sysmgr)
	frappe.delete_doc("CRM Call Log", doc.name, ignore_permissions=True)


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
	m = frappe.get_meta("CRM Call Log")
	fields = []
	for fieldname, label, crm_field, required in CALL_FIELDS:
		f = m.get_field(crm_field) if crm_field else None
		fieldtype = f.fieldtype if f else "Data"
		options, allowed = (f.options if f else None), None
		if fieldname == "direction":
			# The partner vocabulary is Inbound/Outbound; the native column is Incoming/Outgoing.
			fieldtype, options, allowed = "Select", None, list(_DIRECTION_TYPE)
		elif fieldname == "status" and f:
			allowed = [o for o in (f.options or "").split("\n") if o] or None
		fields.append(field_descriptor(fieldname, label, fieldtype, required, options, allowed))
	_schema_ok(
		"call",
		dedup=(
			"None. Every POST creates a new call log and returns a new `name`. Retries are made safe "
			"with the Idempotency-Key header; `external_id` does not deduplicate."
		),
		fields=fields,
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
	names = _read_list(frappe.form_dict, "names")
	if not names:
		frappe.throw(_("names is required"))
	return _bulk_read(names, lambda name: _read_one(name, mp, is_sysmgr))


@frappe.whitelist(methods=["POST"])
@_api(bulk=True)
def call_create_bulk(**_kwargs):
	"""Create many call logs. Body: {"calls":[{...}, ...]} (<= 100). Each record is enforced in its
	own savepoint -> partial success."""
	_user, mp, is_sysmgr = _resolve_caller()
	calls = _read_list(frappe.form_dict, "calls") or []

	def one(i, item):
		view, action = _create_one(item, mp, is_sysmgr)
		return {"index": i, "status": "success", "action": action, "data": view}

	return _run_bulk(calls, one)


@frappe.whitelist(methods=["PUT"])
@_api(bulk=True)
def call_update_bulk(**_kwargs):
	"""Update many calls. Body: {"updates":[{"name":.., ...}, ...]} (<= 100). Partial success."""
	_user, mp, is_sysmgr = _resolve_caller()
	updates = _read_list(frappe.form_dict, "updates") or []

	def one(i, item):
		view, action = _update_one((item or {}).get("name"), item, mp, is_sysmgr)
		return {"index": i, "status": "success", "action": action, "data": view}

	return _run_bulk(updates, one)


@frappe.whitelist(methods=["DELETE"])
@_api(bulk=True)
def call_delete_bulk(**_kwargs):
	"""Delete many calls. Body: {"names":[...]} (<= 100). Partial success."""
	_user, mp, is_sysmgr = _resolve_caller()
	names = _read_list(frappe.form_dict, "names") or []

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
