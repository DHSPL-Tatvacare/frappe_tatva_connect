"""Acefone reconcile — the PULL half of call logging.

The hangup webhook is the real-time source, but a delivery can be missed and a recording is often
processed after hangup. The Call Detail Records API (`GET /v1/call/records`) is the authoritative pull
source. This module pulls a recent window, maps each record into the SAME payload shape the webhook
adapter reads, and feeds it through the SAME entry the webhook worker feeds — `adapter.process`, which
screens, normalizes, resolves and writes. The gates, the field mapping, the lead-linking, the status,
the agent and the recording logic therefore live in exactly one place, and a pulled call can never
disagree with the pushed one.

The two paths share that logic and nothing else, which is the point. A webhook arrives over HTTP from
an untrusted caller, so it enters through the spine and is authenticated, raw-logged and replayable. A
record is fetched by us, with our own token, from the source of truth — there is no delivery to
authenticate and none to replay, and routing the pull through the webhook spine would fabricate both.
The front doors are separate on purpose; the brain behind them is one. The same asymmetry is documented
in `whatsapp/backfill.py`.

The record shape is PINNED against a live response (account 214181): `call_id` is the same key the
webhook sends, `client_number` is always the customer, `did_number` always ours, and `call_flow`
carries the agent's email. Nothing in the mapping is a guess. It still defaults to `dry_run=True`,
because a pull that writes on its first run is not a safe default.
"""
import frappe
from frappe.utils import add_to_date, now_datetime

from tatva_connect.telephony.adapters import acefone as adapter
from tatva_connect.telephony import api as acefone


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

	A record's `call_flow` carries Agent entries with the agent's EMAIL, which is the only
	identifier that resolves to a CRM user. It is the same email the webhook sends, so a reconciled
	call attributes its rep exactly as a live one does.
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
	webhook slots the adapter reads for that direction. Pinned against a live record; nothing here is
	a guess any more.
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



def reconcile_window(from_date=None, to_date=None, dry_run=True, max_pages=20):
	"""Pull every call record in [from_date, to_date] for each account and feed it to the adapter.

	`dry_run=True` (the default) reports what it would do and writes nothing.

	There is no `create_missing` switch any more. It drew a line the design does not have: a record is
	either wanted -- its DID is mapped and a capture rule accepts it -- or it is not, and the same gates
	decide that whether the call was pushed or pulled. "Recover only the ones we already have" was a
	hedge against a call key we were not sure of; the key is pinned now.
	"""
	if not acefone.is_enabled():
		return {"ok": False, "reason": "Acefone disabled"}

	now = now_datetime()
	to_date = to_date or now.strftime("%Y-%m-%d %H:%M:%S")
	from_date = from_date or add_to_date(now, hours=-24).strftime("%Y-%m-%d %H:%M:%S")

	summary = {"scanned": 0, "written": 0, "declined": 0, "failed": 0, "dry_run": bool(dry_run)}
	for account_name in frappe.get_all("CRM Telephony Account", filters={"enabled": 1}, pluck="name"):
		account = frappe.get_doc("CRM Telephony Account", account_name)
		for page in range(1, max_pages + 1):
			resp = acefone.get_call_records(account, from_date=from_date, to_date=to_date, page=page, limit=100)
			rows = _rows_from_report(resp)
			if not rows:
				break
			for row in rows:
				summary["scanned"] += 1
				_reconcile_one(row, account_name, dry_run, summary)
			if len(rows) < 100:
				break
	return summary


def _reconcile_one(row, account, dry_run, summary):
	"""One record, through the adapter entry the webhook worker uses.

	`adapter.process` asks the capture gates itself and returns None when the call is not ours, so the
	pull cannot admit a call the push would have dropped. The writer keys on `call_id`, so a call that
	is already logged is updated rather than duplicated, and there is nothing here to match by hand.
	"""
	payload = _report_to_payload(row, _norm_direction(row))

	if dry_run:
		wanted, _reason = adapter.screen(payload, None, account)
		summary["written" if wanted else "declined"] += 1
		return

	try:
		written = adapter.process(payload, account=account)
	except Exception:
		# One malformed record must not abandon the rest of the window. The partial write is rolled back
		# and the traceback kept, so the record can be pulled again once the cause is fixed.
		frappe.db.rollback()
		summary["failed"] += 1
		frappe.log_error(title="Acefone reconcile: record failed", message=frappe.get_traceback())
		return

	# Committed per record, so a window that dies part way through keeps every call it had already
	# written rather than rolling the whole pull back.
	frappe.db.commit()
	summary["written" if written else "declined"] += 1


@frappe.whitelist()
def refresh_calls(hours=24, dry_run=1):
	"""Manual reconcile entry point. Defaults to a safe dry-run over the last 24h."""
	# Manual operator tool: reconciles a telephony account against its provider API. Gate on read of
	# CRM Telephony Account (manager/admin-only) — blocks reps/no-role from driving the external API.
	# Gate lives on this manual wrapper only; reconcile_window (webhook/scheduler path) stays ungated.
	frappe.has_permission("CRM Telephony Account", "read", throw=True)
	now = now_datetime()
	from_date = add_to_date(now, hours=-int(hours)).strftime("%Y-%m-%d %H:%M:%S")
	return reconcile_window(
		from_date=from_date,
		to_date=now.strftime("%Y-%m-%d %H:%M:%S"),
		dry_run=bool(int(dry_run)),
	)


def scheduled_reconcile():
	"""Scheduler entry — catch up the last 24h of calls a webhook may have missed.

	NOT WIRED to `hooks.scheduler_events`. Wiring it needs a dormant toggle in the automation registry
	and a go-live checklist row, which is the app's rule for anything that runs by itself. Until then
	reconcile is the operator's manual button.
	"""
	return reconcile_window(dry_run=False)
