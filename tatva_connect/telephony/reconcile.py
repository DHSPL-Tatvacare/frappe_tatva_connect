"""Acefone Call Report reconcile — the PULL half of call logging.

The hangup webhook is the real-time source, but recordings are often processed
AFTER hangup, so the webhook can land with an empty `recording_url`. Acefone's
Call Report API (`GET /v1/call-report`) is the authoritative pull source: each
record carries `recording_file_link` + `unique_id`. This module pulls a recent
window and:

  * backfills `recording_url` onto rows that are missing it, and
  * (optionally) recovers calls that have no row at all (a missed webhook),

by mapping each report row into the SAME payload shape the webhook handler reads
and feeding it through the idempotent `handler._process` — so all the
lead-linking / status / recording logic lives in one place.

SAFETY: Acefone's field names are inconsistent across products, so the exact id
that matches a webhook row to a report row must be pinned from a live capture
(see `_report_call_key`). Until then this runs MANUALLY with `dry_run=True` and
`create_missing=False`, so a wrong key can never create duplicates silently.
"""
import frappe
from frappe.utils import add_to_date, get_datetime, now_datetime

from tatva_connect.telephony import api as acefone
from tatva_connect.telephony import handler

# How far on either side of a report's timestamp we accept a number-match to an
# existing row when the id doesn't line up (seconds).
_TIME_MATCH_WINDOW_SEC = 120


def _norm_direction(call_hint) -> str:
	"""Acefone `call_hint` -> handler direction. Defaults to inbound."""
	h = str(call_hint or "").strip().lower()
	return "outbound" if h.startswith("out") else "inbound"


def _report_call_key(row: dict):
	"""The id on a report row that should equal the webhook's stored `call_id`.

	⚠️ NEEDS A LIVE CAPTURE TO PIN. Acefone labels `unique_id` as the call's
	unique id and `call_id` as the caller id in the Call Report — but the webhook
	uses `call_id` as the cross-trigger key. We default to `unique_id` and fall
	back to `call_id`; confirm against one live hangup webhook + one report row,
	then lock this to the single correct key.
	"""
	return row.get("unique_id") or row.get("call_id") or row.get("uuid")


def _report_to_payload(row: dict, direction: str) -> dict:
	"""Map a Call Report row into the webhook payload keys `handler._process` reads."""
	src = row.get("source") or row.get("caller_id_number")
	dst = row.get("destination") or row.get("call_to_number") or row.get("dest_num")
	# Inbound: customer is the caller (source) -> our DID (destination).
	# Outbound: our DID/caller is source -> customer is the destination.
	customer = src if direction == "inbound" else dst
	did = dst if direction == "inbound" else src
	return {
		"call_id": _report_call_key(row),
		"customer_number": customer,
		"did_number": did,
		"call_to_number": dst,
		"recording_url": row.get("recording_file_link") or row.get("recording_url"),
		"call_status": row.get("status") or row.get("call_status"),
		"hangup_cause": row.get("hangup_cause"),
		"duration": row.get("duration") or row.get("call_duration") or row.get("total_call_duration"),
		"start_stamp": row.get("connection_time") or row.get("start_stamp"),
		"end_stamp": row.get("date") or row.get("end_stamp"),
		"answered_agent_number": row.get("answered_agent_number") or row.get("call_answered_by"),
	}


def _rows_from_report(resp) -> list:
	"""Pull the list of call rows out of Acefone's response, tolerating shape.

	Logs the raw top-level shape once so the first live run reveals the real
	envelope (we pin it afterwards).
	"""
	if isinstance(resp, list):
		return resp
	if isinstance(resp, dict):
		for key in ("data", "results", "call_report", "rows", "records"):
			val = resp.get(key)
			if isinstance(val, list):
				return val
			if isinstance(val, dict) and isinstance(val.get("data"), list):
				return val["data"]
	frappe.log_error(title="Acefone call-report: unrecognised shape", message=str(resp)[:2000])
	return []


