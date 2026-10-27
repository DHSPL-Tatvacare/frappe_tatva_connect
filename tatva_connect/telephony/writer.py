"""Envelope -> CRM Call Log. The one brain, shared by every provider.

The only module that writes a Call Log from a CDR, and the only one that branches on direction:

    inbound   type=Incoming  from=customer  to=DID       agent -> receiver
    outbound  type=Outgoing  from=DID       to=customer  agent -> caller

frappe/crm's `parse_call_log` already reads `receiver` for an Incoming call and `caller` for an
Outgoing one, and CallArea.vue picks its icon and verb off `type`. A correct envelope is therefore
the whole job — the lead's Calls tab needs no UI work.
"""
import frappe
from frappe.utils import add_to_date, now_datetime

from tatva_connect.storage import call_media
from tatva_connect.telephony import resolve

CALL_LOG = "CRM Call Log"

# The provider's call id, held in its own column and never read from the row's `name`. crm names a Call
# Log `field:id`, but the naming series is the app's to change; every telephony path dedupes on this
# field so that a change to naming cannot break call ingestion.
CALL_KEY_FIELD = "custom_provider_call_id"

# How recent a still-Initiated outbound row may be to count as the same call when the provider
# echoed no correlation id back.
OUTBOUND_MATCH_WINDOW_MIN = 5

# A row still in this status was written by a live answered trigger, before the call ended.
LIVE_STATUS = "In Progress"

# Envelope key -> column for what a CDR may or may not carry: set when sent, never blanked by an event that did not send it.
_FACTS = {
	"end_reason": "custom_end_reason",
	"end_code": "custom_end_code",
	"call_type": "custom_call_type",
	"campaign": "custom_campaign",
	"hangup_reason": "custom_hangup_reason",
	"customer_ring_sec": "custom_customer_ring_sec",
	"customer_dialled": "custom_customer_dialled",
}

# The agent's saved outcome; a hangup may carry it, and a later disposition event fills it on its own.
DISPOSITION = {
	"disposition": "custom_disposition",
	"disposition_code": "custom_disposition_code",
	"sub_disposition": "custom_sub_disposition",
	"disposition_notes": "custom_disposition_notes",
}


def row_for_key(call_key):
	"""The Call Log row carrying this provider call id, or None.

	The one way a call is found by its key. A row logged by hand carries no provider key, so it can
	never be selected here.
	"""
	if not call_key:
		return None
	return frappe.db.get_value(CALL_LOG, {CALL_KEY_FIELD: call_key}, "name")


def write(cdr) -> str:
	"""Create or update the Call Log row for one envelope. Returns the row name.

	Idempotent: a provider re-sends the same call (10 repeats in a 179-CDR capture), and an
	answered-live trigger is later superseded by its hangup CDR. Both land on the same row.
	"""
	existing = _find_row(cdr)
	doc = frappe.get_doc(CALL_LOG, existing) if existing else frappe.new_doc(CALL_LOG)

	if not existing:
		# crm's autoname is `field:id`, so a new row still needs one, and crm's own Twilio and Exotel
		# handlers set it the same way. Nothing in this app reads it back.
		doc.id = cdr["call_key"]
		doc.telephony_medium = cdr["provider"]

	# Written on update as well: a row matched by correlation carries the placeholder the bridge minted,
	# which the real provider id supersedes once the CDR arrives with one.
	setattr(doc, CALL_KEY_FIELD, cdr["call_key"])

	# Established once. A provider never reverses direction mid-call, and letting it flip would
	# silently swap `from`/`to` on a row that already exists.
	doc.type = "Incoming" if cdr["direction"] == "inbound" else "Outgoing"
	_apply(doc, cdr)

	if existing:
		doc.save(ignore_permissions=True)  # authz-ok: tier-b — webhook: token-authenticated + strict phone+grain attribution
	else:
		doc.flags.tatva_live_call = not frappe.flags.get("tatva_bulk_history")  # a call arriving now, not one a reconcile files from the past
		doc.insert(ignore_permissions=True)  # authz-ok: tier-b — webhook: token-authenticated + strict phone+grain attribution

	frappe.db.commit()
	adopt_recording(doc.name, cdr.get("recording_ref"))
	_publish(doc)
	return doc.name


