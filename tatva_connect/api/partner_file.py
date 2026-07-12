"""Gated partner FILE API — attach / read / list / delete files on a lead.

Shares the ONE brain in `tatva_connect.api._base`: the SAME `_resolve_caller` enablement gate (the
single enabled `CRM Lead API Mapping` row + its grain governs files just like leads/activities/calls —
there is NO per-entity enablement), the SAME `resolve_lead` grain-scoped resolver, the SAME `_ok`/`_fail`
envelope, error codes, rate limit, `_run_bulk` / `_bulk_read` partial-success engines and `_list_ok`
list envelope.

Every file goes through `tatva_connect.storage.file_manager` (the file front door) — NEVER a hand-rolled
`frappe.get_doc({"File": ...})`. That facade sits on the File doc_events, so fail-closed privacy
(Invariant #15: files are ALWAYS private here) and the synchronous Azure offload run automatically on
insert. No parallel storage logic.

IDENTITY. A file is addressed by `name`, the File primary key, returned when it was attached. That is
the only address. `external_id` is the caller's own label (stored in `custom_external_id`): echoed back
on every read, never interpreted, never used to find a file, never a dedup key. A POST attaches a file;
re-POSTing the same bytes attaches a second file. Retries are made safe with the Idempotency-Key header.

There is NO file update: a file's bytes are immutable. Replacing a file means attaching the new one and
deleting the old. The document category is stored in `File.custom_file_type`.

  GET    file_schema       -> discovery: the attach payload contract (fields, types, required)
  GET    file_get          -> one file's metadata + proxy url, grain-scoped
  GET    file_list         -> a lead's files, filtered + paginated
  POST   file_attach       -> download/decode bytes, attach to a lead (or a scoped task/note)
  DELETE file_delete       -> delete a file by `name`, scope-checked
  POST   file_get_bulk     -> {"names":[...]} (<= 100), partial success
  POST   file_attach_bulk  -> {"files":[...]} (<= 100), partial success
  DELETE file_delete_bulk  -> {"names":[...]} (<= 100), partial success
"""
import base64
import binascii

import frappe
from frappe import _
from frappe.query_builder import Order
from frappe.query_builder.functions import Count

from tatva_connect.api._base import (
	ACTION_CREATED,
	ACTION_DELETED,
	ACTION_FETCHED,
	EXTERNAL_ID_FIELD,
	_api,
	_bulk_read,
	_cfg,
	_list_ok,
	_ok,
	_page,
	_read_list,
	_read_required_list,
	_resolve_caller,
	_run_bulk,
	_schema_ok,
	field_descriptor,
	resolve_lead,
	validate_external_id,
)
from tatva_connect.storage import file_manager, file_screening

# All numeric caps (list page sizes, the download timeout) come from the CRM Partner API
# Settings Single via _cfg() — one source of truth, no module-local copy.

# The homes a file may hang from besides the lead — the one table the write (_resolve_target), read
# (_file_lead) and enumeration (_lead_files) sides all walk, so they cannot desync and mint an address
# no read can honour. Each doctype carries reference_doctype/docname back to its lead.
#   request key -> the doctype it homes the file on
_TARGETS = (("activity", "CRM Task"), ("note", "FCRM Note"))
_TARGET_DOCTYPES = tuple(doctype for _key, doctype in _TARGETS)

# The attach payload contract — the ONE source of truth for what a caller may send and what
# `file_schema` advertises. Discovery equals ingestion because both read THIS.
#   fieldname -> (label, fieldtype, required)
FILE_FIELDS = (
	("lead",           "Lead",           "Data", False),
	("mobile_no",      "Mobile No",      "Data", False),
	("activity",       "Activity",       "Data", False),
	("note",           "Note",           "Data", False),
	("external_id",    "External ID",    "Data", False),
	("file_type",      "File Type",      "Data", False),
	("filename",       "Filename",       "Data", True),
	("file_url",       "File URL",       "Data", False),
	("content_base64", "Content Base64", "Data", False),
)


# -- helpers -----------------------------------------------------------------

def _file_view(doc):
	"""The partner-facing shape of a File (proxy URL, never a raw blob/local path)."""
	return {
		"name": doc.name,
		"file_type": doc.get("custom_file_type"),
		"external_id": doc.get(EXTERNAL_ID_FIELD),
		"filename": doc.file_name,
		"file_url": file_manager.proxy_url(doc),
		"is_private": bool(doc.is_private),
		"attached_to_doctype": doc.attached_to_doctype,
		"attached_to_name": doc.attached_to_name,
	}


