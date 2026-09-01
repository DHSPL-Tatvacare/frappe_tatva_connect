"""The Acefone adapter — CDR -> Envelope -> CRM Call Log.

Implements the webhook-spine contract plus `normalize`, which is the only Acefone-specific code in
the app. Everything downstream is provider-blind.

Every mapping is grounded in a 363-CDR live capture across both directions. Where a branch is not
backed by a captured payload it says so, because the first version of this adapter was written from
Acefone's documentation and the capture disproved three of its assumptions:

  * `answered_agent_email` is not an Acefone variable. Where an email exists at all it sits inside
    `answered_agent`; on the 2026 tenant that object carries only a seat, so the seat identifies the rep.
  * `answered_agent_number` is an extension ("Extension-0602141810347"), not a phone. The old code
    fed it to a phone matcher, which could never resolve and could collide on a 10-digit suffix.
  * `hangup_cause` never carries "busy" or "cancel", so both status branches were unreachable.

Observed vocabularies, and nothing outside them:
  direction    : see `_DIRECTIONS` -- an unlisted word defers to the URL trigger, never to a guess
  call_status  : missed · answered                                -- lowercase, despite the docs
  hangup_cause : NormalClearing · destination_hangup · destination_not_set
                 disconnected_by_caller · disconnected_by_callee · hangup_as_per_destination
"""
import frappe
from frappe import parse_json

from tatva_connect import phone
from tatva_connect.channels import contract
from tatva_connect.telephony import envelope as env
from tatva_connect.telephony import resolve, writer

PROVIDER = "Acefone"
ACCOUNT_DT = "CRM Telephony Account"

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
	# Read ONCE and passed on: `_status` warns about an unmapped status, and asking twice warns twice.
	status = _status(payload, event)

	return env.build(
		provider=PROVIDER,
		account=account,
		call_key=str(call_key),
		direction=direction,
		channel=channel,
		customer_number=customer_number,
		did_number=did_number,
		status=status,
		connected=str(payload.get("call_connected") or "").strip() == "1",
		recording_ref=recording_ref(payload, status, account),
		correlation_keys=_correlation_keys(payload),
		agent_key=_agent_email(payload),
		agent_extension=_agent_extension(payload),
		agent_name=(payload.get("answered_agent_name") or "").strip() or None,
		started_at=env.parse_timestamp(payload.get("start_stamp")),
		ended_at=env.parse_timestamp(payload.get("end_stamp")),
		# Total call time including the IVR, not talk time. Acefone exposes no agent talk-time field:
		# `billsec` is empty on every answered call.
		duration_sec=env.to_int(payload.get("duration")),
		raw=payload,
	)


def recording_ref(payload: dict, status: str, account=None):
	"""WHERE THIS CALL'S AUDIO IS — the `recording` vocabulary's one function, and the whole of it.

	Three answers and no fourth, exactly as `channels.contract.RecordingRef` defines them. Grounded in the
	363-CDR capture rather than the documentation: `recording_url` is ON the hangup CDR — all 8 answered
	calls carried one, and so did 130 of the 171 missed ones, because Acefone records the IVR greeting a
	caller hears before nobody picks up. A missed call without one produced no audio and never will.

	A still-live call is the only "not ready yet": the recording is published as part of the hangup the
	terminal CDR reports, so a row parked here is answered by that CDR, not by us polling for it.

	Nothing here fetches, owns, names, retries or serves anything — `storage.call_media` does all of it for
	every producer, and learns this provider's name only as the string riding in on the ref.
	"""
	url = (payload.get("recording_url") or "").strip()
	if url:
		return ref_for_url(url, account)
	if status == _ANSWERED_LIVE:
		return contract.RecordingRef(pending=True)
	return contract.RecordingRef()


def ref_for_url(url: str, account=None):
	"""A ref for a recording URL — from a live CDR, or from a row that already carries one.

	The ONE place an Acefone ref is built, so the backfill of already-logged calls cannot describe a
	recording differently from the way ingestion does.

	The credential and the host allowlist are the SAME two the play-time proxy has always applied to this
	URL, read off the same account row and carried on the ref rather than re-implemented at a second fetch
	site. Both are DERIVED per fetch: an operator who narrows the allowlist narrows it for the next attempt
	too, which a copy stored on a row could never do.
	"""
	acct = _account_doc(account)
	token = acct.get_password("api_token", raise_exception=False) if acct else None
	return contract.RecordingRef(
		url=url,
		provider=PROVIDER,
		# `transfer.fetch_capped` drops this the moment a redirect changes host, so the credential never
		# reaches whoever the provider's storage hop names.
		headers={"Authorization": f"Bearer {token}"} if token else None,
		allowed_hosts=[row.host for row in (acct.get("recording_host_allowlist") or [])] if acct else None,
	)


