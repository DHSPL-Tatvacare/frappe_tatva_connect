"""Acefone webhook ingest + outbound click-to-call.

Adapted from sanskar-onehash/crm_acefone_integration (MIT).

Unlike the OneHash app (which writes a parallel `Acefone Call Log`), we write
to frappe/crm's NATIVE `CRM Call Log` so calls render in the lead's Calls tab
with zero extra glue. Two seams:

  * Inbound/outbound CDR webhooks  -> create/update a CRM Call Log (idempotent
    on uuid|call_id), resolve the lead by customer number, link it.
  * make_acefone_call(...)         -> create an Initiated Outgoing row, fire
    click-to-call carrying the row name as custom_identifier for correlation.

Acefone registers a SEPARATE webhook URL per trigger, so we expose four thin
guest endpoints; each validates ?key=, honours the kill-switch, and delegates
to the shared `_process`. Handlers always return "ok" fast and log on failure
(Acefone retries non-2xx).

Register the URLs on Acefone as (one per trigger):
    https://<host>/api/method/tatva_connect.acefone.handler.inbound_answered?key=<token>
    https://<host>/api/method/tatva_connect.acefone.handler.inbound_complete?key=<token>
    https://<host>/api/method/tatva_connect.acefone.handler.outbound_answered?key=<token>
    https://<host>/api/method/tatva_connect.acefone.handler.outbound_complete?key=<token>
where <token> == Acefone Settings.webhook_verify_token.

CRITICAL seam note: the lead's Calls-tab feed (crm.api.activities.get_linked_calls)
filters primarily on `reference_docname`, but crm's Exotel handler only fills the
`links` child table. So we set BOTH reference_doctype/reference_docname AND call
link_with_reference_doc(...).
"""
import frappe
from frappe import _

from tatva_connect.acefone import api as acefone
from tatva_connect.acefone import routing

SETTINGS = "Acefone Settings"
TELEPHONY_MEDIUM = "Acefone"

# Map (call_status, completed?) -> CRM Call Log status. Acefone gives
# call_status in {"Answered","Missed"} on CDRs; the answered-but-not-complete
# trigger lands a call In Progress. Unknown/failure hangup_causes are handled
# in _status_from_cdr below.
_STATUS_ANSWERED_LIVE = "In Progress"
_STATUS_ANSWERED_DONE = "Completed"
_STATUS_MISSED = "No Answer"

# How recent an Initiated Outgoing row may be to count as the same call when
# custom_identifier did not round-trip (number + recency fallback).
_OUTBOUND_MATCH_WINDOW_MIN = 5


# ---------------------------------------------------------------------------
# Guest webhook endpoints (one per Acefone trigger)
# ---------------------------------------------------------------------------
@frappe.whitelist(allow_guest=True)
def inbound_answered(**kwargs):
	return _entry(direction="inbound", completed=False)


@frappe.whitelist(allow_guest=True)
def inbound_complete(**kwargs):
	return _entry(direction="inbound", completed=True)


@frappe.whitelist(allow_guest=True)
def outbound_answered(**kwargs):
	return _entry(direction="outbound", completed=False)


@frappe.whitelist(allow_guest=True)
def outbound_complete(**kwargs):
	return _entry(direction="outbound", completed=True)


def _entry(direction: str, completed: bool):
	"""Shared front door: verify token, kill-switch, then process. Always 'ok'."""
	if not acefone.is_enabled():
		return "ok"
	if not _valid_key():
		raise frappe.PermissionError("Invalid Acefone webhook key")

	payload = {k: v for k, v in frappe.form_dict.items() if k not in ("cmd", "key")}
	try:
		_process(payload, direction=direction, completed=completed)
	except Exception:
		frappe.db.rollback()
		frappe.log_error(
			title="Acefone webhook: processing failed",
			message=frappe.get_traceback(),
		)
	return "ok"


def _valid_key() -> bool:
	expected = frappe.db.get_single_value(SETTINGS, "webhook_verify_token")
	if not expected:
		# No token configured -> accept (mirrors crm Exotel's "key and key==token"
		# being false; but here we let an un-configured install pass so a first
		# capture can be inspected). Operators SHOULD set a token in prod.
		return True
	key = frappe.request.args.get("key") if frappe.request else None
	key = key or frappe.form_dict.get("key")
	return key == expected


