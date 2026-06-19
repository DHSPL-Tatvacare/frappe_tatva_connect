"""Acefone webhook adapter — the per-vendor brains behind the shared spine.

The spine (`tatva_connect.webhooks.spine`) owns the front door (kill-switch, auth,
raw-log, fast-ACK, enqueue) and the worker dispatch; this module owns ONLY the
Acefone-specific payload parsing and the clean DB moves into frappe/crm's native
`CRM Call Log`. The brains moved here verbatim from `handler.py` — same idempotency
(call_id|uuid), same status mapping, same fail-closed lead attribution.

Adapter contract (duck-typed, no ABC):
  * is_relevant(payload, event, account)      -> cheap front-door pre-filter (inline)
  * already_processed(payload, event, account) -> idempotency vs CRM Call Log (worker)
  * handle(payload, event, account)            -> parse + DB moves + attribution (worker)
  * account_for_payload(payload, event)        -> re-derive the receiving account on replay

The `event` is the URL trigger segment the spine carries through: one of
"inbound_answered" / "inbound_complete" / "outbound_answered" / "outbound_complete".
Direction (inbound/outbound) and completion (answered-live vs hangup-complete) are both
recovered from it, so `_status_from_cdr` behaves exactly as before.

CRITICAL — swallow-as-200 is FIXED here: there is NO try/except that rolls back, logs
and returns "ok". `handle` runs in the spine worker; any failure propagates to the RQ
failed registry (the DLQ) and is replayable, instead of being lost behind a 200.

Field map (operator-configured Acefone dashboard body; no live sample yet, so each axis
has a fallback chain — see the plan's Acefone section):
  * call_id (stable across triggers) -> id          fallback: uuid
  * answered_agent_email -> user                     fallbacks: answered_agent_name,
    answered_agent_number -> CRM Telephony Agent.acefone_number -> user
  * caller_id_number -> from (customer)              fallbacks: customer_number,
    customer_number_with_prefix, customer_phone, caller_id
  * call_to_number / did_number -> to / DID
  * recording_url -> recording_url
  * start_stamp -> start_time   end_stamp -> end_time   duration -> duration
  * call_status (Answered/Missed) + hangup_cause -> status

CRITICAL seam note (unchanged): the lead's Calls-tab feed
(crm.api.activities.get_linked_calls) filters primarily on `reference_docname`, but crm's
Exotel handler only fills the `links` child table. So we set BOTH reference_doctype/
reference_docname AND call link_with_reference_doc(...).
"""
import frappe

from tatva_connect.telephony import api as acefone
from tatva_connect.telephony import routing

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
# Adapter contract
# ---------------------------------------------------------------------------
def is_relevant(payload, event, account) -> bool:
	"""Cheap front-door pre-filter (runs inline before enqueue). Every Acefone hangup CDR
	is relevant — it always becomes/updates a CRM Call Log row — provided we can key it."""
	return bool(payload.get("call_id") or payload.get("uuid"))


def already_processed(payload, event, account) -> bool:
	"""Idempotency vs the target doctype (runs in the worker). True only when a COMPLETED
	row already exists for this call_id|uuid — an earlier answered-live trigger leaves an
	In Progress row that the complete trigger must still update, so we never short-circuit
	those. Keyed on call_id (stable across triggers), uuid fallback.

	Mirrors the original _find_existing's id check; the answered->complete update path is
	preserved by routing through handle() whenever the row isn't already Completed."""
	call_id = payload.get("call_id") or payload.get("uuid")
	if not call_id:
		return False
	status = frappe.db.get_value("CRM Call Log", call_id, "status")
	return status == _STATUS_ANSWERED_DONE


def handle(payload, event, account) -> None:
	"""Parse one Acefone CDR + apply the clean DB moves into CRM Call Log. Runs in the
	worker; exceptions propagate to the RQ failed registry (DLQ). No swallow-as-200."""
	direction, completed = _direction_completed(event)
	_process(payload, direction=direction, completed=completed, account_name=account)


def account_for_payload(payload, event):
	"""Re-derive the receiving account from a STORED payload, for spine.replay() (which has
	no live request token). Match the CDR's DID to a Telephony Account; None if no match
	(the spine then passes account=None and attribution fails closed)."""
	try:
		return routing.account_for_did(payload.get("did_number"))
	except Exception:
		return None


def _direction_completed(event):
	"""Recover (direction, completed) from the URL trigger segment the spine carried.

	event in {inbound_answered, inbound_complete, outbound_answered, outbound_complete};
	defaults to (inbound, completed) if absent/unknown — a hangup CDR is terminal."""
	e = (event or "").strip().lower()
	direction = "outbound" if e.startswith("outbound") else "inbound"
	completed = not e.endswith("answered")
	return direction, completed


