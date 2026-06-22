"""Gated partner CALL API — create / read / list / delete call logs on a lead.

Shares the ONE brain in `tatva_connect.api._base`: the SAME `_resolve_caller`
enablement gate (the single enabled `CRM Lead API Mapping` row + its grain governs
calls just like leads/activities/files — there is NO per-entity enablement), the SAME
`resolve_lead` grain-scoped resolver, the SAME `_ok`/`_fail` envelope, error codes,
rate limit (100 req/60s) and `_run_bulk` partial-success engine.

Calls land in frappe/crm's native `CRM Call Log` — the SAME doctype the Acefone webhook
adapter writes (`tatva_connect.telephony.adapter`). No parallel call store.

`external_id` is the idempotency key, stored in `custom_external_id` (Data, indexed):
re-sending the same external_id updates that call log, never doubling. The CRM Call Log
autoname is `field:id`, so the partner-facing `external_id` is NOT the row name — we mint
a synthetic `id` on insert and dedup on `custom_external_id`, exactly as the foundation
field's description prescribes.

Lead attribution (Invariant #16 — NO best-guess):
  * `lead`/`mobile_no` given -> the shared grain-scoped `resolve_lead`.
  * else -> STRICT last-10 phone match on the customer number, SCOPED to the caller's
    grain (vertical+group). Exactly one match links; no match or ambiguous (2+) -> leave
    UNLINKED (note `unlinked`), never attach to the wrong lead.

  POST   call_create       -> create-or-upsert a call log (dedup on external_id)
  GET    call_get          -> one call by `name`, grain-scoped, generic not-found
  GET    call_list         -> a lead's calls (+ direction/status), paginated
  DELETE call_delete       -> delete a call by `name`, scope-checked
  POST   call_create_bulk  -> {"calls":[...]} (<= 100), partial success
"""
import frappe
from frappe import _
from frappe.utils import cint, get_datetime

from tatva_connect.api._base import (  # noqa: F401
	BULK_MAX,
	_api,
	_fail,  # noqa: F401  (kept available for symmetry with the sibling modules)
	_norm_phone,
	_ok,
	_read_list,
	_resolve_caller,
	_run_bulk,
	resolve_lead,
)

# Pagination (mirrors the sibling list contract; BULK_MAX is the shared per-call cap).
LIST_DEFAULT = 20       # default page size
LIST_MAX = 200          # max page size

DEDUP_FIELD = "custom_external_id"   # the idempotency key column on CRM Call Log

# Partner direction vocab -> CRM Call Log `type` vocab.
_DIRECTION_TYPE = {"Inbound": "Incoming", "Outbound": "Outgoing"}


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
		"external_id": doc.get(DEDUP_FIELD),
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


def _find_by_external_id(external_id):
	"""The CRM Call Log name carrying this external_id (the dedup key), or None."""
	if not external_id:
		return None
	return frappe.db.get_value("CRM Call Log", {DEDUP_FIELD: external_id}, "name")


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


def _upsert_one(data, mp, is_sysmgr):
	"""Create-or-upsert ONE call log. Deduped on external_id. Returns (call_view, action).

	external_id already on a row -> update THAT row (re-send is idempotent). New external_id
	-> insert a fresh row with a synthetic `id` (the autoname field). Backdate creation from
	`started_at` on insert."""
	external_id = data.get("external_id")
	if not external_id:
		frappe.throw(_("external_id is required"))
	direction = data.get("direction")
	if not direction or direction not in _DIRECTION_TYPE:
		frappe.throw(_("direction (Inbound or Outbound) is required"))

	lead_name = _attribute_lead(data, mp, is_sysmgr)

	existing = _find_by_external_id(external_id)
	if existing:
		doc = frappe.get_doc("CRM Call Log", existing)
		_apply_fields(doc, data, lead_name)
		doc.save(ignore_permissions=True)
		return _call_view(doc), "updated"

	doc = frappe.new_doc("CRM Call Log")
	# The autoname is field:id, so the row NAME is `id`, NOT the dedup key. Mint a stable
	# synthetic id (partner: prefix keeps it clear of provider call ids) and dedup on
	# custom_external_id, exactly as the foundation field prescribes.
	doc.id = "PARTNER-{0}".format(external_id)
	doc.set(DEDUP_FIELD, external_id)
	# Sensible required-field floors so a sparse payload still inserts (status defaults New-
	# equivalent "Completed" only if the caller sent none; from/to default to empty strings).
	doc.status = data.get("status") or "Completed"
	setattr(doc, "from", "")
	doc.to = ""
	_apply_fields(doc, data, lead_name)
	doc.insert(ignore_permissions=True)
	# Backdate creation from started_at (historical load), mirroring the activity API.
	if data.get("started_at"):
		dt = get_datetime(data.get("started_at"))
		if dt:
			frappe.db.set_value("CRM Call Log", doc.name, "creation", dt, update_modified=False)
	return _call_view(doc), "created"