# ---------------------------------------------------------------------------
# CDR -> CRM Call Log
# ---------------------------------------------------------------------------
def _process(payload: dict, direction: str, completed: bool):
	"""Create or update one CRM Call Log row from an Acefone CDR payload."""
	frappe.publish_realtime("acefone_call", payload)

	call_id = payload.get("uuid") or payload.get("call_id")
	customer_number = (
		payload.get("customer_number")
		or payload.get("customer_number_with_prefix")
		or payload.get("customer_phone")
	)
	call_type = "Incoming" if direction == "inbound" else "Outgoing"
	status = _status_from_cdr(payload, completed)

	# Best-effort per-tenant attribution: match the CDR's DID to an Acefone
	# Account. Never block logging if it doesn't resolve.
	account_name = None
	try:
		account_name = routing.account_for_did(payload.get("did_number"))
	except Exception:
		frappe.log_error(title="Acefone: DID -> account match failed", message=frappe.get_traceback())

	doc = _find_existing(call_id, direction, customer_number, payload)
	if doc:
		_apply(doc, payload, status=status, call_type=call_type, customer_number=customer_number)
		if account_name:
			doc.custom_acefone_account = account_name
		doc.save(ignore_permissions=True)
	else:
		doc = frappe.new_doc("CRM Call Log")
		doc.id = call_id
		doc.type = call_type
		doc.telephony_medium = TELEPHONY_MEDIUM
		_apply(doc, payload, status=status, call_type=call_type, customer_number=customer_number)
		if account_name:
			doc.custom_acefone_account = account_name
		doc.insert(ignore_permissions=True)

	frappe.db.commit()


def _status_from_cdr(payload: dict, completed: bool) -> str:
	"""Map an Acefone CDR to the CRM Call Log status vocabulary.

	Vocab: Initiated/Ringing/In Progress/Completed/Failed/Busy/No Answer/Queued/Canceled.
	Acefone primary signal is `call_status` in {"Answered","Missed"}; refine with
	`hangup_cause` for terminal states when present.
	"""
	call_status = (payload.get("call_status") or "").strip().lower()
	hangup = (payload.get("hangup_cause") or "").strip().lower()

	if call_status == "missed":
		# Distinguish busy/cancel where Acefone tells us via hangup_cause.
		if "busy" in hangup:
			return "Busy"
		if "cancel" in hangup or "originator_cancel" in hangup:
			return "Canceled"
		return _STATUS_MISSED

	if call_status == "answered":
		return _STATUS_ANSWERED_DONE if completed else _STATUS_ANSWERED_LIVE

	# call_status absent/unknown: fall back on completion + hangup.
	if completed:
		if "normal" in hangup or "clearing" in hangup:
			return "Completed"
		if "busy" in hangup:
			return "Busy"
		if "no_answer" in hangup or "noanswer" in hangup:
			return "No Answer"
		if "cancel" in hangup:
			return "Canceled"
		return "Failed"
	return "In Progress"


def _apply(doc, payload: dict, status: str, call_type: str, customer_number):
	"""Overlay CDR fields onto a CRM Call Log doc (create or update path)."""
	doc.status = status

	did = payload.get("did_number")
	caller_id = payload.get("caller_id")
	# Incoming: customer -> our DID. Outgoing: our DID/caller_id -> customer.
	if call_type == "Incoming":
		setattr(doc, "from", str(customer_number or caller_id or ""))
		doc.to = str(did or "")
	else:
		setattr(doc, "from", str(did or caller_id or ""))
		doc.to = str(customer_number or "")

	doc.medium = str(did or "") or doc.medium

	if payload.get("duration") not in (None, ""):
		doc.duration = _to_int(payload.get("duration"))
	if payload.get("recording_url"):
		doc.recording_url = payload.get("recording_url")

	start = _parse_dt(payload.get("start_stamp"))
	end = _parse_dt(payload.get("end_stamp"))
	if start:
		doc.start_time = start
	if end:
		doc.end_time = end

	# Agent -> Frappe user (CRM Telephony Agent.acefone_number).
	agent_user = _user_for_agent_number(payload.get("answered_agent_number"))
	if agent_user:
		if call_type == "Incoming":
			doc.receiver = agent_user
		else:
			doc.caller = agent_user

	# Resolve + link the lead (set BOTH reference_* and the links child table).
	_link_lead(doc, customer_number)


def _link_lead(doc, customer_number):
	"""Resolve customer number -> Contact/Lead/Deal and link the call log.

	Mirrors crm Exotel's link(): get_contact_by_phone_number returns a dict; a
	["lead"]/["deal"] key promotes the doctype. Does NOT auto-create. We set the
	reference_* pair (for the Calls-tab feed) AND link_with_reference_doc (parity
	with how crm stores call links).
	"""
	if not customer_number:
		return
	try:
		from crm.integrations.api import get_contact_by_phone_number

		contact = get_contact_by_phone_number(str(customer_number))
	except Exception:
		frappe.log_error(title="Acefone: contact lookup failed", message=frappe.get_traceback())
		return

	if not contact or not contact.get("name"):
		return

	doctype, docname = "Contact", contact.get("name")
	if contact.get("lead"):
		doctype, docname = "CRM Lead", contact.get("lead")
	elif contact.get("deal"):
		doctype, docname = "CRM Deal", contact.get("deal")

	doc.reference_doctype = doctype
	doc.reference_docname = docname
	doc.link_with_reference_doc(doctype, docname)