def _resolve_target(data, lead_name):
	"""(attached_to_doctype, attached_to_name) for an attach — the WRITE side of _TARGETS. Defaults to
	the lead; a request key from _TARGETS homes the file on that record instead, but ONLY after
	scope-checking it belongs to THIS lead (else the same generic not-found, no probing)."""
	for key, doctype in _TARGETS:
		name = data.get(key)
		if not name:
			continue
		ref = frappe.db.get_value(
			doctype, name, ["reference_doctype", "reference_docname"], as_dict=True
		)
		if not ref or ref.reference_doctype != "CRM Lead" or ref.reference_docname != lead_name:
			frappe.throw(_("{0} not found").format(doctype), frappe.DoesNotExistError)
		return doctype, name
	return "CRM Lead", lead_name


def _load_bytes(data):
	"""The file's bytes from the payload: download a (signed/expiring) `file_url`, or decode
	`content_base64`. Exactly one source is required."""
	file_url = data.get("file_url")
	content_b64 = data.get("content_base64")
	if file_url:
		import requests  # ALLOWLIST 2026-06-29: streaming download (stream=True) with a byte-cap + raise_for_status — make_get_request returns processed JSON, can't stream/cap. Do NOT convert.

		from tatva_connect.utils import assert_safe_public_url

		assert_safe_public_url(file_url)  # SSRF: block internal/metadata targets before fetching
		cfg = _cfg()
		max_bytes = cfg["file_download_max_mb"] * 1024 * 1024
		# The URL is the caller's input, so one that will not fetch is a 400, never a 500. Every
		# requests failure is a RequestException; unmapped, _classify would call it a server_error.
		chunks, total = [], 0
		try:
			# timeout IS set (config-sourced); bandit is low-confidence only because it can't resolve the value statically.
			resp = requests.get(
				file_url, timeout=cfg["file_download_timeout_seconds"], stream=True,
				allow_redirects=False,  # SSRF: assert_safe_public_url vetted THIS host only; a 3xx could bounce to an internal target
			)  # nosec B113
			if 300 <= resp.status_code < 400:
				frappe.throw(_("file_url must resolve directly, without redirects"))
			resp.raise_for_status()
			# The stream is inside the guard: a connection that dies mid-download raises here, not at
			# the get(). Our own throws are ValidationError, so they pass through untouched.
			for chunk in resp.iter_content(64 * 1024):
				total += len(chunk)
				if total > max_bytes:
					frappe.throw(_("File exceeds the {0} MB limit").format(cfg["file_download_max_mb"]))
				chunks.append(chunk)
		except requests.exceptions.RequestException as e:
			frappe.throw(_("file_url could not be fetched: {0}").format(type(e).__name__))
		return b"".join(chunks)
	if content_b64:
		try:
			return base64.b64decode(content_b64, validate=True)
		except (binascii.Error, ValueError):
			frappe.throw(_("content_base64 is not valid base64"))
	frappe.throw(_("file_url or content_base64 is required"))


def _scoped_file(name, mp, is_sysmgr):
	"""Load a File by name, grain-scoped: the lead it (or its task) hangs off MUST be on the
	caller's vertical+group. Missing AND out-of-scope return the SAME generic not-found."""
	if not name:
		frappe.throw(_("name (the File id) is required"))
	doc = frappe.db.exists("File", name) and frappe.get_doc("File", name)
	if not doc:
		frappe.throw(_("File not found"), frappe.DoesNotExistError)

	lead_name = _file_lead(doc)
	if not lead_name:
		frappe.throw(_("File not found"), frappe.DoesNotExistError)
	if mp:
		# resolve_lead re-applies the partner's forced vertical+group filter -> a file whose
		# lead is on another line resolves to not-found, never leaking it.
		try:
			resolve_lead(mp, is_sysmgr, {"lead": lead_name})
		except frappe.DoesNotExistError:
			frappe.throw(_("File not found"), frappe.DoesNotExistError)
	return doc


def _file_lead(doc):
	"""The CRM Lead a File hangs from — the read side of _TARGETS. Directly, or through any home the
	write side accepts. None if neither."""
	if doc.attached_to_doctype == "CRM Lead":
		return doc.attached_to_name
	if doc.attached_to_doctype in _TARGET_DOCTYPES and doc.attached_to_name:
		ref = frappe.db.get_value(
			doc.attached_to_doctype, doc.attached_to_name,
			["reference_doctype", "reference_docname"], as_dict=True,
		)
		if ref and ref.reference_doctype == "CRM Lead":
			return ref.reference_docname
	return None


