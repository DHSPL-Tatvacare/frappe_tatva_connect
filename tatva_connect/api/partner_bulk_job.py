# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The async bulk-job tier's HTTP surface: submit -> status -> results -> cancel.

A submit is CHEAP (validate the envelope, store the batch as the job's own private file, enqueue) so it
never holds a web worker on the batch itself; the partner_bulk worker drains the job through the SAME
create brain the sync endpoints use. Dormant until Partner::AsyncBulk::jobs is enabled. States follow
Salesforce Bulk API 2.0 (Open -> UploadComplete -> InProgress -> JobComplete | Failed | Aborted).
"""
import base64
import binascii
import json

import frappe
from frappe import _
from frappe.utils import add_to_date, now_datetime

from tatva_connect.api._base import (
	_ASYNC_BULK,
	ACTION_CREATED,
	ACTION_FETCHED,
	ACTION_UPDATED,
	_api,
	_cfg,
	_fail,
	_idem_key,
	_list_ok,
	_ok,
	_page,
	_request,
	_resolve_caller,
)
from tatva_connect.automation import settings as automation

_OPERATIONS = ("lead_create", "activity_create")  # Phase 1-2; call/note/file_attach are later phases
_FORMATS = ("inline", "csv", "jsonl")  # csv is flat lead-core only; activity needs jsonl
_NON_TERMINAL = ("Open", "UploadComplete", "InProgress")
_TERMINAL = ("JobComplete", "Failed", "Aborted")
_ASYNC_PURGE = "Partner::AsyncBulk::purge"  # dormant toggle for the retention purge


def _job_id():
	return frappe.form_dict.get("job_id")


def _owned_job(job_id, user, mp):
	"""The one owner-scoped loader: a partner sees only its own jobs; missing and out-of-scope answer the
	SAME generic not-found. A trusted sysmgr (no mapping) sees any."""
	job = frappe.db.get_value(
		"CRM Bulk Job", job_id,
		["name", "partner", "status", "operation", "total", "processed", "succeeded", "failed",
		 "error_summary", "submitted_at", "started_at", "finished_at"],
		as_dict=True,
	)
	if not job or (mp and job.partner != user):
		return None
	return job


def _attach_payload(job_name, content, fmt):
	"""Store the submitted batch as the job's OWN private file (M1: owned by the job, dies with it), so
	inline and uploaded jobs share one read path. The privacy floor keeps it private regardless."""
	ext = "csv" if fmt == "csv" else "jsonl"
	doc = frappe.new_doc("File")
	doc.file_name = f"{job_name}.{ext}"
	doc.attached_to_doctype = "CRM Bulk Job"
	doc.attached_to_name = job_name
	doc.is_private = 1
	doc.content = content
	doc.insert(ignore_permissions=True)  # authz-ok: tier-b — gated by _resolve_caller; file owned by the job


def _uploaded_bytes(cfg):
	"""The uploaded file's bytes, bounded to the byte cap AS they are read (never buffering an oversized
	body whole): multipart (frappe.request.files) or content_base64 (parity with the sync file endpoint,
	and testable). None if neither is present."""
	max_bytes = cfg["async_file_max_mb"] * 1024 * 1024
	over = _("File exceeds the {0} MB limit.").format(cfg["async_file_max_mb"])
	req = _request()
	files = getattr(req, "files", None) if req else None
	if files:
		for f in files.values():
			if f.content_length and f.content_length > max_bytes:
				frappe.throw(over)
			raw = f.stream.read(max_bytes + 1)
			if len(raw) > max_bytes:
				frappe.throw(over)
			return raw
	b64 = frappe.form_dict.get("content_base64")
	if b64:
		if len(b64) * 3 // 4 > max_bytes:  # base64 expands ~4/3; reject before decoding
			frappe.throw(over)
		try:
			return base64.b64decode(b64, validate=True)
		except (binascii.Error, ValueError):
			frappe.throw(_("content_base64 is not valid base64"))
	return None


def guard_webhook_url(doc, method=None):
	"""SSRF-guard a bulk-job completion webhook: a partner's outbound URL may not resolve to an internal
	or metadata address. Scoped to CRM Bulk Job webhooks so no unrelated webhook is touched."""
	if doc.webhook_doctype == "CRM Bulk Job" and doc.request_url:
		from tatva_connect.utils import assert_safe_public_url
		assert_safe_public_url(doc.request_url)


def queue_pressure(user, mp):
	"""The bounded-queue refusal as (code, message, http), or None. Held here so EVERY submit path —
	the HTTP endpoint and the Desk import alike — pushes back on the same two caps."""
	cfg = _cfg()
	if mp and frappe.db.count("CRM Bulk Job", {"partner": user, "status": ["in", _NON_TERMINAL]}) \
			>= cfg["async_concurrent_jobs_per_partner"]:
		return ("rate_limited", _("Too many jobs in flight; retry when one finishes."), 429)
	if frappe.db.count("CRM Bulk Job", {"status": ["in", _NON_TERMINAL]}) >= cfg["async_global_queue_max"]:
		return ("server_busy", _("The bulk-job queue is full; retry shortly."), 503)
	return None


def submit_job(user, operation, fmt, payload, *, total=0, idempotency_key=None, extra=None):
	"""Insert the job, own its payload, enqueue the worker — the ONE submit path, HTTP and Desk alike.

	`extra` carries columns only one lane knows about (the Desk import names itself on the job), so a
	second creator never has to exist to add a field."""
	job = frappe.new_doc("CRM Bulk Job")
	job.update({"partner": user, "operation": operation, "input_format": fmt, "status": "UploadComplete",
	            "idempotency_key": idempotency_key, "submitted_at": now_datetime(), "total": total,
	            **(extra or {})})
	job.insert(ignore_permissions=True)  # authz-ok: tier-b — gated by the caller's own authz (_resolve_caller on the API lane, has_permission on the Desk lane)
	_attach_payload(job.name, payload, fmt)
	frappe.enqueue("tatva_connect.api.partner_bulk_worker.process_job", queue="partner_bulk",
	               bulk_job_id=job.name, timeout=_cfg()["async_job_timeout_seconds"],
	               enqueue_after_commit=True)
	return job.name


@frappe.whitelist(methods=["POST"])
@_api
def create(**_kwargs):
	"""Submit an async bulk-create job. Body: {operation, format?, records?}. Returns 202 {job_id, status}.
	Idempotent on the Idempotency-Key header (a resubmit replays the same job)."""
	if not automation.is_enabled(_ASYNC_BULK):
		return _fail("forbidden", _("Async bulk jobs are not enabled."), 403)
	user, mp, _is = _resolve_caller()
	data = frappe.form_dict
	operation, fmt = data.get("operation"), (data.get("format") or "inline")
	if operation not in _OPERATIONS:
		return _fail("validation_error", _("operation must be one of: {0}").format(", ".join(_OPERATIONS)), 400)
	if fmt not in _FORMATS:
		return _fail("validation_error", _("format must be one of: {0}").format(", ".join(_FORMATS)), 400)

	# Backpressure: a bounded queue refused cheaply; the caps themselves live in queue_pressure.
	cfg = _cfg()
	pressure = queue_pressure(user, mp)
	if pressure:
		code, message, http = pressure
		return _fail(code, message, http, retry_after=cfg["bulk_window_seconds"])

	# Inline is shape-checked here; a file's bytes are size-capped and stored as-is (the worker screens
	# and parses). CSV carries only flat lead-core; activity needs the nesting of JSONL.
	if fmt == "inline":
		records = frappe.parse_json(data.get("records")) if isinstance(data.get("records"), str) else data.get("records")
		if not isinstance(records, list) or not records:
			return _fail("validation_error", _("records must be a non-empty JSON array for an inline job."), 400)
		if len(records) > cfg["async_inline_max_records"]:
			return _fail("validation_error", _("Max {0} records per inline job; received {1}.").format(
				cfg["async_inline_max_records"], len(records)), 400)
		payload, total = "\n".join(json.dumps(r) for r in records), len(records)
	else:
		if fmt == "csv" and operation != "lead_create":
			return _fail("validation_error", _("CSV is accepted only for lead_create; use jsonl for {0}.").format(operation), 400)
		# Idempotency fingerprints form_dict only, so a multipart file is invisible; an idempotent file submit must use content_base64.
		if _idem_key() and not data.get("content_base64"):
			return _fail("validation_error", _("Use content_base64 (not a multipart upload) for an idempotent file submit."), 400)
		payload = _uploaded_bytes(cfg)
		if payload is None:
			return _fail("validation_error", _("A file upload or content_base64 is required for a {0} job.").format(fmt), 400)
		total = 0  # the worker counts records once it parses the file

	job_name = submit_job(user, operation, fmt, payload, total=total, idempotency_key=_idem_key())
	frappe.local.response["http_status_code"] = 202
	_ok(action=ACTION_CREATED, data={"job_id": job_name, "status": "UploadComplete"})


@frappe.whitelist(methods=["GET"])
@_api(read=True)
def get(**_kwargs):
	"""Status of one job, owner-scoped. Returns its state and counts."""
	user, mp, _is = _resolve_caller()
	job = _owned_job(_job_id(), user, mp)
	if not job:
		return _fail("not_found", _("Job not found."), 404)
	_ok(action=ACTION_FETCHED, data=job)


@frappe.whitelist(methods=["GET"])
@_api(read=True, bulk=True)
def results(**_kwargs):
	"""Per-record outcomes of one job, paginated. The same list envelope every read uses."""
	user, mp, _is = _resolve_caller()
	job = _owned_job(_job_id(), user, mp)
	if not job:
		return _fail("not_found", _("Job not found."), 404)
	limit, offset = _page(frappe.form_dict)
	total = frappe.db.count("CRM Bulk Job Result", {"job": job.name})
	rows = frappe.get_all(
		"CRM Bulk Job Result", filters={"job": job.name},
		fields=["record_index", "action", "record_name", "error_code", "error_message"],
		order_by="record_index asc", start=offset, page_length=limit,
	)
	_list_ok("results", rows, total, offset, limit)


@frappe.whitelist(methods=["POST"])
@_api
def cancel(**_kwargs):
	"""Clean cancel. A not-yet-started job is dropped to Aborted; a running job is flagged and the worker
	compensates (deletes only the records IT created) before ending Aborted."""
	user, mp, _is = _resolve_caller()
	job = _owned_job(_job_id(), user, mp)
	if not job:
		return _fail("not_found", _("Job not found."), 404)
	if job.status in _TERMINAL:
		return _fail("conflict", _("Job has already finished."), 409)
	from tatva_connect.api.partner_bulk_worker import finish_job
	if finish_job(job.name, "Aborted", commit=False, guard_pre_start=True):
		status = "Aborted"  # won the pre-start abort — the worker had not started, nothing was created
	else:
		# the worker already claimed it → cooperative cancel; it compensates its created rows between chunks
		frappe.db.set_value("CRM Bulk Job", job.name, "cancel_requested", 1, update_modified=False)
		status = frappe.db.get_value("CRM Bulk Job", job.name, "status")
	_ok(action=ACTION_UPDATED, data={"job_id": job.name, "status": status})


def purge_expired_jobs():
	"""Scheduler (gated, dormant): drop finished jobs, their per-record results and their payload file
	once past the retention window, so the tables and blob store do not grow forever."""
	if not automation.is_enabled(_ASYNC_PURGE):
		return
	from tatva_connect.api.partner_bulk_worker import _purge_payload
	cutoff = add_to_date(now_datetime(), days=-_cfg()["async_results_retention_days"])
	expired = frappe.get_all("CRM Bulk Job",
	                         filters={"status": ["in", _TERMINAL], "finished_at": ["<", cutoff]}, pluck="name")
	for name in expired:
		_purge_payload(frappe.get_doc("CRM Bulk Job", name))  # blob first, then the row cascades its results
		frappe.delete_doc("CRM Bulk Job", name, force=True, ignore_permissions=True)  # authz-ok: tier-b — scheduler housekeeping gated by _ASYNC_PURGE
	if expired:
		frappe.db.commit()
