"""Gated partner FILE API — attach / read / list / delete files on a lead.

Shares the ONE brain in `tatva_connect.api._base`: the SAME `_resolve_caller`
enablement gate (the single enabled `CRM Lead API Mapping` row + its grain governs
files just like leads/activities — there is NO per-entity enablement), the SAME
`resolve_lead` grain-scoped resolver, the SAME `_ok`/`_fail` envelope, error codes,
rate limit (100 req/60s) and `_run_bulk` partial-success engine.

Every file goes through `tatva_connect.storage.file_manager` (the file front door) —
NEVER a hand-rolled `frappe.get_doc({"File": ...})`. That facade sits on the File
doc_events, so fail-closed privacy (Invariant #15: files are ALWAYS private here) and
the synchronous Azure offload run automatically on insert. No parallel storage logic.

`external_id` is the idempotency key: the LSQ attachment id, stored in
`File.custom_lsq_attachment_id`. Re-sending the same external_id for the same lead
returns the existing file (action="exists"), never a duplicate. The document category
is stored in `File.custom_file_type`.

  POST   file_attach       -> download/decode bytes, attach to lead (or a scoped task)
  GET    file_get          -> one file's metadata + proxy url, grain-scoped
  GET    file_list         -> a lead's files, filtered + paginated
  DELETE file_delete       -> delete a file by name, scope-checked
  POST   file_attach_bulk  -> {"files":[...]} (<= 100), partial success
"""
import base64
import binascii

import frappe
from frappe import _
from frappe.utils import cint

from tatva_connect.api._base import (  # noqa: F401
	_api,
	_cfg,
	_fail,
	_norm_phone,
	_ok,
	_read_list,
	_resolve_caller,
	_run_bulk,
	resolve_lead,
)
from tatva_connect.storage import file_manager

# All numeric caps (list page sizes, the download timeout) come from the CRM Partner API
# Settings Single via _cfg() — one source of truth, no module-local copy.


# -- helpers -----------------------------------------------------------------

def _file_view(doc):
	"""The partner-facing shape of a File (proxy URL, never a raw blob/local path)."""
	return {
		"name": doc.name,
		"file_type": doc.get("custom_file_type"),
		"external_id": doc.get("custom_lsq_attachment_id"),
		"filename": doc.file_name,
		"file_url": file_manager.proxy_url(doc),
		"is_private": bool(doc.is_private),
		"attached_to_doctype": doc.attached_to_doctype,
		"attached_to_name": doc.attached_to_name,
	}


def _resolve_target(data, lead_name):
	"""(attached_to_doctype, attached_to_name) for an attach. Defaults to the lead; if an
	`activity` (CRM Task name) is given, attach to that task — but ONLY after scope-checking
	the task actually belongs to THIS lead (else the same generic not-found, no probing)."""
	activity = data.get("activity")
	if not activity:
		return "CRM Lead", lead_name
	ref = frappe.db.get_value(
		"CRM Task", activity, ["reference_doctype", "reference_docname"], as_dict=True
	)
	if not ref or ref.reference_doctype != "CRM Lead" or ref.reference_docname != lead_name:
		frappe.throw(_("Activity not found"), frappe.DoesNotExistError)
	return "CRM Task", activity


def _load_bytes(data):
	"""The file's bytes from the payload: download a (signed/expiring) `file_url`, or decode
	`content_base64`. Exactly one source is required."""
	file_url = data.get("file_url")
	content_b64 = data.get("content_base64")
	if file_url:
		import requests

		resp = requests.get(file_url, timeout=_cfg()["file_download_timeout_seconds"])
		resp.raise_for_status()
		return resp.content
	if content_b64:
		try:
			return base64.b64decode(content_b64, validate=True)
		except (binascii.Error, ValueError):
			frappe.throw(_("content_base64 is not valid base64"))
	frappe.throw(_("file_url or content_base64 is required"))


def _attach_one(data, mp, is_sysmgr):
	"""Attach one file to a lead (or a scoped task). Idempotent on external_id +
	attached_to_name. Returns (file_view, action)."""
	lead_name = resolve_lead(mp, is_sysmgr, data)

	external_id = data.get("external_id")
	if not external_id:
		frappe.throw(_("external_id is required"))
	file_type = data.get("file_type")
	filename = data.get("filename")
	if not filename:
		frappe.throw(_("filename is required"))

	target_doctype, target_name = _resolve_target(data, lead_name)

	# DEDUP: same external_id already attached to this same record -> return it untouched.
	existing = file_manager.find(
		custom_lsq_attachment_id=external_id, attached_to_name=target_name
	)
	if existing:
		return _file_view(existing), "exists"

	content = _load_bytes(data)
	# ALWAYS private (Invariant #15) — file_manager.save defaults private=True; the File
	# doc_events enforce the private floor regardless. custom_file_type / custom_lsq_attachment_id
	# go via meta so the dedup key + category land on the row in one insert.
	doc = file_manager.save(
		content,
		filename=filename,
		attached_to_doctype=target_doctype,
		attached_to_name=target_name,
		private=True,
		meta={"custom_file_type": file_type, "custom_lsq_attachment_id": external_id},
	)
	return _file_view(doc), "attached"