def _find_existing(call_id, direction, customer_number, payload):
	"""Locate the row this CDR updates.

	1. Same id (uuid|call_id) already stored -> update it.
	2. Outbound: custom_identifier matches a CRM Call Log name -> that row.
	3. Outbound fallback: most recent Initiated Acefone Outgoing row to this
	   number within the match window.
	Otherwise None (a fresh row will be created).
	"""
	if call_id and frappe.db.exists("CRM Call Log", call_id):
		return frappe.get_doc("CRM Call Log", call_id)

	if direction == "outbound":
		ident = payload.get("custom_identifier")
		if ident and frappe.db.exists("CRM Call Log", ident):
			return frappe.get_doc("CRM Call Log", ident)

		match = _recent_initiated_outbound(customer_number)
		if match:
			return frappe.get_doc("CRM Call Log", match)

	return None


def _recent_initiated_outbound(customer_number):
	"""Fallback correlation: latest Initiated Acefone Outgoing row to a number."""
	digits = acefone.normalize_number(customer_number)
	if not digits:
		return None
	cutoff = frappe.utils.add_to_date(frappe.utils.now_datetime(), minutes=-_OUTBOUND_MATCH_WINDOW_MIN)
	rows = frappe.get_all(
		"CRM Call Log",
		filters={
			"telephony_medium": TELEPHONY_MEDIUM,
			"type": "Outgoing",
			"status": "Initiated",
			"to": ["like", f"%{digits[-10:]}%"],
			"creation": [">=", cutoff],
		},
		order_by="creation desc",
		limit=1,
		pluck="name",
	)
	return rows[0] if rows else None


def _user_for_agent_number(agent_number):
	"""Map an Acefone agent number -> the Frappe user via CRM Telephony Agent."""
	digits = acefone.normalize_number(agent_number)
	if not digits:
		return None
	return frappe.db.get_value(
		"CRM Telephony Agent", {"acefone_number": ["like", f"%{digits[-10:]}%"]}, "user"
	)


# ---------------------------------------------------------------------------
# Outbound (click-to-call)
# ---------------------------------------------------------------------------
@frappe.whitelist()
def make_acefone_call(reference_doctype: str, reference_name: str):
	"""Programmatic outbound call to a lead/deal (API entry point).

	The interactive path is the native phone icon, which goes through
	`bridge.make_a_call`. This thin wrapper keeps a by-reference API: it
	permission-checks the record, resolves its number, and delegates to the same
	single outbound core (routing, call log, click-to-call) in `bridge`.
	"""
	from tatva_connect.acefone import bridge

	if not frappe.has_permission(reference_doctype, "read", reference_name):
		frappe.throw(_("Not permitted to call from this record."), frappe.PermissionError)
	destination = _destination_for(reference_doctype, reference_name)
	if not destination:
		frappe.throw(_("No phone number found on {0}.").format(reference_name))
	return bridge.make_a_call(destination)


def _destination_for(reference_doctype: str, reference_name: str):
	"""Resolve the customer phone number for the reference record."""
	field = "mobile_no"
	if reference_doctype in ("CRM Lead", "CRM Deal", "Contact"):
		return frappe.db.get_value(reference_doctype, reference_name, field)
	# Unknown doctype: try mobile_no, fall back to phone.
	meta = frappe.get_meta(reference_doctype)
	for f in ("mobile_no", "phone", "phone_no"):
		if meta.has_field(f):
			return frappe.db.get_value(reference_doctype, reference_name, f)
	return None


# ---------------------------------------------------------------------------
# Defensive parsing helpers
# ---------------------------------------------------------------------------
def _to_int(value) -> int:
	try:
		return int(float(value))
	except (TypeError, ValueError):
		return 0


def _parse_dt(value):
	"""Parse an Acefone timestamp defensively.

	Acefone sends 'YYYY-MM-DD HH:MM:SS' by default but the format may be
	configurable or epoch seconds. Return a value frappe can store, or None.
	"""
	if value in (None, "", "0"):
		return None
	# Epoch seconds (all digits, plausible range).
	s = str(value).strip()
	if s.isdigit() and len(s) >= 9:
		try:
			from datetime import datetime, timezone

			return datetime.fromtimestamp(int(s), tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
		except Exception:
			return None
	try:
		return frappe.utils.get_datetime(s)
	except Exception:
		frappe.log_error(title="Acefone: unparseable timestamp", message=s)
		return None
