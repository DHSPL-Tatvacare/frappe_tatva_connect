# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The Desk lead import's endpoints: fields from `lead/mapping`, files via `tabular`, jobs via `submit_job`."""
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


@frappe.whitelist()
def list_sections(lead_import):
	"""The sections a column may target, in the seed's own display order."""
	_doc(lead_import)
	return [section["section_key"] for section in mapping.mappable_sections()]


@frappe.whitelist()
def list_fields(lead_import, section):
	"""The fields in one section this contract may write."""
	imp = _doc(lead_import)
	return [{"value": field["fieldname"], "label": field["label"]}
	        for field in mapping.mappable_fields(section=section, contract=imp.contract_doc())]


# read_columns and describe_grain removed (header read on attach; the contract field shows the grain); archived in .archive/.


@frappe.whitelist()
def start_validation(lead_import):
	"""Queue the dry run: every row written through the live path, then rolled back."""
	imp = _doc(lead_import, "write")
	if not imp.field_key_map():
		frappe.throw(_("Map at least one column before validating."), title=_("Mapping required"))
	return _queue(imp, dry_run=1, status="Validating")


@frappe.whitelist()
def start_import(lead_import):
	"""Queue the live run of a validated file."""
	imp = _doc(lead_import, "write")
	imp.assert_importable()
	return _queue(imp, dry_run=0, status="Importing")


def _queue(imp, dry_run, status):
	"""Submit through the one job path, gated and capped like a partner job; a dry run is gated too."""
	if not automation.is_enabled(_TOGGLE):
		frappe.throw(_("The Desk bulk import is turned off."), title=_("Feature off"))
	user = frappe.session.user
	pressure = partner_bulk_job.queue_pressure(user, True)
	if pressure:
		frappe.throw(pressure[1], title=_("Queue busy"))
	raw, fmt = imp.payload()
	lane = automation.QUIET if dry_run else imp.bulk_lane  # a rehearsal never triggers anything
	job = partner_bulk_job.submit_job(user, "lead_import", fmt, raw,
	                                  extra={"source_import": imp.name, "dry_run": dry_run, "bulk_lane": lane})
	stamped = {"status": status, "dry_run_job" if dry_run else "import_job": job}
	frappe.db.set_value(_IMPORT, imp.name, stamped)  # bumps modified, so a form opened before this cannot save over it
	return job


@frappe.whitelist()
def download_template(lead_import, fmt="xlsx"):
	"""A blank file whose header is every field key this contract may write."""
	imp = _doc(lead_import)
	keys = [field["field_key"] for field in mapping.mappable_fields(contract=imp.contract_doc())]
	if not keys:
		frappe.throw(_("This contract permits no writable fields."), title=_("Nothing to import"))
	tabular.respond(keys, [], fmt, f"lead-import-{imp.name}")


@frappe.whitelist()
def contract_query(doctype, txt, searchfield, start, page_len, filters):
	"""Link query: only enabled contracts whose grain this operator holds, as (name, label)."""
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
	"""CRM Bulk Job on_update: copy a finished lead_import job's outcome onto its import."""
	if doc.operation != "lead_import" or not doc.get("source_import"):
		return
	if doc.status not in ("JobComplete", "Failed", "Aborted"):
		return
	imp = frappe.get_doc(_IMPORT, doc.source_import)
	frappe.db.set_value(_IMPORT, imp.name, _terminal_state(doc, imp))
	frappe.publish_realtime("lead_import_refresh", {"lead_import": imp.name}, user=doc.partner, after_commit=True)


def _terminal_state(job, imp):
	"""A dry run validates when any row passed (the refused are skipped on import); a live run imports."""
	state = {"valid_rows": job.succeeded, "invalid_rows": job.failed}
	if job.get("dry_run"):
		passed = job.status == "JobComplete" and job.succeeded
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