def _lead_files(lead_name):
	"""The one predicate for "a file belonging to this lead" — attached to the lead itself, or to any
	record homed on it through _TARGETS. Returns (File table, where-condition) for frappe.qb."""
	f = frappe.qb.DocType("File")
	homes = [("CRM Lead", [lead_name])]
	for _key, doctype in _TARGETS:
		names = frappe.get_all(
			doctype,
			filters={"reference_doctype": "CRM Lead", "reference_docname": lead_name},
			pluck="name",
		)
		if names:
			homes.append((doctype, names))

	cond = None
	for doctype, names in homes:
		this = (f.attached_to_doctype == doctype) & (f.attached_to_name.isin(names))
		cond = this if cond is None else (cond | this)
	return f, cond


# -- per-record core (shared by singular + bulk) -----------------------------

def _create_one(data, mp, is_sysmgr):
	"""Attach ONE file to a lead (or a scoped task/note). Returns (file_view, "created").

	An attach ATTACHES: there is no dedup on a caller-supplied key. `external_id`, if sent, is stamped
	as a label and nothing more. A caller that re-POSTs the same bytes gets a second File — that is
	correct, and the Idempotency-Key header is how a retry is made safe."""
	lead_name = resolve_lead(mp, is_sysmgr, data)

	filename = data.get("filename")
	if not filename:
		frappe.throw(_("filename is required"))
	validate_external_id("File", data.get("external_id"))

	target_doctype, target_name = _resolve_target(data, lead_name)
	content = _load_bytes(data)
	# Screen the bytes BEFORE the File is saved (shared brain; dormant unless the operator has
	# activated the "Partner API" channel in CRM File Screening Settings). A block throws a
	# ValidationError, which @_api returns as a structured _fail — no File is created.
	file_screening.screen(
		file_name=filename, raw=content, channel="Partner API", source=frappe.session.user,
		attached_to_doctype=target_doctype, attached_to_name=target_name,
		source_ip=getattr(frappe.local, "request_ip", None),
	)
	# ALWAYS private (Invariant #15) — file_manager.save defaults private=True; the File
	# doc_events enforce the private floor regardless. The category + the caller's label go
	# via meta so both land on the row in one insert.
	doc = file_manager.save(
		content,
		filename=filename,
		attached_to_doctype=target_doctype,
		attached_to_name=target_name,
		private=True,
		meta={"custom_file_type": data.get("file_type"),
		      EXTERNAL_ID_FIELD: data.get("external_id"),
		      "custom_source": "Partner API"},
	)
	return _file_view(doc), ACTION_CREATED


def _delete_one(name, mp, is_sysmgr):
	"""Delete one file by `name`, scope-checked. The on_trash hook drops the Azure blob
	(last reference)."""
	doc = _scoped_file(name, mp, is_sysmgr)
	frappe.delete_doc("File", doc.name, ignore_permissions=True)  # authz-ok: tier-b — gated by _resolve_caller + resolve_lead, before the save


def _read_one(name, mp, is_sysmgr):
	"""Load one file by `name` -> the partner view. The per-record loader both file_get and
	file_get_bulk call, so a single read and a bulk read can never diverge."""
	return _file_view(_scoped_file(name, mp, is_sysmgr))


# -- discovery ---------------------------------------------------------------

@frappe.whitelist(methods=["GET"])
@_api(read=True)
def file_schema(**_kwargs):
	"""Discovery: the attach payload contract — every field a caller may send, its type, and whether
	it is required. The shape is fixed (it does not vary by partner or grain), but it is discoverable,
	so an integrator never hardcodes a field list."""
	_resolve_caller()
	cfg = _cfg()
	_schema_ok(
		"file",
		dedup=(
			"None. Every POST attaches a new file and returns a new `name`. Retries are made safe with "
			"the Idempotency-Key header; `external_id` does not deduplicate."
		),
		fields=[field_descriptor(fn, label, ftype, required)
		        for fn, label, ftype, required in FILE_FIELDS],
		bytes=(
			"The bytes are supplied either as a downloadable `file_url` (fetched by the server) or as "
			"`content_base64`. Exactly one is sent. The maximum download size is {0} MB.".format(
				cfg["file_download_max_mb"])
		),
		screening=(
			"Every file is scanned for malware before it is stored. A file that fails the scan is "
			"rejected outright and nothing is written: the response is a 400 with `error.code` "
			"`validation_error` and the message \"This file failed a security scan and was not "
			"accepted.\" The scan runs on the bytes themselves, so it applies equally to a "
			"`content_base64` upload and to a `file_url` the server fetches. A rejected file is not "
			"quarantined and cannot be retrieved; a clean copy is sent instead."
		),
		privacy=(
			"Every file attached through this API is private. `is_private` always reads true and "
			"`file_url` is always a proxy url, never a raw storage key or a local path."
		),
		target=(
			"A file is attached to its lead by default. Passing `activity` (a CRM Task name) homes it on "
			"that activity; passing `note` (an FCRM Note name) homes it on that note. Either must belong "
			"to the same lead."
		),
		immutable="A file's bytes cannot be changed. Replacing a file means attaching the new one and deleting the old.",
	)


