"""The Acefone adapter — CDR -> Envelope -> CRM Call Log.

Implements the webhook-spine contract (is_relevant / already_processed / handle /
account_for_payload) plus `normalize`, which is the only Acefone-specific code in the app.
Everything downstream (`telephony.writer`) is provider-blind.

Every mapping below is grounded in a 179-CDR live capture (2026-07-11, 19 DIDs, 76 minutes).
Where a branch is NOT backed by a captured payload it says so — because the first version of
this adapter was written from Acefone's documentation and was wrong in three places, each of
which the live capture disproved:

  * `answered_agent_email` does not exist as an Acefone variable. The email is inside
    `answered_agent`, which is an ARRAY of objects. (8/8 answered calls carried it.)
  * `answered_agent_number` is an EXTENSION ("Extension-0602141810347"), not a phone. The old
    code fed it to a phone matcher, which could never resolve and could false-positive on a
    10-digit suffix collision.
  * `answered_agent_name` is a FIRST name ("Prafull"), so matching `User.full_name` never fires.

Observed vocabularies (nothing outside these appeared in 179 payloads):
  direction    : "inbound" (131) · "Dialer (inbound)" (38)  -- every ANSWERED call was Dialer
  call_status  : "missed" (161) · "answered" (8)            -- lowercase, despite the docs
  hangup_cause : NormalClearing · destination_hangup · destination_not_set
                 disconnected_by_caller · disconnected_by_callee · hangup_as_per_destination
"""
import frappe

from tatva_connect.telephony import envelope as env
from tatva_connect.telephony import routing, writer

PROVIDER = "Acefone"

# Back-compat alias: `CRM Call Log.telephony_medium` stores the provider name, and
# observability/reconcile still speak of it as the "medium".
TELEPHONY_MEDIUM = PROVIDER

# CRM Call Log status vocabulary (frappe/crm):
#   Initiated · Ringing · In Progress · Completed · Failed · Busy · No Answer · Queued · Canceled
_ANSWERED_LIVE = "In Progress"
_ANSWERED_DONE = "Completed"
_MISSED = "No Answer"


# ---------------------------------------------------------------------------
# Spine contract
# ---------------------------------------------------------------------------
def is_relevant(payload, event, account) -> bool:
	"""Cheap front-door pre-filter, inline before enqueue. A CDR we cannot key is not a call
	we can build a row from. The raw payload is already persisted by the time we run, so
	returning False stores it without acting on it."""
	return bool(payload.get("call_id") or payload.get("uuid"))


def already_processed(payload, event, account) -> bool:
	"""True only when a COMPLETED row already exists for this call. An answered-live trigger
	leaves an In Progress row that the later hangup CDR must still update, so we never
	short-circuit those.

	Acefone genuinely re-sends: the 179-CDR capture contained 10 repeats of calls already seen.
	"""
	call_key = payload.get("call_id") or payload.get("uuid")
	if not call_key:
		return False
	return frappe.db.get_value("CRM Call Log", call_key, "status") == _ANSWERED_DONE


def handle(payload, event, account) -> None:
	"""Parse + write. Runs in the spine worker; exceptions propagate to the RQ failed registry
	(the DLQ) and are replayable. No swallow-as-200."""
	process(payload, event=event, account=account)


def account_for_payload(payload, event):
	"""Re-derive the receiving account from a STORED payload, for `spine.replay()` (which has
	no live request token). None if the DID matches no account — attribution then fails closed."""
	try:
		return routing.account_for_did(payload.get("did_number") or payload.get("call_to_number"))
	except Exception:
		frappe.log_error(title="Acefone: DID -> account match failed", message=frappe.get_traceback())
		return None


def process(payload: dict, event=None, account=None):
	"""The one entry point, shared by the webhook and the reconcile pull. Returns the Call Log
	row name, or None when the CDR carries no usable key."""
	if account is None:
		account = account_for_payload(payload, event)
	cdr = normalize(payload, event=event, account=account)
	if cdr is None:
		return None
	return writer.write(cdr)


# ---------------------------------------------------------------------------
# Acefone CDR -> Envelope. The only provider-specific code in the app.
# ---------------------------------------------------------------------------
def normalize(payload: dict, event=None, account=None):
	"""One Acefone CDR -> one Envelope. None when the CDR carries no usable key."""
	# `call_id` is STABLE across every trigger of one call; `uuid` varies per leg. Key on
	# call_id so a transferred call stays one row.
	call_key = payload.get("call_id") or payload.get("uuid")
	if not call_key:
		return None

	direction, channel = _direction_channel(payload, event)
	customer_number, did_number = _numbers(payload, direction)

	return env.build(
		provider=PROVIDER,
		account=account,
		call_key=str(call_key),
		direction=direction,
		channel=channel,
		customer_number=customer_number,
		did_number=did_number,
		status=_status(payload, event),
		connected=str(payload.get("call_connected") or "").strip() == "1",
		# Acefone echoes NOTHING back: `custom_identifier` is not among its webhook variables at
		# all, and `ref_id` — the closest candidate — was empty on all 179 captured CDRs. Read
		# both anyway (an account may be configured differently) and let the writer fall back to
		# number+recency when, as expected, it comes back None.
		correlation_key=(payload.get("custom_identifier") or payload.get("ref_id") or "").strip() or None,
		agent_key=_agent_email(payload),
		agent_name=(payload.get("answered_agent_name") or "").strip() or None,
		started_at=env.parse_timestamp(payload.get("start_stamp")),
		ended_at=env.parse_timestamp(payload.get("end_stamp")),
		# Total call time INCLUDING time in the IVR — not talk time. Acefone exposes no agent
		# talk-time field: `billsec` is empty on every answered call, and on missed calls it is
		# just duration-minus-the-ring-second (the IVR answers, not a human).
		duration_sec=env.to_int(payload.get("duration")),
		recording_url=payload.get("recording_url") or None,
		raw=payload,
	)


