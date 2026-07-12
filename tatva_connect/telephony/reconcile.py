"""Acefone reconcile — the PULL half of call logging. The mirror of `whatsapp/backfill.py`.

The hangup webhook is the real-time source, but a delivery can be missed. The Call Detail Records API
(`GET /v1/call/records`) is the authoritative pull source. A pull starts from a LEAD, resolves that
lead's telephony account through `routing` — the same grain-to-account rule an outbound call uses —
pulls that account's records, keeps the ones carrying the lead's number, and feeds each through
`adapter.process`, the same entry the webhook worker uses. Every path in the app therefore reaches a
call the same way, and a pulled call can never disagree with a pushed one.

RECONCILE NEVER DELETES. `CRM Call Log` is a MIXED table: rows this app writes from a provider (keyed
on the provider's `call_id`) sit beside rows a rep typed by hand (`telephony_medium = Manual`, no
provider key). The obvious way to write a reconciler — clear the window, refetch it — is safe only when
the provider is the single source, which is why WhatsApp can get away with a coarser one. Here it would
destroy the reps' own work, silently and irrecoverably: a manual row carries no `call_id`, so no refetch
can bring it back. The pull is strictly an upsert on the provider's key, and it may only ever touch a
row it created. `tests/telephony/test_reconcile_never_deletes.py` holds this.

The window is an implementation detail, not a mode. Acefone's API takes a date range and IGNORES a
customer-number filter (`client_number`, `customer_number` and `caller_id` were each sent against live
traffic and each returned the unfiltered window), so the lead's calls are selected here, on the number.

The record shape is PINNED against a live response (account 214181): `call_id` is the same key the
webhook sends, `client_number` is always the customer, `did_number` always ours, and `call_flow` carries
the agent's email. Nothing in the mapping is a guess.
"""
import frappe
from frappe.utils import add_to_date, now_datetime

from tatva_connect import automation
from tatva_connect.telephony import api as acefone
from tatva_connect.telephony import envelope as env
from tatva_connect.telephony import routing, writer
from tatva_connect.telephony.adapters import acefone as adapter

CALL_LOG = "CRM Call Log"

# One page of the CDR API, and the ceiling on how many are walked for one lead.
PAGE_SIZE = 100
MAX_PAGES = 20


def _norm_direction(row: dict) -> str:
	"""The record's own `direction`. It says "inbound"/"outbound" outright."""
	return "outbound" if "outbound" in str(row.get("direction") or "").casefold() else "inbound"


def _webhook_direction(row: dict, direction: str) -> str:
	"""Rebuild the direction string the WEBHOOK would have sent for this call.

	The two sources carry the same fact differently. A webhook says "Dialer (inbound)"; a record says
	`direction: inbound` plus `call_hint: dialer`. Rebuilding the webhook's phrasing here means the
	adapter's one parser derives the channel for both paths — otherwise the same call would be Dialer
	when pushed and IVR when pulled, and a channel-scoped capture rule would treat them differently.
	"""
	return f"Dialer ({direction})" if str(row.get("call_hint") or "").casefold() == "dialer" else direction


def _agents_from_flow(row: dict) -> list:
	"""The agents who touched the call, in the webhook's `answered_agent` shape.

	A record's `call_flow` carries Agent entries with the agent's EMAIL, which is the only identifier
	that resolves to a CRM user. It is the same email the webhook sends, so a reconciled call attributes
	its rep exactly as a live one does.
	"""
	agents = []
	for entry in row.get("call_flow") or []:
		if not isinstance(entry, dict) or entry.get("type") != "Agent":
			continue
		email = (entry.get("email") or "").strip()
		if email:
			agents.append({"name": entry.get("name"), "email": email, "number": entry.get("num")})
	return agents


def _report_to_payload(row: dict, direction: str) -> dict:
	"""Map a Call Detail Record into Acefone's own WEBHOOK key vocabulary.

	Emits the provider's webhook keys and lets `adapters.acefone.normalize` do the direction logic,
	rather than pre-computing customer and DID here. One brain: the pull and the push can never drift
	apart on which number is the customer.

	A record is clearer than a webhook about who is who -- `client_number` is always the customer and
	`did_number` always ours, whichever way the call went -- so the two are simply placed into the
	webhook slots the adapter reads for that direction.
	"""
	customer = row.get("client_number")
	did = row.get("did_number")

	return {
		# The same key the webhook sends. Confirmed identical on a live record.
		"call_id": row.get("call_id"),
		"uuid": row.get("uuid"),
		# The webhook's phrasing, so the adapter's one parser derives the channel for both paths.
		"direction": _webhook_direction(row, direction),
		# Inbound the customer calls our DID; outbound we dial the customer from it.
		"caller_id_number": customer if direction == "inbound" else did,
		"call_to_number": did if direction == "inbound" else customer,
		# Read by `account_for_payload` to resolve the account off the DID map.
		"did_number": did,
		"call_status": row.get("status"),
		"call_connected": "1" if str(row.get("status") or "").casefold() == "answered" else "0",
		"hangup_cause": row.get("hangup_cause"),
		"duration": row.get("call_duration") or row.get("total_call_duration"),
		# The record splits the start into date + time; the webhook sends one stamp.
		"start_stamp": f"{row.get('date')} {row.get('time')}".strip() if row.get("date") else None,
		"end_stamp": row.get("end_stamp"),
		"recording_url": row.get("recording_url"),
		"answered_agent": _agents_from_flow(row),
		"answered_agent_name": row.get("agent_name"),
		"answered_agent_number": row.get("agent_number"),
	}