def _existing_row(key, customer_number, when):
	"""Find the CRM Call Log this report row corresponds to: by id, else by
	customer number within a tight time window around the call."""
	if key and frappe.db.exists("CRM Call Log", key):
		return key
	digits = acefone.normalize_number(customer_number)
	if not (digits and when):
		return None
	try:
		ts = get_datetime(when)
	except Exception:
		return None
	lo = add_to_date(ts, seconds=-_TIME_MATCH_WINDOW_SEC)
	hi = add_to_date(ts, seconds=_TIME_MATCH_WINDOW_SEC)
	rows = frappe.get_all(
		"CRM Call Log",
		filters={
			"telephony_medium": handler.TELEPHONY_MEDIUM,
			"creation": ["between", [lo, hi]],
		},
		or_filters={"from": ["like", f"%{digits[-10:]}%"], "to": ["like", f"%{digits[-10:]}%"]},
		limit=1,
		pluck="name",
	)
	return rows[0] if rows else None


def reconcile_window(from_date=None, to_date=None, dry_run=True, create_missing=False, max_pages=20):
	"""Pull the Call Report for [from_date, to_date] across all Acefone accounts and
	backfill recordings / recover missed calls.

	dry_run=True (default): touches nothing — logs what it WOULD do. Flip to False
	only after the live id check passes. create_missing=False: only backfill rows
	that already exist; never insert.
	"""
	if not acefone.is_enabled():
		return {"ok": False, "reason": "Acefone disabled"}

	now = now_datetime()
	to_date = to_date or now.strftime("%Y-%m-%d %H:%M:%S")
	from_date = from_date or add_to_date(now, hours=-24).strftime("%Y-%m-%d %H:%M:%S")

	summary = {"scanned": 0, "recording_backfilled": 0, "created": 0, "skipped_no_row": 0, "dry_run": bool(dry_run)}
	accounts = frappe.get_all("CRM Acefone Account", pluck="name")
	for account_name in accounts:
		account = frappe.get_doc("CRM Acefone Account", account_name)
		for page in range(1, max_pages + 1):
			resp = acefone.get_call_report(account, from_date=from_date, to_date=to_date, page=page, limit=100)
			rows = _rows_from_report(resp)
			if not rows:
				break
			for row in rows:
				summary["scanned"] += 1
				_reconcile_one(row, dry_run, create_missing, summary)
			if len(rows) < 100:
				break
	if not dry_run:
		frappe.db.commit()
	return summary


def _reconcile_one(row, dry_run, create_missing, summary):
	direction = _norm_direction(row.get("call_hint") or row.get("call_type"))
	payload = _report_to_payload(row, direction)
	when = payload.get("end_stamp") or payload.get("start_stamp")
	existing = _existing_row(payload["call_id"], payload["customer_number"], when)

	if existing:
		has_rec = bool(frappe.db.get_value("CRM Call Log", existing, "recording_url"))
		if payload["recording_url"] and not has_rec:
			summary["recording_backfilled"] += 1
			if not dry_run:
				handler._process(payload, direction=direction, completed=True)
		return
	if create_missing:
		summary["created"] += 1
		if not dry_run:
			handler._process(payload, direction=direction, completed=True)
	else:
		summary["skipped_no_row"] += 1


@frappe.whitelist()
def refresh_calls(hours=24, dry_run=1, create_missing=0):
	"""Manual reconcile entry point. Defaults to a safe dry-run over the last 24h.

	dry_run=1 logs only; create_missing=0 backfills existing rows but never inserts.
	"""
	now = now_datetime()
	from_date = add_to_date(now, hours=-int(hours)).strftime("%Y-%m-%d %H:%M:%S")
	return reconcile_window(
		from_date=from_date,
		to_date=now.strftime("%Y-%m-%d %H:%M:%S"),
		dry_run=bool(int(dry_run)),
		create_missing=bool(int(create_missing)),
	)


def scheduled_reconcile():
	"""Scheduler entry — backfill recordings + recover missed calls for the last 6h.

	⚠️ NOT WIRED in hooks.py yet — enable only after the live id check (see
	_report_call_key) confirms cross-source matching. Runs live (dry_run=False)
	with create_missing=True once trusted.
	"""
	return reconcile_window(dry_run=False, create_missing=True, from_date=None, to_date=None)
