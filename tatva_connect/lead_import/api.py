# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The Desk import's server surface. The server decides scope; the client renders what it is handed.

Nothing here is a new path. Sections and fields come from `lead/mapping.py` — the same seam that feeds
the Intake builder and the Facebook question mapper. The file is read by `tabular` — the same reader the
partner CSV lane uses. A job is created by `partner_bulk_job.submit_job` — the same submit path the HTTP
endpoint uses, so a Desk import inherits the queue caps instead of side-stepping them.
"""
import frappe
from frappe import _

from tatva_connect import tabular
from tatva_connect.access import entitlement
from tatva_connect.api import partner_bulk_job
from tatva_connect.automation import settings as automation
from tatva_connect.lead import mapping

_TOGGLE = "Lead::BulkImport::desk"  # dormant: the Desk import writes nothing until an operator turns it on
_IMPORT = "CRM Lead Import"
_CAP = 100  # a Link dropdown never needs more, and entitlement is checked per row below


def _doc(lead_import, ptype="read"):
	frappe.has_permission(_IMPORT, ptype, doc=lead_import, throw=True)
	return frappe.get_doc(_IMPORT, lead_import)


def _contract(imp):
	return frappe.get_cached_doc("CRM Lead API Mapping", imp.contract)


@frappe.whitelist()
def list_sections(lead_import):
	"""The sections a column may target, in the seed's own display order."""
	_doc(lead_import)
	return [section["section_key"] for section in mapping.mappable_sections()]


@frappe.whitelist()
def list_fields(lead_import, section):
	"""The fields within one section this grain may write — the seam intake and facebook also ask."""
	imp = _doc(lead_import)
	return [{"value": field["fieldname"], "label": field["label"]}
	        for field in mapping.mappable_fields(section=section, contract=_contract(imp))]


@frappe.whitelist()
def describe_grain(lead_import):
	"""The grain the contract carries, for the form's read-only headline."""
	imp = _doc(lead_import)
	contract = _contract(imp)
	return {"vertical": contract.vertical, "group": contract.crm_group,
	        "program": imp.program or contract.program, "source": contract.source,
	        "writable_count": len(mapping.mappable_fields(contract=contract))}


@frappe.whitelist()
def read_columns(lead_import):
	"""Fill the column grid from the file's header, pre-resolving any header that already names a field."""
	imp = _doc(lead_import, "write")
	raw, fmt = _payload(imp)
	rows = tabular.read(raw, fmt)
	known = {field["field_key"]: field for field in mapping.mappable_fields(contract=_contract(imp))}
	imp.columns = []
	for header in (rows[0].keys() if rows else []):
		resolved = known.get(header)
		imp.append("columns", {"source_column": header,
		                       "target_table": resolved["section"] if resolved else None,
		                       "target_field": resolved["fieldname"] if resolved else None})
	imp.row_count = len([row for row in rows if "__error__" not in row])
	imp.save()
	return {"row_count": imp.row_count, "columns": len(imp.columns)}


def _payload(imp):
	"""The upload's bytes and format — M2: asked of the File row, never read off a path."""
	if not imp.import_file:
		frappe.throw(_("No file is attached."), title=_("File required"))
	name = frappe.db.get_value("File", {"file_url": imp.import_file}, "name")
	if not name:
		frappe.throw(_("The attached file is missing."), title=_("File required"))
	doc = frappe.get_doc("File", name)
	content = doc.get_content()
	raw = content.encode("utf-8") if isinstance(content, str) else content
	suffix = (doc.file_name or "").rsplit(".", 1)[-1].lower()
	return raw, ("xlsx" if suffix in ("xlsx", "xls") else suffix)


@frappe.whitelist()
def start_validation(lead_import):
	"""Queue the mandatory dry run — every row written through the live path, then rolled back."""
	imp = _doc(lead_import, "write")
	if not imp.field_key_map():
		frappe.throw(_("At least one column must be mapped before the file is validated."),
		             title=_("Mapping required"))
	return _queue(imp, dry_run=1, status="Validating")