# -- endpoints ---------------------------------------------------------------

@frappe.whitelist(methods=["POST"])
@_api
def call_create(**kwargs):
	"""Create-or-upsert a call log. Body: {lead|mobile_no, external_id, direction
	(Inbound/Outbound), from_number, to_number, status, duration, recording_url?,
	started_at?}. Deduped on external_id; re-send updates the same row, never doubles.
	Lead resolution is grain-scoped (lead/mobile_no) OR strict phone+grain match on the
	customer number — no/ambiguous match leaves the call UNLINKED (never a wrong lead)."""
	user, mp, is_sysmgr = _resolve_caller()
	view, action = _upsert_one(frappe.form_dict, mp, is_sysmgr)
	_ok(action=action, data=view)


@frappe.whitelist(methods=["GET"])
@_api
def call_get(**kwargs):
	"""Read one call by `name`, grain-scoped (own line only). Out-of-scope/missing ->
	the SAME generic not-found."""
	user, mp, is_sysmgr = _resolve_caller()
	doc = _scoped_call(frappe.form_dict.get("name"), mp, is_sysmgr)
	_ok(action="fetched", data=_call_view(doc))


@frappe.whitelist(methods=["GET"])
@_api
def call_list(**kwargs):
	"""List a lead's calls, paginated. Query: lead|mobile_no (grain-scoped), optional
	direction (Inbound/Outbound) / status, limit (<=200, default 20), offset. Returns
	{total, count, offset, limit, has_more, calls:[...]}."""
	user, mp, is_sysmgr = _resolve_caller()
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

	limit = min(cint(data.get("limit")) or LIST_DEFAULT, LIST_MAX)
	offset = cint(data.get("offset") or data.get("limit_start"))

	total = frappe.db.count("CRM Call Log", filters)
	rows = frappe.get_all(
		"CRM Call Log", filters=filters,
		fields=["name", DEDUP_FIELD, "reference_docname", "type", "from", "to",
		        "status", "duration", "recording_url", "start_time"],
		limit_page_length=limit, limit_start=offset, order_by="creation desc",
	)
	calls = [{
		"name": r.name,
		"external_id": r.get(DEDUP_FIELD),
		"lead": r.reference_docname,
		"direction": "Inbound" if r.type == "Incoming" else "Outbound",
		"from_number": r.get("from"),
		"to_number": r.to,
		"status": r.status,
		"duration": r.duration,
		"recording_url": r.recording_url,
		"start_time": str(r.start_time) if r.start_time else None,
	} for r in rows]
	_ok(action="fetched", data={
		"total": total, "count": len(calls), "offset": offset, "limit": limit,
		"has_more": (offset + len(calls)) < total, "calls": calls,
	})


@frappe.whitelist(methods=["DELETE"])
@_api
def call_delete(**kwargs):
	"""Delete one call by `name`, scope-checked (own line only). Out-of-scope/missing ->
	the SAME generic not-found."""
	user, mp, is_sysmgr = _resolve_caller()
	name = frappe.form_dict.get("name")
	doc = _scoped_call(name, mp, is_sysmgr)
	frappe.delete_doc("CRM Call Log", doc.name, ignore_permissions=True)
	_ok(action="deleted", data={"name": name})


@frappe.whitelist(methods=["POST"])
@_api
def call_create_bulk(**kwargs):
	"""Create-or-upsert many call logs. Body: {"calls":[{...}, ...]} (<= 100). Each record
	is enforced in its own savepoint -> partial success; each is idempotent on its own
	external_id."""
	user, mp, is_sysmgr = _resolve_caller()
	calls = _read_list(frappe.form_dict, "calls") or []

	def one(i, item):
		view, action = _upsert_one(item, mp, is_sysmgr)
		return {"index": i, "status": "success", "action": action,
				"name": view["name"], "external_id": view["external_id"]}

	return _run_bulk(calls, one)