def _scoped_file(name, mp, is_sysmgr):
	"""Load a File by name, grain-scoped: the lead it (or its task) hangs off MUST be on the
	caller's vertical+group. Missing AND out-of-scope return the SAME generic not-found."""
	if not name:
		frappe.throw(_("name is required"))
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
	"""The CRM Lead name a File hangs off — directly, or via its CRM Task. None if neither."""
	if doc.attached_to_doctype == "CRM Lead":
		return doc.attached_to_name
	if doc.attached_to_doctype == "CRM Task" and doc.attached_to_name:
		ref = frappe.db.get_value(
			"CRM Task", doc.attached_to_name, ["reference_doctype", "reference_docname"], as_dict=True
		)
		if ref and ref.reference_doctype == "CRM Lead":
			return ref.reference_docname
	return None


# -- endpoints ---------------------------------------------------------------

@frappe.whitelist(methods=["POST"])
@_api
def file_attach(**kwargs):
	"""Attach a file to a lead (or a scoped activity task). Body:
	{lead|mobile_no, activity?, external_id, file_type, file_url|content_base64, filename}.
	Idempotent on external_id: re-sending returns the existing file (action="exists")."""
	user, mp, is_sysmgr = _resolve_caller()
	view, action = _attach_one(frappe.form_dict, mp, is_sysmgr)
	_ok(action=action, data=view)


@frappe.whitelist(methods=["GET"])
@_api
def file_get(**kwargs):
	"""Read one file by `name`, grain-scoped. Returns metadata + the proxy url."""
	user, mp, is_sysmgr = _resolve_caller()
	doc = _scoped_file(frappe.form_dict.get("name"), mp, is_sysmgr)
	_ok(action="fetched", data=_file_view(doc))


@frappe.whitelist(methods=["GET"])
@_api
def file_list(**kwargs):
	"""List a lead's files (optional `file_type`), paginated. Query: lead|mobile_no,
	file_type?, limit (<=200, default 20), offset. Lead is grain-scoped via resolve_lead."""
	user, mp, is_sysmgr = _resolve_caller()
	data = frappe.form_dict
	lead_name = resolve_lead(mp, is_sysmgr, data)

	filters = {"attached_to_doctype": "CRM Lead", "attached_to_name": lead_name}
	if data.get("file_type"):
		filters["custom_file_type"] = data.get("file_type")

	cfg = _cfg()
	limit = min(cint(data.get("limit")) or cfg["list_default_page"], cfg["list_max_page"])
	offset = cint(data.get("offset") or data.get("limit_start"))

	total = frappe.db.count("File", filters)
	rows = frappe.get_all(
		"File", filters=filters,
		fields=["name", "custom_file_type", "custom_lsq_attachment_id", "file_name",
		        "file_url", "is_private", "attached_to_doctype", "attached_to_name"],
		limit_page_length=limit, limit_start=offset, order_by="creation desc",
	)
	files = [{
		"name": r.name,
		"file_type": r.custom_file_type,
		"external_id": r.custom_lsq_attachment_id,
		"filename": r.file_name,
		# Pass a doc-like so proxy_url takes the .file_url branch (extracts the blob key);
		# a bare string is treated as an already-built key and double-wraps the proxy URL.
		"file_url": file_manager.proxy_url(frappe._dict(file_url=r.file_url)),
		"is_private": bool(r.is_private),
		"attached_to_doctype": r.attached_to_doctype,
		"attached_to_name": r.attached_to_name,
	} for r in rows]
	_ok(action="fetched", data={
		"total": total, "count": len(files), "offset": offset, "limit": limit,
		"has_more": offset + len(files) < total, "files": files,
	})


@frappe.whitelist(methods=["DELETE"])
@_api
def file_delete(**kwargs):
	"""Delete one file by `name`, scope-checked (own line only). Out-of-scope/missing ->
	the SAME generic not-found. The on_trash hook drops the Azure blob (last reference)."""
	user, mp, is_sysmgr = _resolve_caller()
	name = frappe.form_dict.get("name")
	doc = _scoped_file(name, mp, is_sysmgr)
	frappe.delete_doc("File", doc.name, ignore_permissions=True)
	_ok(action="deleted", data={"name": name})


@frappe.whitelist(methods=["POST"])
@_api
def file_attach_bulk(**kwargs):
	"""Attach many files. Body: {"files":[{...}, ...]} (<= 100). Partial success — each file
	is idempotent on its own external_id."""
	user, mp, is_sysmgr = _resolve_caller()
	files = _read_list(frappe.form_dict, "files") or []

	def one(i, item):
		view, action = _attach_one(item, mp, is_sysmgr)
		return {"index": i, "status": "success", "action": action, "data": view}

	return _run_bulk(files, one)