# ---------------------------------------------------------------------------
# CDR -> CRM Call Log
# ---------------------------------------------------------------------------
def _process(payload: dict, direction: str, completed: bool, account_name=None):
	"""Create or update one CRM Call Log row from an Acefone CDR payload.

	`account_name` is the receiving tenant. The webhook passes it (resolved from the URL
	token); the reconcile pull passes None, so we fall back to matching the CDR's DID to a
	Telephony Account."""
	# Acefone's `call_id` is STABLE across every trigger of one call ("tracks the
	# different triggers for a particular call"); `uuid` VARIES per leg/connection.
	# So key on call_id first for idempotency — a transferred call stays one row.
	call_id = payload.get("call_id") or payload.get("uuid")
	customer_number = (
		payload.get("caller_id_number")
		or payload.get("customer_number")
		or payload.get("customer_number_with_prefix")
		or payload.get("customer_phone")
	)
	call_type = "Incoming" if direction == "inbound" else "Outgoing"
	status = _status_from_cdr(payload, completed)

	# Attribution: prefer the token-identified account (webhook); else match the CDR's DID
	# to a Telephony Account (reconcile pull). Never block logging if it doesn't resolve.
	if account_name is None:
		try:
			account_name = routing.account_for_did(payload.get("did_number"))
		except Exception:
			frappe.log_error(title="Acefone: DID -> account match failed", message=frappe.get_traceback())

	doc = _find_existing(call_id, direction, customer_number, payload)
	if doc:
		_apply(doc, payload, status=status, call_type=call_type, customer_number=customer_number, account_name=account_name)
		if account_name:
			doc.custom_telephony_account = account_name
		doc.save(ignore_permissions=True)
	else:
		doc = frappe.new_doc("CRM Call Log")
		doc.id = call_id
		doc.type = call_type
		doc.telephony_medium = TELEPHONY_MEDIUM
		_apply(doc, payload, status=status, call_type=call_type, customer_number=customer_number, account_name=account_name)
		if account_name:
			doc.custom_telephony_account = account_name
		doc.insert(ignore_permissions=True)

	frappe.db.commit()

	# Post-commit realtime, mirroring WATI's per-row contract: emit only after the row is
	# durably committed (and we only reach here once a row was written), so any live listener
	# reads committed data rather than an in-flight row.
	frappe.publish_realtime("telephony_call", payload)


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


def _apply(doc, payload: dict, status: str, call_type: str, customer_number, account_name=None):
	"""Overlay CDR fields onto a CRM Call Log doc (create or update path)."""
	doc.status = status

	did = payload.get("call_to_number") or payload.get("did_number")
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

	# Agent -> Frappe user. Prefer the operator-configured email/name directly; fall back
	# to mapping the agent number via CRM Telephony Agent.acefone_number.
	agent_user = _resolve_agent_user(payload)
	if agent_user:
		if call_type == "Incoming":
			doc.receiver = agent_user
		else:
			doc.caller = agent_user

	# Resolve + link the lead (set BOTH reference_* and the links child table).
	_link_lead(doc, customer_number, account_name)


def _resolve_agent_user(payload: dict):
	"""Map an Acefone CDR's agent to a Frappe user. Prefer answered_agent_email (a real
	user), then answered_agent_name (matched to a user's full name), then
	answered_agent_number -> CRM Telephony Agent.acefone_number -> user."""
	email = (payload.get("answered_agent_email") or "").strip()
	if email and frappe.db.exists("User", email):
		return email

	name = (payload.get("answered_agent_name") or "").strip()
	if name:
		user = frappe.db.get_value("User", {"full_name": name}, "name")
		if user:
			return user

	return _user_for_agent_number(payload.get("answered_agent_number"))


def _link_lead(doc, customer_number, account_name=None):
	"""Link the call to its lead by strict phone+receiving-account match; unlinked if no hit.
	Sets reference_* (for the Calls-tab feed) AND link_with_reference_doc (crm parity)."""
	if not customer_number:
		return

	lead_name = _resolve_lead_for_call(customer_number, account_name)
	if not lead_name:
		return

	doc.reference_doctype = "CRM Lead"
	doc.reference_docname = lead_name
	doc.link_with_reference_doc("CRM Lead", lead_name)


def _resolve_lead_for_call(customer_number, account_name):
	"""Strict match: a lead whose phone ends in the same last-10 AND whose taxonomy routes to
	the receiving Acefone account. No account, or not exactly one routed lead -> None (leave
	unlinked; never best-guess to first/most-recent)."""
	digits = acefone.normalize_number(customer_number)
	if not digits or not account_name:
		return None
	candidates = frappe.get_all(
		"CRM Lead", filters={"mobile_no": ["like", f"%{digits[-10:]}"]}, pluck="name"
	)
	scoped = routing.leads_for_number_and_account(candidates, account_name) if candidates else []
	return scoped[0] if len(scoped) == 1 else None


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
			"to": ["like", f"%{digits[-10:]}"],
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
		"CRM Telephony Agent", {"acefone_number": ["like", f"%{digits[-10:]}"]}, "user"
	)


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