def _direction_channel(payload: dict, event):
	"""Direction and channel, from the payload's own `direction` field.

	Acefone qualifies direction with the routing channel — "inbound" vs "Dialer (inbound)" —
	and the two are genuinely different payload shapes, so the channel is worth carrying: it is
	what the capture policy will filter on.

	The URL trigger (`event`) is a CROSS-CHECK only. It used to be the source of truth, which
	meant a webhook registered against the wrong URL silently inverted `from`/`to`.
	"""
	raw = (payload.get("direction") or "").strip().lower()
	if raw:
		direction = "outbound" if "outbound" in raw else "inbound"
		channel = "Dialer" if "dialer" in raw else "IVR"
	else:
		# No direction in the body (an operator omitted it from the dashboard JSON). Fall back
		# to the URL trigger rather than dropping the call, and say so.
		direction = "outbound" if (event or "").startswith("outbound") else "inbound"
		channel = "IVR"
		frappe.logger("telephony").warning(
			f"Acefone CDR without `direction`; inferred {direction!r} from event {event!r}"
		)

	if event and not event.startswith(direction):
		# The webhook is registered against the opposite-direction URL. Not fatal — the payload
		# wins — but the Acefone dashboard is misconfigured and someone should know.
		frappe.logger("telephony").warning(
			f"Acefone direction mismatch: payload says {direction!r}, URL trigger says {event!r}"
		)
	return direction, channel


def _numbers(payload: dict, direction: str):
	"""(customer, DID), each reduced to last-10 digits.

	The two Acefone fields have direction-INDEPENDENT meanings:
	    call_to_number   = the number that was DIALLED
	    caller_id_number = the number shown as the ORIGIN

	Inbound, that makes call_to_number our DID and caller_id_number the customer — confirmed on
	all 179 captured CDRs. Outbound, the platform dials the customer and presents our DID, so
	the two swap roles.

	The outbound branch is UNPROVEN: the capture contains zero outbound CDRs. It is the reading
	that Acefone's own field definitions and LeadSquared's documented Source/Destination
	semantics both imply, but no live payload has exercised it.
	"""
	dialled = payload.get("call_to_number") or payload.get("did_number")
	origin = payload.get("caller_id_number") or payload.get("caller_id")
	spare = payload.get("customer_number_with_prefix") or payload.get("customer_number")

	if direction == "inbound":
		customer, did = (origin or spare), dialled
	else:
		customer, did = (dialled or spare), origin

	return env.phone_digits(customer), env.phone_digits(did)


def _status(payload: dict, event) -> str:
	"""CDR -> CRM Call Log status.

	`call_status` arrives LOWERCASE ("missed"/"answered") despite the docs promising title case,
	so compare case-insensitively rather than relying on that accident.

	No Busy/Canceled branch: `hangup_cause` never carried "busy" or "cancel" in 179 CDRs. The
	old code mapped those from the documentation and they were unreachable.
	"""
	call_status = (payload.get("call_status") or "").strip().lower()
	# We register hangup triggers, so a CDR is terminal unless the URL says it is the
	# answered-but-still-live trigger.
	live = (event or "").endswith("answered")

	if call_status == "answered":
		return _ANSWERED_LIVE if live else _ANSWERED_DONE
	if call_status == "missed":
		return _MISSED

	# Never observed. Surface it rather than silently inventing an outcome.
	frappe.logger("telephony").warning(f"Acefone CDR with unmapped call_status {call_status!r}")
	return _ANSWERED_LIVE if live else "Failed"


def _agent_email(payload: dict):
	"""The agent's email, out of the `answered_agent` ARRAY. The only identifier that resolves.

	Shape (8/8 answered CDRs):
	    [{"id": "...", "name": "Prafull", "number": "Extension-06021...",
	      "email": "prafull.chobe@...", "is_transferred_agent": "No"}]

	Take the LAST entry: on a transfer the array carries every agent that touched the call, and
	the final one is who actually handled it.
	"""
	agents = payload.get("answered_agent")
	if isinstance(agents, str):
		# A form-urlencoded body delivers the array as a JSON string.
		agents = frappe.parse_json(agents) if agents.strip().startswith("[") else None
	if isinstance(agents, dict):
		agents = [agents]
	if isinstance(agents, list):
		for entry in reversed(agents):
			if isinstance(entry, dict) and (entry.get("email") or "").strip():
				return entry["email"].strip().lower()
	return None