def _account_doc(account):
	"""The receiving account, or None. A NAME comes in — the spine and the DID map both carry names."""
	if not account or not frappe.db.exists(ACCOUNT_DT, account):
		return None
	return frappe.get_cached_doc(ACCOUNT_DT, account)


# Acefone's own words for direction, each mapped to (direction, channel). `clicktocall` is undocumented and was found on a live outbound CDR: agent-dialled, so Dialer.
_DIRECTIONS = {
	"inbound": ("inbound", "IVR"),
	"dialer (inbound)": ("inbound", "Dialer"),
	"outbound": ("outbound", "IVR"),
	"dialer (outbound)": ("outbound", "Dialer"),
	"clicktocall": ("outbound", "Dialer"),
}


def _direction_channel(payload: dict, event):
	"""Direction and channel, read from the payload's own `direction` field.

	Acefone qualifies direction with the routing channel — "inbound" against "Dialer (inbound)" — and
	the two are genuinely different payload shapes, so the channel is carried: it is what the capture
	policy filters on.

	A word outside the table falls back to the URL trigger, which names the direction outright. The
	old code assumed anything without "outbound" in it was inbound, so the undocumented `clicktocall`
	read as inbound, `_numbers` swapped customer and DID, and every click-to-call was declined as an
	unmapped DID. An unknown word must never silently pick a direction.
	"""
	raw = (payload.get("direction") or "").strip().casefold()
	known = _DIRECTIONS.get(raw)
	if known:
		direction, channel = known
	else:
		direction = "outbound" if (event or "").startswith("outbound") else "inbound"
		channel = "Dialer" if "dialer" in raw else "IVR"
		frappe.logger("telephony").warning(
			f"Acefone CDR direction {raw!r} is not a known word; inferred {direction!r} from event {event!r}"
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


def _correlation_keys(payload: dict) -> tuple:
	"""Every id that could name the row the bridge minted, best first, de-duplicated.

	Both return on a call WE placed: `custom_identifier` is the one we sent, `ref_id` is Acefone's own
	and comes back from the click-to-call POST as well as on the CDR. Neither appears on a call placed
	from Acefone's softphone, which is why the writer still keeps a window fallback behind this.
	"""
	seen = (payload.get("custom_identifier"), payload.get("ref_id"))
	return tuple(dict.fromkeys(k.strip() for k in seen if isinstance(k, str) and k.strip()))


def _answered_agents(payload: dict) -> list:
	"""The `answered_agent` entries as a list, whatever shape the tenant sent.

	One parser, because the object carries BOTH identifiers and each was normalising it separately.
	A form-urlencoded body delivers it as a JSON string; a single agent arrives as a bare object.
	"""
	agents = payload.get("answered_agent")
	if isinstance(agents, str):
		agents = parse_json(agents) if agents.strip().startswith(("[", "{")) else None
	if isinstance(agents, dict):
		agents = [agents]
	return agents if isinstance(agents, list) else []


def _last_agent_value(payload: dict, key: str):
	"""The last agent's `key`, or None. Last because on a transfer it is the agent who handled the call."""
	for entry in reversed(_answered_agents(payload)):
		if isinstance(entry, dict) and (entry.get(key) or "").strip():
			return entry[key].strip()
	return None


def _agent_extension(payload: dict):
	"""The agent's Acefone seat ("0602417430016"), or None. What identifies them when no email is sent.

	Taken WHOLE and never digit-matched: a seat is not a phone number, and suffix-matching one against
	a phone column is the collision this adapter was already burned by. It is the same value the
	operator already stores on `CRM Telephony Agent` to place that rep's calls, so nothing new is configured.
	"""
	seat = (payload.get("answered_agent_number") or "").strip() or _last_agent_value(payload, "number")
	if not seat:
		return None
	# The webhook writes it bare; a CDR row writes the same value as "Extension-0602417430016".
	return seat.rsplit("-", 1)[-1].strip() or None


def _agent_email(payload: dict):
	"""The agent's email, or None. The identifier that resolves directly to a CRM user where a tenant sends one."""
	email = _last_agent_value(payload, "email")
	return email.casefold() if email else None
