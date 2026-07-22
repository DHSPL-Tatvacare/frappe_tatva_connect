"""The Acefone adapter — CDR -> Envelope -> CRM Call Log.

Implements the webhook-spine contract plus `normalize`, which is the only Acefone-specific code in
the app. Everything downstream is provider-blind.

Every mapping is grounded in a 363-CDR live capture across both directions. Where a branch is not
backed by a captured payload it says so, because the first version of this adapter was written from
Acefone's documentation and the capture disproved three of its assumptions:

  * `answered_agent_email` is not an Acefone variable. The email sits inside `answered_agent`, an
    array of objects, and was present on every answered call.
  * `answered_agent_number` is an extension ("Extension-0602141810347"), not a phone. The old code
    fed it to a phone matcher, which could never resolve and could collide on a 10-digit suffix.
  * `hangup_cause` never carries "busy" or "cancel", so both status branches were unreachable.

Observed vocabularies, and nothing outside them:
  direction    : inbound · Dialer (inbound) · Dialer (outbound)   -- only Dialer calls are answered
  call_status  : missed · answered                                -- lowercase, despite the docs
  hangup_cause : NormalClearing · destination_hangup · destination_not_set
                 disconnected_by_caller · disconnected_by_callee · hangup_as_per_destination
"""
import frappe
from frappe import parse_json

from tatva_connect import phone
from tatva_connect.telephony import envelope as env
from tatva_connect.telephony import resolve, writer

PROVIDER = "Acefone"

# `CRM Call Log.telephony_medium` stores the provider name; observability and reconcile call it the
# medium. Aliased rather than duplicated.
TELEPHONY_MEDIUM = PROVIDER

_ANSWERED_LIVE = "In Progress"
_ANSWERED_DONE = "Completed"
_MISSED = "No Answer"


def screen(payload, event, account):
	"""(wanted, reason). One question, asked once, so the payload is parsed once.

	The reason is what makes a declined row actionable: an operator reads it, maps the DID it names,
	replays the row, and the call lands. A no is never a loss — the spine persists the payload either
	way.
	"""
	cdr = normalize(payload, event=event, account=account)
	if cdr is None:
		return False, "no call_id or uuid to key the call on"
	if resolve.is_ours(cdr):
		return True, None

	if not resolve.grain_for(cdr):
		return False, f"DID {cdr['did_number'] or '(none)'} is not mapped to a grain"
	if not resolve.should_capture(cdr):
		return False, f"no capture rule captures {cdr['direction']} {cdr['channel']} calls"
	return False, f"DID {cdr['did_number']} belongs to a different telephony account"


def already_processed(payload, event, account) -> bool:
	"""True only when a completed row already exists for this call.

	An answered-live trigger leaves an In Progress row that the later hangup CDR must still update,
	so those are never short-circuited.
	"""
	call_key = payload.get("call_id") or payload.get("uuid")
	row = writer.row_for_key(call_key)
	if not row:
		return False
	return frappe.db.get_value("CRM Call Log", row, "status") == _ANSWERED_DONE


def handle(payload, event, account) -> None:
	"""Parse and write. Runs in the spine worker; a failure reaches the DLQ and stays replayable."""
	process(payload, event=event, account=account)


def account_for_payload(payload, event):
	"""Re-derive the receiving account from a stored payload, for replay and reconcile, which carry no
	token.

	Resolved off the DID map, the same table the grain comes from, so a replayed call is attributed to
	exactly the account the live delivery was. Reading it from anywhere else is how the two paths drift
	apart and start writing calls under different accounts.
	"""
	try:
		return resolve.account_for_did(payload.get("did_number") or payload.get("call_to_number"))
	except Exception:
		frappe.log_error(title="Acefone: DID -> account match failed", message=frappe.get_traceback())
		return None


def process(payload: dict, event=None, account=None):
	"""Write the call. Returns the Call Log row name, or None when the call is not ours.

	Shared by the webhook worker, the reconcile pull and replay. The gates are re-asked here rather
	than trusted from the front door: replay and reconcile call this directly, with no front door
	ahead of them, so a gate living only at the front door could be walked straight past.
	"""
	if account is None:
		account = account_for_payload(payload, event)
	wanted, _reason = screen(payload, event, account)
	if not wanted:
		return None
	return writer.write(normalize(payload, event=event, account=account))


