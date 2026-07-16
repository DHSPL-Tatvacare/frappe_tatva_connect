# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The partner_bulk worker: drain one CRM Bulk Job through the SAME create brain the sync endpoints use.

Runs on its OWN RQ queue (the bulkhead) so a backfill never touches the web pool, and AS the job's
partner so grain scope and ownership follow the identical brain. Reads the job's payload file, parses it,
and commits in chunks under a deadlock-retry with READ COMMITTED — so the whole tier runs serially
without a 503 storm. Terminal states follow Salesforce Bulk API 2.0 (JobComplete | Failed | Aborted).
"""
import json
import time

import frappe
from frappe import _
from frappe.utils import add_to_date, now_datetime
from frappe.utils.csvutils import read_csv_content

from tatva_connect.api._base import (
	BulkDeadlock,
	_cfg,
	_process_bulk,
	_resolve_caller,
	async_volume_exhausted,
)
from tatva_connect.automation import settings as automation
from tatva_connect.storage import file_screening

_ASYNC_REAPER = "Partner::AsyncBulk::reaper"  # dormant toggle for the stranded-InProgress reaper


class PayloadRejected(Exception):
	"""The payload was refused whole (virus / disallowed type, or over the record cap) — the file is
	purged and the job Failed, with nothing parsed."""


def process_job(bulk_job_id):
	"""Drain one job. Idempotent: a re-enqueue of an already-started or finished job is a no-op.

	The param is `bulk_job_id`, not `job_id`: `job_id` is a RESERVED frappe.enqueue kwarg (it sets the
	RQ job's own id), so a value passed as job_id never reaches this function."""
	job = frappe.get_doc("CRM Bulk Job", bulk_job_id)
	if job.status != "UploadComplete":
		return  # cheap early-out; the guarded claim below is the authoritative writer election
	frappe.set_user(job.partner)  # grain scope + ownership from the same brain the sync endpoint uses
	frappe.db.sql("SET SESSION TRANSACTION ISOLATION LEVEL READ COMMITTED")  # disarm insert gap-locks
	won = _claim_started(job.name)  # exactly one of {this worker, a pre-start cancel} may leave UploadComplete
	frappe.db.commit()
	if not won:
		return  # a pre-start cancel aborted it first — nothing to drain
	try:
		raw = _payload_bytes(job)
		_screen(job, raw)  # ClamAV + magic-byte, before a byte is parsed; a block raises PayloadRejected
		_drain(job, _to_batch(job, raw))
	except PayloadRejected as exc:
		_purge_payload(job)
		finish_job(job.name, "Failed", error=f"File rejected: {exc}"[:500])
	except Exception as exc:
		_rollback()  # drop the failed chunk's stale webhook queue so finish_job re-registers the flush (B1)
		finish_job(job.name, "Failed", error=f"{type(exc).__name__}: {exc}"[:500])
		frappe.log_error(title=f"Partner bulk job {job.name} failed")


def _drain(job, items):
	"""Process the payload in chunks; commit each; between chunks honour a cancel request AND the async
	write budget (the same limit brain). A record that will not parse is a per-record failure, not a crash."""
	chunk_size = _cfg()["async_chunk_records"]
	_user, mp, _is = _resolve_caller()
	creator = _creator(job.operation)
	processed = succeeded = failed = 0
	for start in range(0, len(items), chunk_size):
		if frappe.db.get_value("CRM Bulk Job", job.name, "cancel_requested"):
			_compensate_and_abort(job)
			return
		batch = items[start:start + chunk_size]
		if async_volume_exhausted(len(batch), mp):
			finish_job(job.name, "Failed", error="Async write quota exhausted; retry after the daily window.")
			return
		records, parse_fails = _parse(batch)
		results, summary = _retry_deadlock(lambda: _process_bulk([rec for _o, rec in records], creator))
		_write_results(job.name, start, records, results, parse_fails)
		processed += len(batch)
		succeeded += summary["succeeded"]
		failed += summary["failed"] + len(parse_fails)
		frappe.db.set_value("CRM Bulk Job", job.name,
		                    {"processed": processed, "succeeded": succeeded, "failed": failed},
		                    update_modified=False)
		frappe.db.commit()
	finish_job(job.name, "JobComplete")


def _parse(batch):
	"""A record is a CSV dict (already parsed) or a JSONL string (parsed here, tolerantly). A JSON decode
	error is a per-record failure (plan §9), not a crash. Returns (records:[(offset,dict)], fails:[(offset,msg)])."""
	records, fails = [], []
	for offset, item in enumerate(batch):
		if isinstance(item, dict):
			if "__error__" in item:
				fails.append((offset, item["__error__"]))  # a ragged CSV row
			else:
				records.append((offset, item))
		else:
			try:
				records.append((offset, json.loads(item)))
			except Exception as exc:
				fails.append((offset, str(exc)[:200]))
	return records, fails


def _retry_deadlock(fn, tries=4):
	"""Run fn; on a deadlock (the whole chunk txn was rolled back) retry with backoff, per MariaDB's own
	'restart the transaction' guidance. READ COMMITTED makes these rare; the retry mops up the rest."""
	for attempt in range(tries):
		try:
			return fn()
		except BulkDeadlock:
			_rollback()  # the chunk's inserts (and their queued webhooks) are gone; the retry re-queues fresh
			if attempt == tries - 1:
				raise
			time.sleep(0.15 * (attempt + 1))


def _creator(operation):
	"""The resource's OWN per-record create closure — the same factory the sync bulk endpoint calls."""
	if operation == "lead_create":
		from tatva_connect.api import partner
		user, mp, is_sysmgr, parent_fields, child_allow = partner._caller_fields()
		return partner.bulk_creator(user, mp, is_sysmgr, parent_fields, child_allow,
		                            partner._allowed_programs(user, bool(mp)))
	from tatva_connect.api import partner_activity
	_user, mp, is_sysmgr = _resolve_caller()
	return partner_activity.bulk_creator(mp, is_sysmgr)


def _write_results(job_name, start, records, results, parse_fails):
	"""One CRM Bulk Job Result per record. `results` is index-aligned with `records` (the parsed rows);
	`parse_fails` are lines that never parsed. Both addressed by their input row index."""
	for r in results:
		offset = records[r["index"]][0]
		if r["status"] == "success":
			_insert_result(job_name, start + offset,
			               "merged" if r.get("action") == "updated" else "created",
			               record_name=(r.get("data") or {}).get("name"))
		else:
			err = r.get("error") or {}
			_insert_result(job_name, start + offset, "failed",
			               error_code=err.get("code"), error_message=(err.get("message") or "")[:500])
	for offset, msg in parse_fails:
		_insert_result(job_name, start + offset, "failed", error_code="bad_request", error_message=msg)


def _insert_result(job_name, record_index, action, record_name=None, error_code=None, error_message=None):
	row = frappe.new_doc("CRM Bulk Job Result")
	row.update({"job": job_name, "record_index": record_index, "action": action,
	            "record_name": record_name, "error_code": error_code, "error_message": error_message})
	row.insert(ignore_permissions=True)  # authz-ok: tier-b — system bookkeeping, gated by the job


def _payload_bytes(job):
	"""The job's payload bytes, from its OWN attached file (M2: via FileOverride)."""
	name = _payload_file(job.name)
	if not name:
		return b""
	content = frappe.get_doc("File", name).get_content()
	return content.encode("utf-8") if isinstance(content, str) else content


def _screen(job, raw):
	"""Screen the payload via the existing brain (ClamAV + magic-byte + allowlist) BEFORE a byte is
	parsed. Inline bytes are our OWN validated serialization (never an upload) so they are not screened.
	The screener throws on a block; translate it to PayloadRejected so the caller purges + fails."""
	if not raw or job.input_format == "inline":
		return
	try:
		file_screening.screen(file_name=f"{job.name}.{job.input_format}", raw=raw,
		                      channel="Partner API", source=job.partner)
	except frappe.ValidationError as exc:
		raise PayloadRejected(str(exc)) from exc


def _to_batch(job, raw):
	"""The batch: CSV -> flat lead-core dicts (native reader); else -> raw JSONL lines parsed per-chunk.
	Enforces the record cap cheaply (a newline count, before materialising) and sets `total`."""
	cap = _cfg()["async_file_max_records"]
	if raw.count(b"\n") + 1 > cap:  # upper bound on record count, before the file is materialised
		raise PayloadRejected(_("Exceeds the {0} record limit.").format(cap))
	if job.input_format == "csv":
		batch = _csv_records(raw)
	else:
		text = raw.decode("utf-8", "ignore") if isinstance(raw, bytes) else raw
		batch = [line for line in text.splitlines() if line.strip()]
	frappe.db.set_value("CRM Bulk Job", job.name, "total", len(batch), update_modified=False)
	return batch


def _csv_records(raw):
	"""Flat lead-core CSV -> dicts via the native reader; the header keys each row. A row whose column
	count does not match the header becomes a per-record failure, never a silently mis-mapped record."""
	rows = [r for r in read_csv_content(raw) if r]
	if not rows:
		return []
	header = [h.strip() for h in rows[0]]
	out = []
	for r in rows[1:]:
		if len(r) != len(header):
			out.append({"__error__": _("row has {0} columns, expected {1}").format(len(r), len(header))})
		else:
			out.append(dict(zip(header, r, strict=False)))
	return out


def _payload_file(job_name):
	return frappe.db.get_value(
		"File", {"attached_to_doctype": "CRM Bulk Job", "attached_to_name": job_name}, "name")


def _purge_payload(job):
	"""Drop the job's attached payload file — a blocked upload is never kept."""
	name = _payload_file(job.name)
	if name:
		frappe.delete_doc("File", name, force=True, ignore_permissions=True)  # authz-ok: tier-b — gated by process_job's UploadComplete guard + job ownership
	frappe.db.commit()


def _compensate_and_abort(job):
	"""Clean cancel: delete only the records THIS job created (never a merge onto a pre-existing lead),
	via the resource's OWN delete brain, later rows first; then end Aborted."""
	created = frappe.get_all("CRM Bulk Job Result", filters={"job": job.name, "action": "created"},
	                         order_by="record_index desc", pluck="record_name")
	delete_one = _deleter(job.operation)
	for name in created:
		if not name:
			continue
		try:
			delete_one(name)
			frappe.db.commit()
		except Exception:
			_rollback()  # already gone or blocked — skip, never fail the cleanup
	finish_job(job.name, "Aborted")


def _deleter(operation):
	"""The resource's OWN delete-one, bound to the caller — reused, not re-implemented."""
	from tatva_connect.api import partner, partner_activity
	_user, mp, is_sysmgr = _resolve_caller()
	delete_one = partner._delete_one if operation == "lead_create" else partner_activity._delete_one
	return lambda name: delete_one(name, mp, is_sysmgr)


def _rollback():
	"""Roll back AND drop the webhook queue together. A bare rollback resets frappe.db.after_commit but
	NOT frappe.local._webhook_queue, so a later save would append to a queue whose flush is unregistered
	and the completion webhook would never fire (and the rolled-back writes' webhooks would linger)."""
	frappe.db.rollback()
	frappe.local._webhook_queue = None


def _claim_started(job_name):
	"""Atomic start-claim: flip UploadComplete -> InProgress, winning against a racing pre-start cancel.
	True iff THIS worker won the row; a lost claim means a cancel aborted it first, so don't drain."""
	frappe.db.sql("""UPDATE `tabCRM Bulk Job` SET status='InProgress', started_at=%s
	                 WHERE name=%s AND status='UploadComplete'""", (now_datetime(), job_name))
	return frappe.db.sql("SELECT ROW_COUNT()")[0][0] == 1


def finish_job(job_name, status, error=None, commit=True, guard_pre_start=False):
	"""The ONE terminal transition (JobComplete | Failed | Aborted). Saves via the ORM so the native
	Webhook fires on_update; a save-time rule that throws falls back to a direct write so a job is NEVER
	stranded InProgress. guard_pre_start (the cancel's pre-start abort): applied ONLY if the row is still
	Open/UploadComplete -- an atomic claim that LOSES to a worker that already started; returns False when
	lost. Worker callers commit; the endpoint lets the request finalizer commit."""
	if guard_pre_start:
		frappe.db.sql("""UPDATE `tabCRM Bulk Job` SET status=%s
		                 WHERE name=%s AND status IN ('Open', 'UploadComplete')""", (status, job_name))
		if frappe.db.sql("SELECT ROW_COUNT()")[0][0] != 1:
			return False  # the worker already claimed it; caller falls through to cooperative cancel
	doc = frappe.get_doc("CRM Bulk Job", job_name)
	doc.status = status
	doc.finished_at = now_datetime()
	if error:
		doc.error_summary = error
	try:
		doc.save(ignore_permissions=True)  # authz-ok: tier-b — gated by process_job's UploadComplete guard + cancel's _owned_job; ORM save fires the webhook
	except Exception:
		_rollback()  # a save-time automation rule threw; force the terminal state (no webhook) over stranding at InProgress
		frappe.db.set_value("CRM Bulk Job", job_name,
		                    {"status": status, "finished_at": now_datetime(), "error_summary": error},
		                    update_modified=False)
		frappe.log_error(title=f"Bulk job {job_name} terminal save failed; forced {status}")
	if commit:
		frappe.db.commit()
	return True


def reap_stranded_jobs():
	"""Scheduler (gated, dormant): a job still InProgress past the job timeout means its worker died
	(deploy / OOM / kill) — RQ would have stopped it by then — so mark it Failed and drop its payload,
	rather than let it sit half-done forever. finish_job fires the webhook so the partner is told."""
	if not automation.is_enabled(_ASYNC_REAPER):
		return
	cutoff = add_to_date(now_datetime(), seconds=-_cfg()["async_job_timeout_seconds"])
	stranded = frappe.get_all("CRM Bulk Job",
	                          filters={"status": "InProgress", "started_at": ["<", cutoff]}, pluck="name")
	for name in stranded:
		_purge_payload(frappe.get_doc("CRM Bulk Job", name))
		finish_job(name, "Failed", error="Worker did not finish within the job timeout (reaped).")
	if stranded:
		frappe.db.commit()