@frappe.whitelist()
def start_import(lead_import):
	"""Queue the live run. Refused unless a validation ran against exactly the bytes now attached."""
	if not automation.is_enabled(_TOGGLE):
		frappe.throw(_("The Desk bulk import is turned off."), title=_("Feature off"))
	imp = _doc(lead_import, "write")
	imp.assert_importable()
	return _queue(imp, dry_run=0, status="Importing")


def _queue(imp, dry_run, status):
	"""Submit through the ONE submit path, so a Desk import is capped exactly as a partner job is."""
	user = frappe.session.user
	pressure = partner_bulk_job.queue_pressure(user, True)
	if pressure:
		frappe.throw(pressure[1], title=_("Queue busy"))
	raw, fmt = _payload(imp)
	job = partner_bulk_job.submit_job(user, "lead_import", fmt, raw,
	                                  extra={"source_import": imp.name, "dry_run": dry_run})
	stamped = {"status": status, "dry_run_job" if dry_run else "import_job": job}
	frappe.db.set_value(_IMPORT, imp.name, stamped, update_modified=False)
	return job


@frappe.whitelist()
def download_template(lead_import, fmt="xlsx"):
	"""A blank file whose columns are exactly the field_keys this grain may write."""
	imp = _doc(lead_import)
	keys = [field["field_key"] for field in mapping.mappable_fields(contract=_contract(imp))]
	if not keys:
		frappe.throw(_("This contract permits no writable fields."), title=_("Nothing to import"))
	tabular.respond(keys, [], fmt, f"lead-import-{imp.name}")


@frappe.whitelist()
def contract_query(doctype, txt, searchfield, start, page_len, filters):
	"""Link query: only contracts whose grain this operator holds. The server decides scope, never the client.

	Returns [(name, display_label), ...] — the shape every Link query in this app returns."""
	conds = {"enabled": 1}
	if (txt or "").strip():
		conds["name"] = ["like", f"%{txt.strip()}%"]
	rows = frappe.get_all(  # authz-ok: every row is clamped by entitlement.grain_entitled below before it is returned
		"CRM Lead API Mapping", filters=conds, fields=["name", "vertical", "crm_group", "program"],
		order_by="name asc", limit_page_length=_CAP)
	out = []
	for row in rows:
		grain = {"vertical": row.vertical, "group": row.crm_group, "program": row.program}
		if entitlement.grain_entitled(grain, frappe.session.user):
			out.append((row.name, f"{row.vertical or '—'} · {row.crm_group or '—'} · {row.program or '—'}"))
	return out[int(start):int(start) + int(page_len)]


def follow_job_status(doc, method=None):
	"""Mirror a lead_import job's terminal state onto the import it came from (doc_event on CRM Bulk Job)."""
	if doc.operation != "lead_import" or not doc.get("source_import"):
		return
	if doc.status not in ("JobComplete", "Failed", "Aborted"):
		return
	imp = frappe.get_doc(_IMPORT, doc.source_import)
	frappe.db.set_value(_IMPORT, imp.name, _terminal_state(doc, imp), update_modified=False)


def _terminal_state(job, imp):
	"""What a finished job means for its import: a dry run validates, a live run imports."""
	state = {"valid_rows": job.succeeded, "invalid_rows": job.failed}
	if job.get("dry_run"):
		passed = job.status == "JobComplete" and job.succeeded and not job.failed
		state["status"] = "Validated" if passed else "Validation Failed"
		state["validated_against"] = imp.file_hash if passed else None
		return state
	if job.status == "Aborted":
		state["status"] = "Cancelled"
	elif job.status == "Failed":
		state["status"] = "Import Failed"
	else:
		state["status"] = "Partially Imported" if job.failed else "Imported"
	return state