def adopt_recording(name, ref) -> None:
	"""Hand the adapter's answer to the media layer, and point the row at OUR copy of the audio.

	THE ROW NEVER HOLDS A PROVIDER'S URL AGAIN. A provider ages its recordings out and the link dies with
	them, which is how a lead's Calls tab ends up with a play button that fetches a 404 from somebody
	else's host. `store_recording` fetches the bytes once, owns them to this call, and Azure offload
	follows from the File row like every other file in the app — none of which is re-implemented here.

	Provider-blind, like the rest of this module: the ref is DATA the adapter built, so adding a telephony
	provider still adds an adapter and nothing else. Called on the update path too, because a first CDR can
	arrive before the recording is published and the hangup CDR is what carries it.

	AFTER the call row is committed, and committed itself: the fetch reaches a provider over the network,
	and a worker killed mid-fetch must still leave the call logged. A failure is not raised — it lands on
	the media row with its reason and its next attempt, which is what the retry sweep reads.
	"""
	if ref is None:
		return  # a row this app did not ingest from a CDR — the media layer does not invent state for it
	ours = call_media.store_recording(name, ref)
	if ours:
		frappe.db.set_value(CALL_LOG, name, "recording_url", ours, update_modified=False)
	# Committed whatever the answer was: Awaiting-with-a-next-attempt is the state the sweep retries from.
	frappe.db.commit()


def write_disposition(call_key, disposition) -> str | None:
	"""Fill a logged call's disposition. Returns the row name, or None when no call carries this key.

	One UPDATE of the disposition columns alone, never a load-and-save, so it cannot overwrite what the hangup
	wrote and has no `modified` to race. Never creates a row: a disposition is about a call, not a call.
	"""
	name = row_for_key(call_key)
	if not name:
		return None
	values = {column: disposition[key] for key, column in DISPOSITION.items() if _sent(disposition.get(key))}
	if values:
		frappe.db.set_value(CALL_LOG, name, values, update_modified=False)
	return name


def _sent(value) -> bool:
	"""Whether an event carried this fact; 0 seconds is a value, a blank is not."""
	return value not in (None, "")


def _publish(doc) -> None:
	"""Post-commit nudge to the record's DOC room; no room means `"all"`, which every System User joins unguarded, and that shipped the raw CDR to every browser."""
	if frappe.flags.get("tatva_bulk_history"):
		return  # a reconcile in progress tells the lead ONCE when it ends (reconcile.reconcile_lead), never per row
	# The rep who placed an outbound call hears how it ended, in their own room only.
	if doc.type == "Outgoing" and doc.caller:
		frappe.publish_realtime(
			"telephony_call_status",
			{"call_log": doc.name, "status": doc.status, "duration": doc.duration},
			user=doc.caller,
		)
	# The row's OWN reference, never a hardcoded doctype — an outbound row can hang off a CRM Deal.
	# `reference_doctype` defaults to "CRM Lead", so the docname is what says it resolved.
	if not doc.get("reference_docname"):
		return
	frappe.publish_realtime(
		"telephony_call",
		{"reference_doctype": doc.reference_doctype, "reference_name": doc.reference_docname},
		doctype=doc.reference_doctype,
		docname=doc.reference_docname,
	)


def republish(reference_doctype, reference_name) -> None:
	"""The ONE signal a finished bulk walk sends, after its commit — the per-row nudge `_publish` held back."""
	frappe.publish_realtime(
		"telephony_call",
		{"reference_doctype": reference_doctype, "reference_name": reference_name},
		doctype=reference_doctype,
		docname=reference_name,
	)


