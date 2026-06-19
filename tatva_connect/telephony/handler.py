"""Acefone webhook endpoints + outbound click-to-call.

Adapted from sanskar-onehash/crm_acefone_integration (MIT).

Unlike the OneHash app (which writes a parallel `Acefone Call Log`), we write
to frappe/crm's NATIVE `CRM Call Log` so calls render in the lead's Calls tab
with zero extra glue. Two seams:

  * Inbound/outbound CDR webhooks  -> the shared webhook spine, which raw-logs,
    fast-ACKs and enqueues; the per-vendor parse + idempotent CRM Call Log moves
    live in `tatva_connect.telephony.adapter`.
  * make_acefone_call(...)         -> create an Initiated Outgoing row, fire
    click-to-call carrying the row name as custom_identifier for correlation.

Acefone registers a SEPARATE webhook URL per trigger, so we expose four thin guest
endpoints; each is just the shared spine front door (kill-switch, token auth+identity,
raw-log, fast-ACK, enqueue) parametrised with the trigger's direction/completion as the
`event`. The worker (`spine.process`) hands the CDR to the adapter — so a processing
failure lands in the RQ failed registry (the DLQ), NOT swallowed behind a 200.

Register the pretty, per-account URLs on each tenant's Acefone dashboard (one per trigger;
generate the token and copy the URLs from the Telephony Account form):
    https://<host>/webhooks/telephony/<provider>/<token>/inbound_answered
    https://<host>/webhooks/telephony/<provider>/<token>/inbound_complete
    https://<host>/webhooks/telephony/<provider>/<token>/outbound_answered
    https://<host>/webhooks/telephony/<provider>/<token>/outbound_complete
where <token> == that Telephony Account's `webhook_token`. nginx rewrites the path to the
native endpoint with ?token=; the token both authenticates the caller and names the receiving
account (routing.account_by_webhook_token) — auth + identity in one, mirroring the WhatsApp
webhook. No dependence on the CDR's did_number for auth.
"""
import frappe
from frappe import _
from frappe.rate_limiter import rate_limit

from tatva_connect.telephony import adapter
from tatva_connect.telephony import api as acefone
from tatva_connect.telephony import routing
from tatva_connect.webhooks import spine

# TATVA L2: removed the TELEPHONY_MEDIUM / _process re-export shims (Invariant 14).
# reconcile.py + observability/capture.py now import these from telephony.adapter directly.


# ---------------------------------------------------------------------------
# Guest webhook endpoints (one per Acefone trigger) — thin spine front doors
# ---------------------------------------------------------------------------
@frappe.whitelist(allow_guest=True)
@rate_limit(key="token", limit=120, seconds=60, ip_based=True)
def inbound_answered(**kwargs):
	return _receive("inbound_answered")


@frappe.whitelist(allow_guest=True)
@rate_limit(key="token", limit=120, seconds=60, ip_based=True)
def inbound_complete(**kwargs):
	return _receive("inbound_complete")


@frappe.whitelist(allow_guest=True)
@rate_limit(key="token", limit=120, seconds=60, ip_based=True)
def outbound_answered(**kwargs):
	return _receive("outbound_answered")


@frappe.whitelist(allow_guest=True)
@rate_limit(key="token", limit=120, seconds=60, ip_based=True)
def outbound_complete(**kwargs):
	return _receive("outbound_complete")


def _receive(event: str):
	"""Hand the trigger to the shared spine. `event` carries direction + completion; the
	adapter recovers both from it. The spine does kill-switch, token auth+identity, raw-log,
	fast-ACK and enqueue."""
	return spine.receive(
		"Acefone",
		enabled=acefone.is_enabled,
		resolve_account=routing.account_by_webhook_token,
		adapter=adapter,
		event=event,
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
	from tatva_connect.telephony import bridge

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