def _rows_from_report(resp) -> list:
	"""The call records out of Acefone's response. The envelope is `{count, limit, page, results}`."""
	if isinstance(resp, dict) and isinstance(resp.get("results"), list):
		return resp["results"]
	if isinstance(resp, list):
		return resp
	frappe.log_error(title="Acefone call records: unrecognised shape", message=str(resp)[:2000])
	return []


def _records_for_number(account_doc, number: str, days: int) -> list:
	"""This lead's records out of the account's window.

	The API takes a date range and ignores a customer-number filter, so the match is made here, on the
	last-10 digits — the same reduction `envelope.phone_digits` applies to every number in the app.
	"""
	now = now_datetime()
	from_date = add_to_date(now, days=-int(days)).strftime("%Y-%m-%d %H:%M:%S")
	to_date = now.strftime("%Y-%m-%d %H:%M:%S")

	mine = []
	for page in range(1, MAX_PAGES + 1):
		resp = acefone.get_call_records(
			account_doc, from_date=from_date, to_date=to_date, page=page, limit=PAGE_SIZE
		)
		rows = _rows_from_report(resp)
		if not rows:
			break
		mine.extend(r for r in rows if env.phone_digits(r.get("client_number")) == number)
		if len(rows) < PAGE_SIZE:
			break
	else:
		# The ceiling was reached with records still unread. Silently returning a partial window would
		# read as "this lead has no more calls", so it is said out loud.
		frappe.log_error(
			title="Acefone reconcile: window truncated",
			message=f"{account_doc.name}: stopped at {MAX_PAGES} pages of {PAGE_SIZE} over {days}d",
		)
	return mine


def reconcile_lead(lead_name: str, days: int = 7, dry_run: bool = True) -> dict:
	"""Pull one lead's calls from its routed account and write in any the webhook missed.

	`dry_run=True` (the default) counts only and touches nothing. Nothing is ever deleted: a record
	already on the lead updates its own row, keyed on the provider's `call_id`, and a manual row is not
	addressable by that key at all.
	"""
	if not acefone.is_enabled():
		return {"ok": False, "reason": "Acefone disabled"}

	lead = frappe.get_cached_doc("CRM Lead", lead_name)
	account = routing.resolve_account_for_lead(lead)
	if not account:
		return {"ok": False, "reason": "no telephony route for this lead"}
	number = env.phone_digits(lead.get("mobile_no"))
	if not number:
		return {"ok": False, "reason": "lead has no mobile number"}

	account_doc = frappe.get_doc("CRM Telephony Account", account)
	rows = _records_for_number(account_doc, number, days)

	summary = {"ok": True, "lead": lead_name, "account": account, "scanned": 0, "new": 0,
	           "existing": 0, "declined": 0, "failed": 0, "dry_run": bool(dry_run)}
	for row in rows:
		summary["scanned"] += 1
		_reconcile_one(row, account, dry_run, summary)
	return summary


def _reconcile_one(row, account, dry_run, summary):
	"""One record, through the adapter entry the webhook worker uses.

	`adapter.process` asks the capture gates itself and returns None when the call is not ours, so the
	pull cannot admit a call the push would have dropped, and it upserts on `call_id`, so a call already
	logged is updated rather than duplicated.
	"""
	payload = _report_to_payload(row, _norm_direction(row))
	known = bool(writer.row_for_key(payload.get("call_id") or payload.get("uuid")))

	if dry_run:
		wanted, _reason = adapter.screen(payload, None, account)
		if not wanted:
			summary["declined"] += 1
		else:
			summary["existing" if known else "new"] += 1
		return

	try:
		written = adapter.process(payload, account=account)
	except Exception:
		# One malformed record must not abandon the rest of the lead's window. The partial write is
		# rolled back and the traceback kept, so the record can be pulled again once the cause is fixed.
		frappe.db.rollback()
		summary["failed"] += 1
		frappe.log_error(title="Acefone reconcile: record failed", message=frappe.get_traceback())
		return

	if not written:
		summary["declined"] += 1
		return
	# Committed per record, so a window that dies part way through keeps every call it had written.
	frappe.db.commit()
	summary["existing" if known else "new"] += 1


@frappe.whitelist()
def refresh_calls(reference_name: str, dry_run=1) -> dict:
	"""Manual entry — pull one lead's calls and write in anything the webhook missed.

	Defaults to a safe dry-run. Requires WRITE access to the lead, so a caller who cannot act on the
	lead cannot probe its call history through the provider either.
	"""
	frappe.has_permission("CRM Lead", "write", doc=reference_name, throw=True)
	return reconcile_lead(reference_name, dry_run=bool(int(dry_run)))


def scheduled_reconcile(hours: int = 24) -> dict:
	"""Scheduler entry — top up recently-active leads from the call records API.

	DORMANT and NOT WIRED in hooks.py. Gated by the `Telephony::Acefone::reconcile` switch, which is OFF
	by default. The operator arms it: turn the switch on and register a Scheduled Job Type for this
	method with the chosen cron. A no-op until then, even if called.
	"""
	if not automation.is_enabled("Telephony::Acefone::reconcile"):
		return {"ok": False, "reason": "Telephony::Acefone::reconcile disabled"}

	since = add_to_date(now_datetime(), hours=-int(hours))
	leads = frappe.get_all(
		CALL_LOG,
		filters={"reference_doctype": "CRM Lead", "modified": [">=", since]},
		distinct=True,
		pluck="reference_name",
	)
	summary = {"ok": True, "leads": 0, "new": 0}
	for lead in leads:
		if not lead:
			continue
		summary["leads"] += 1
		res = reconcile_lead(lead, dry_run=False)
		if isinstance(res, dict):
			summary["new"] += res.get("new", 0)
	return summary
