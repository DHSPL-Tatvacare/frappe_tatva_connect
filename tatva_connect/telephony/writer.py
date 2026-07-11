"""Envelope -> CRM Call Log. The one brain, shared by every provider.

This is the only module that writes a Call Log from a CDR. It knows nothing about Acefone,
Ozonetel, or any provider's field name — it reads the envelope and nothing else.

Direction is the ONLY thing it branches on, and it branches once:

    inbound   type=Incoming  from=customer  to=DID       agent -> receiver
    outbound  type=Outgoing  from=DID       to=customer  agent -> caller

frappe/crm's `parse_call_log` already reads `receiver` for an Incoming call and `caller` for
an Outgoing one, and CallArea.vue already picks the icon and the verb off `type`. So getting
the envelope right is the whole job: the Lead's Calls tab needs no UI work.
"""
import frappe

from tatva_connect.telephony import envelope as env
from tatva_connect.telephony import routing

CALL_LOG = "CRM Call Log"

# How recent a still-Initiated outbound row may be to count as the same call when the provider
# echoed no correlation id back.
OUTBOUND_MATCH_WINDOW_MIN = 5


def write(cdr) -> str:
	"""Create or update the Call Log row for one envelope. Returns the row name.

	Idempotent on `call_key`: the provider re-sends the same call (10 repeats in a 179-CDR
	capture), and an answered-live trigger is later superseded by the hangup CDR. Both land on
	the same row.
	"""
	existing = _find_row(cdr)
	doc = frappe.get_doc(CALL_LOG, existing) if existing else frappe.new_doc(CALL_LOG)

	if not existing:
		doc.id = cdr["call_key"]
		doc.telephony_medium = cdr["provider"]

	# Direction can only be established once — a provider never reverses it mid-call, and letting
	# it flip would silently swap `from`/`to` on an existing row.
	doc.type = "Incoming" if cdr["direction"] == "inbound" else "Outgoing"

	_apply(doc, cdr)

	if existing:
		doc.save(ignore_permissions=True)  # authz-ok: tier-b — webhook: token-authenticated + strict phone+grain attribution
	else:
		doc.insert(ignore_permissions=True)  # authz-ok: tier-b — webhook: token-authenticated + strict phone+grain attribution

	frappe.db.commit()
	# Post-commit, so a live listener reads a durable row rather than an in-flight one.
	frappe.publish_realtime("telephony_call", cdr.get("raw") or {})
	return doc.name


def _find_row(cdr):
	"""The existing Call Log row this CDR updates, or None for a fresh one.

	1. Same `call_key`  — the normal path, and the only one inbound ever needs.
	2. `correlation_key` — the row WE pre-created when placing an outbound call, if the
	   provider echoed our id back.
	3. Outbound only: the most recent still-Initiated row we placed to this number inside the
	   match window. The fallback for a provider that echoes nothing — which is Acefone: the
	   `custom_identifier` we send never comes back (empty on all 179 captured CDRs).

	Without 2 and 3, an outbound CDR would create a SECOND row and orphan the Initiated one
	`bridge.make_a_call` wrote.
	"""
	if frappe.db.exists(CALL_LOG, cdr["call_key"]):
		return cdr["call_key"]

	if cdr["direction"] != "outbound":
		return None

	ident = cdr.get("correlation_key")
	if ident and frappe.db.exists(CALL_LOG, ident):
		return ident

	phone = cdr.get("customer_number")
	if not phone:
		return None
	cutoff = frappe.utils.add_to_date(frappe.utils.now_datetime(), minutes=-OUTBOUND_MATCH_WINDOW_MIN)
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


def _apply(doc, cdr) -> None:
	"""Overlay the envelope onto the doc. Same code on the create and the update path."""
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

	# Only assign when we actually resolved someone: a later CDR for the same call must never
	# blank out an agent an earlier one established.
	user = resolve_agent(cdr)
	if user:
		if inbound:
			doc.receiver = user
		else:
			doc.caller = user

	link_lead(doc, cdr)


def resolve_agent(cdr):
	"""The envelope's agent key -> a Frappe user, or None.

	The key is an email — that is what BOTH providers give us (Acefone's `answered_agent[].email`,
	Ozonetel's agent identifier in its User-Agent mapping). Provider-local identifiers like
	Acefone's `Extension-0602141810347` are deliberately NOT matched here: they are not phone
	numbers, and the old code's attempt to treat them as such could never resolve.

	Unresolved is not an error. The call is still logged, just unattributed — only an unmapped
	DID drops a call, never an unmapped agent.
	"""
	email = (cdr.get("agent_key") or "").strip().lower()
	if not email:
		return None
	return frappe.db.get_value("User", {"name": email, "enabled": 1}, "name")


def link_lead(doc, cdr) -> None:
	"""Attach the call to exactly one lead, or leave it unlinked.

	The rule, and it is the only one: `(DID -> grain) + customer phone` must resolve to a
	SINGLE lead. Two facts that are each ambiguous alone intersect at one lead — a person with
	four leads across four programs has four rows with the same phone, and the DID is what says
	which of the four this call belongs to.

	Sets `reference_*` ONLY, deliberately. `crm.api.activities.get_linked_calls` unions a
	`reference_docname` query with a `Dynamic Link` join, so also calling
	`link_with_reference_doc()` — as the old adapter did — returned the same call TWICE and
	rendered it twice in the Lead's Calls tab.
	"""
	lead = _lead_for(cdr)
	if not lead:
		return
	doc.reference_doctype = "CRM Lead"
	doc.reference_docname = lead


def _lead_for(cdr):
	"""The one lead this call belongs to, or None. Never a best guess."""
	phone = cdr.get("customer_number")
	account = cdr.get("account")
	# `phone_digits` already returns '' below 10 digits, so a garbage number can never become a
	# short suffix that matches half the lead table.
	if not phone or len(phone) < env.PHONE_MIN_DIGITS or not account:
		return None

	# Suffix-anchored: a lead's stored number may carry a +91 prefix, but the match must be on
	# the tail, never `%...%` on both ends (which would match a number merely CONTAINING these
	# digits).
	candidates = frappe.get_all("CRM Lead", filters={"mobile_no": ["like", f"%{phone}"]}, pluck="name")
	if not candidates:
		return None

	scoped = routing.leads_for_number_and_account(candidates, account)
	if len(scoped) == 1:
		return scoped[0]
	if len(scoped) > 1:
		# Two leads sharing a phone inside one grain is a data defect, not a routing choice.
		# Surface it; do not pick one.
		frappe.logger("telephony").warning(
			f"telephony: {len(scoped)} leads share {phone} on account {account}; call left unlinked"
		)
	return None