# -- singular endpoints ------------------------------------------------------

@frappe.whitelist(methods=["GET"])
@_api(read=True)
def file_get(**_kwargs):
	"""Read one file by `name`, grain-scoped. Returns metadata + the proxy url."""
	_user, mp, is_sysmgr = _resolve_caller()
	_ok(action=ACTION_FETCHED, data=_read_one(frappe.form_dict.get("name"), mp, is_sysmgr))


@frappe.whitelist(methods=["POST"])
@_api
def file_attach(**_kwargs):
	"""Attach a file to a lead (or a scoped activity/note). Body:
	{lead|mobile_no, activity?, note?, external_id?, file_type?, file_url|content_base64, filename}.
	Returns the `name` to address the file by from now on."""
	_user, mp, is_sysmgr = _resolve_caller()
	view, action = _create_one(frappe.form_dict, mp, is_sysmgr)
	_ok(action=action, data=view)


@frappe.whitelist(methods=["DELETE"])
@_api
def file_delete(**_kwargs):
	"""Delete one file by `name`, scope-checked (own line only). Out-of-scope/missing ->
	the SAME generic not-found."""
	_user, mp, is_sysmgr = _resolve_caller()
	name = frappe.form_dict.get("name")
	_delete_one(name, mp, is_sysmgr)
	_ok(action=ACTION_DELETED, data={"name": name})


# -- bulk / query endpoints --------------------------------------------------

@frappe.whitelist(methods=["POST"])
@_api(bulk=True, read=True)
def file_get_bulk(**_kwargs):
	"""Read many files by `names` (<= 100). Input-ordered; out-of-scope/unknown names are
	reported not_found in place."""
	_user, mp, is_sysmgr = _resolve_caller()
	names = _read_required_list(frappe.form_dict, "names")
	return _bulk_read(names, lambda name: _read_one(name, mp, is_sysmgr))


@frappe.whitelist(methods=["POST"])
@_api(bulk=True)
def file_attach_bulk(**_kwargs):
	"""Attach many files. Body: {"files":[{...}, ...]}. Partial success.

	The ceiling is the FILE ceiling, not the row one: a file is bytes to decode, scan and write, not a
	row. file_schema publishes the number this enforces."""
	_user, mp, is_sysmgr = _resolve_caller()
	files = _read_required_list(frappe.form_dict, "files")

	def one(i, item):
		view, action = _create_one(item, mp, is_sysmgr)
		return {"index": i, "status": "success", "action": action, "data": view}

	return _run_bulk(files, one, entity="file")


@frappe.whitelist(methods=["DELETE"])
@_api(bulk=True)
def file_delete_bulk(**_kwargs):
	"""Delete many files. Body: {"names":[...]}. Partial success. A delete moves no bytes, so it
	shares the general row ceiling."""
	_user, mp, is_sysmgr = _resolve_caller()
	names = _read_required_list(frappe.form_dict, "names")

	def one(i, name):
		_delete_one(name, mp, is_sysmgr)
		return {"index": i, "status": "success", "action": ACTION_DELETED, "data": {"name": name}}

	return _run_bulk(names, one)


@frappe.whitelist(methods=["GET"])
@_api(bulk=True, read=True)
def file_list(**_kwargs):
	"""List a lead's files (optional `file_type`), paginated. Query: lead|mobile_no,
	file_type?, limit (<=200, default 20), offset. Lead is grain-scoped via resolve_lead.

	"A lead's files" means every home in _TARGETS, not just the lead itself — so a file attached to an
	activity or a note appears here, exactly as file_get already resolves it."""
	_user, mp, is_sysmgr = _resolve_caller()
	data = frappe.form_dict
	lead_name = resolve_lead(mp, is_sysmgr, data)

	f, cond = _lead_files(lead_name)
	if data.get("file_type"):
		cond = cond & (f.custom_file_type == data.get("file_type"))

	limit, offset = _page(data)
	total = frappe.qb.from_(f).select(Count("*")).where(cond).run()[0][0]
	rows = (
		frappe.qb.from_(f)
		.select(f.name, f.custom_file_type, getattr(f, EXTERNAL_ID_FIELD), f.file_name,
		        f.file_url, f.is_private, f.attached_to_doctype, f.attached_to_name)
		.where(cond)
		.orderby(f.creation, order=Order.desc)
		.limit(limit).offset(offset)
		.run(as_dict=True)
	)
	# Pass a doc-like so proxy_url takes the .file_url branch (extracts the blob key);
	# a bare string is treated as an already-built key and double-wraps the proxy URL.
	_list_ok("files", [_file_view(frappe._dict(r)) for r in rows], total, offset, limit)