def _apply(doc, cdr) -> None:
	"""Overlay an envelope onto a doc. The same code on the create and the update path."""
	customer = cdr["customer_number"]
	did = cdr["did_number"]
	inbound = cdr["direction"] == "inbound"
	was_live = doc.get("status") == LIVE_STATUS

	doc.status = cdr["status"]
	setattr(doc, "from", customer if inbound else did)
	doc.to = did if inbound else customer
	doc.medium = did or doc.medium

	# The number's own account, not the sibling whose token delivered it: the one its rep is credited from and its recording is read through.
	grain = resolve.grain_for(cdr)
	doc.custom_telephony_account = grain["telephony_account"]
	if cdr.get("duration_sec"):
		doc.duration = cdr["duration_sec"]
	for key, column in _FACTS.items():
		if _sent(cdr.get(key)):
			doc.set(column, cdr[key])
	for key, column in DISPOSITION.items():
		if _sent(cdr["disposition"].get(key)):
			doc.set(column, cdr["disposition"][key])
	if cdr.get("started_at"):
		doc.start_time = cdr["started_at"]
	if cdr.get("ended_at"):
		doc.end_time = cdr["ended_at"]

	# `reference_*` only (crm's `get_linked_calls` also joins Dynamic Links, so linking both shows the call twice); linked once, never moved.
	lead = None if doc.reference_docname else resolve.lead_for(cdr, grain)
	if lead:
		doc.reference_doctype = "CRM Lead"
		doc.reference_docname = lead

	# Credited once: a refresh, replay or re-sent CDR never moves a call to another rep.
	rep = "receiver" if inbound else "caller"
	if not doc.get(rep):
		doc.set(rep, _credited(doc, cdr, grain))
	elif inbound and was_live:
		# A live pickup credits the first answer; a later event names who handled it after a transfer, as the hangup always did.
		doc.set(rep, resolve.user_for(cdr, grain) or doc.get(rep))


def _credited(doc, cdr, grain):
	"""The rep a call is credited to: whoever handled it, unless the operator keeps an outgoing lead call for the lead's own reps."""
	user = resolve.user_for(cdr, grain)
	if cdr["direction"] != "outbound" or doc.get("reference_doctype") != "CRM Lead":
		return user
	return user if resolve.may_credit(user, doc.get("reference_docname")) else None


def _find_row(cdr):
	"""The existing row a CDR updates, or None for a fresh one.

	Matched on the call key, then on any correlation id the provider echoed back, then — outbound only
	— on the most recent still-Initiated row placed to this number inside the match window.

	ALL the echoed ids are tried, not the first. Acefone returns its own `ref_id` from the click-to-call
	POST and repeats both that and our `custom_identifier` on the CDR; the bridge stamps whichever the
	POST answered onto the row. Trying one id matched the row only when that guess agreed with what the
	bridge stamped, and otherwise fell through to the window — which expires, and then writes a second
	row and orphans the Initiated one.

	Both key lookups read the provider-call-id column, never the row's `name`.
	"""
	existing = row_for_key(cdr["call_key"])
	if existing:
		return existing

	if cdr["direction"] != "outbound":
		return None

	for key in cdr.get("correlation_keys") or ():
		correlated = row_for_key(key)
		if correlated:
			return correlated

	phone = cdr.get("customer_number")
	if not phone:
		return None
	cutoff = add_to_date(now_datetime(), minutes=-OUTBOUND_MATCH_WINDOW_MIN)
	rows = frappe.get_all(
		CALL_LOG,
		filters={
			"telephony_medium": cdr["provider"],
			"type": "Outgoing",
			"status": "Initiated",
			"to": ["like", f"%{phone}"],
			"creation": [">=", cutoff],
		},
		order_by="creation desc",
		limit=1,
		pluck="name",
	)
	return rows[0] if rows else None
