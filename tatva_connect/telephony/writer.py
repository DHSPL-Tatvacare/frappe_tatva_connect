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

from tatva_connect.telephony import resolve

CALL_LOG = "CRM Call Log"

# The provider's call id, held in its own column and never read from the row's `name`. crm names a Call
# Log `field:id`, but the naming series is the app's to change; every telephony path dedupes on this
# field so that a change to naming cannot break call ingestion.
CALL_KEY_FIELD = "custom_provider_call_id"

# How recent a still-Initiated outbound row may be to count as the same call when the provider
# echoed no correlation id back.
OUTBOUND_MATCH_WINDOW_MIN = 5


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
		doc.insert(ignore_permissions=True)  # authz-ok: tier-b — webhook: token-authenticated + strict phone+grain attribution

	frappe.db.commit()
	_publish(doc)
	return doc.name


def _publish(doc) -> None:
	"""Post-commit nudge to the record's DOC room; no room means `"all"`, which every System User joins unguarded, and that shipped the raw CDR to every browser."""
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


def _apply(doc, cdr) -> None:
	"""Overlay an envelope onto a doc. The same code on the create and the update path."""
	customer = cdr["customer_number"]
	did = cdr["did_number"]
	inbound = cdr["direction"] == "inbound"

	doc.status = cdr["status"]
	setattr(doc, "from", customer if inbound else did)
	doc.to = did if inbound else customer
	doc.medium = did or doc.medium

	if cdr.get("account"):
		doc.custom_telephony_account = cdr["account"]
	if cdr.get("duration_sec"):
		doc.duration = cdr["duration_sec"]
	if cdr.get("recording_url"):
		doc.recording_url = cdr["recording_url"]
	if cdr.get("started_at"):
		doc.start_time = cdr["started_at"]
	if cdr.get("ended_at"):
		doc.end_time = cdr["ended_at"]

	# The DID's grain drives both remaining questions: which lead, and whether the rep who answered
	# actually works this grain.
	grain = resolve.grain_for(cdr)

	# Assigned only when someone resolves. A later CDR for the same call must not blank out a rep an
	# earlier one established.
	user = resolve.user_for(cdr, grain)
	if user:
		if inbound:
			doc.receiver = user
		else:
			doc.caller = user

	# `reference_*` only. `crm.api.activities.get_linked_calls` unions a `reference_docname` query
	# with a `Dynamic Link` join, so also calling `link_with_reference_doc()` returned the same call
	# twice and rendered it twice in the lead's Calls tab.
	lead = resolve.lead_for(cdr, grain)
	if lead:
		doc.reference_doctype = "CRM Lead"
		doc.reference_docname = lead


def _find_row(cdr):
	"""The existing row a CDR updates, or None for a fresh one.

	Matched on the call key, then on a correlation id the provider echoed back, then — outbound only
	— on the most recent still-Initiated row placed to this number inside the match window. The last
	fallback exists because Acefone echoes nothing: the `custom_identifier` sent on a click-to-call
	never returns. Without it an outbound CDR would create a second row and orphan the Initiated one.

	Both key lookups read the provider-call-id column, never the row's `name`.
	"""
	existing = row_for_key(cdr["call_key"])
	if existing:
		return existing

	if cdr["direction"] != "outbound":
		return None

	correlated = row_for_key(cdr.get("correlation_key"))
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