def normalize(payload: dict, event=None, account=None):
	"""One Acefone CDR -> one Envelope. None when the CDR carries no usable key."""
	# `call_id` is stable across every trigger of one call; `uuid` varies per leg. Keying on call_id
	# keeps a transferred call on one row.
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
		# Read from both candidates, though neither ever returns: `custom_identifier` is not an
		# Acefone webhook variable and `ref_id` was empty on all 363 captured CDRs.
		correlation_key=(payload.get("custom_identifier") or payload.get("ref_id") or "").strip() or None,
		agent_key=_agent_email(payload),
		agent_name=(payload.get("answered_agent_name") or "").strip() or None,
		started_at=env.parse_timestamp(payload.get("start_stamp")),
		ended_at=env.parse_timestamp(payload.get("end_stamp")),
		# Total call time including the IVR, not talk time. Acefone exposes no agent talk-time field:
		# `billsec` is empty on every answered call.
		duration_sec=env.to_int(payload.get("duration")),
		recording_url=payload.get("recording_url") or None,
		raw=payload,
	)


def _direction_channel(payload: dict, event):
	"""Direction and channel, read from the payload's own `direction` field.

	Acefone qualifies direction with the routing channel — "inbound" against "Dialer (inbound)" — and
	the two are genuinely different payload shapes, so the channel is carried: it is what the capture
	policy filters on. The URL trigger is a cross-check only.
	"""
	raw = (payload.get("direction") or "").strip().casefold()
	if raw:
		direction = "outbound" if "outbound" in raw else "inbound"
		channel = "Dialer" if "dialer" in raw else "IVR"
	else:
		# Absent from the body, so the URL trigger is used rather than dropping the call.
		direction = "outbound" if (event or "").startswith("outbound") else "inbound"
		channel = "IVR"
		frappe.logger("telephony").warning(
			f"Acefone CDR without `direction`; inferred {direction!r} from event {event!r}"
		)

	if event and not event.startswith(direction):
		# The webhook is registered against the opposite-direction URL. The payload wins, but the
		# provider dashboard is misconfigured and someone should know.
		frappe.logger("telephony").warning(
			f"Acefone direction mismatch: payload says {direction!r}, URL trigger says {event!r}"
		)
	return direction, channel


def _numbers(payload: dict, direction: str):
	"""(customer, DID), each reduced to last-10 digits.

	The two Acefone fields have direction-independent meanings: `call_to_number` is the number that
	was dialled, `caller_id_number` the number shown as the origin. Inbound, that makes call_to_number
	the DID and caller_id_number the customer. Outbound the platform dials the customer and presents
	the DID, so the two swap roles. Both readings are confirmed against live CDRs.
	"""
	dialled = payload.get("call_to_number") or payload.get("did_number")
	origin = payload.get("caller_id_number") or payload.get("caller_id")
	spare = payload.get("customer_number_with_prefix") or payload.get("customer_number")

	if direction == "inbound":
		customer, did = (origin or spare), dialled
	else:
		customer, did = (dialled or spare), origin

	return phone.match_digits(customer, last=10), phone.match_digits(did, last=10)


def _status(payload: dict, event) -> str:
	"""CDR -> CRM Call Log status.

	`call_status` arrives lowercase despite the docs promising title case, so it is folded rather than
	compared as sent. There is no Busy or Canceled branch: `hangup_cause` never carried either word in
	363 CDRs, and the branches mapped from the documentation were unreachable.
	"""
	call_status = (payload.get("call_status") or "").strip().casefold()
	# Only hangup triggers are registered, so a CDR is terminal unless the URL names the
	# answered-but-still-live trigger.
	live = (event or "").endswith("answered")

	if call_status == "answered":
		return _ANSWERED_LIVE if live else _ANSWERED_DONE
	if call_status == "missed":
		return _MISSED

	frappe.logger("telephony").warning(f"Acefone CDR with unmapped call_status {call_status!r}")
	return _ANSWERED_LIVE if live else "Failed"


def _agent_email(payload: dict):
	"""The agent's email, taken from the `answered_agent` array. The only identifier that resolves.

	The last entry is used: on a transfer the array carries every agent that touched the call, and the
	final one handled it.
	"""
	agents = payload.get("answered_agent")
	if isinstance(agents, str):
		# A form-urlencoded body delivers the array as a JSON string.
		agents = parse_json(agents) if agents.strip().startswith("[") else None
	if isinstance(agents, dict):
		agents = [agents]
	if not isinstance(agents, list):
		return None
	for entry in reversed(agents):
		if isinstance(entry, dict) and (entry.get("email") or "").strip():
			return entry["email"].strip().casefold()
	return None
