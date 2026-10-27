# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The Desk lead import's endpoints: fields from `lead/mapping`, files via `tabular`, jobs via `submit_job`."""
import frappe
from frappe import _
from frappe.core.doctype.file.utils import get_content_hash
from frappe.utils import cint

from tatva_connect import tabular
from tatva_connect.access import entitlement
from tatva_connect.api import partner, partner_bulk_job
from tatva_connect.automation import settings as automation
from tatva_connect.lead import mapping
from tatva_connect.notifications import batch

_TOGGLE = "Lead::BulkImport::desk"  # dormant: the Desk import writes nothing until an operator turns it on
_IMPORT = "CRM Lead Import"
_CAP = 100  # a Link dropdown never needs more, and entitlement is checked per row below
_PREVIEW_ROWS = 10

# stage -> (statuses it starts from, status while it runs)
_STAGES = {"validate": (("Draft", "Validation Failed", "Cancelled", "Import Failed"), "Validating"), "import": (("Validated",), "Importing")}
RUNNING = tuple(running for _starts, running in _STAGES.values())


# -- what an import may do next ------------------------------------------------

def next_stage(imp):
	"""The one action this import offers now, or None: the form draws it and the endpoints refuse any other."""
	if imp.is_new() or not imp.data_source():
		return None
	if imp.status in RUNNING:
		return "stop"
	return next((stage for stage, (starts, _running) in _STAGES.items() if imp.status in starts and imp.columns), None)


def running_job(imp):
	"""The CRM Bulk Job in flight for this import, read off its status: a retry keeps an older import job beside the new run."""
	return {"Validating": imp.dry_run_job, "Importing": imp.import_job}.get(imp.status)


# -- stages --------------------------------------------------------------------

@frappe.whitelist()
def start_validation(lead_import):
	"""Queue the dry run: every row written through the live path, then rolled back."""
	imp = _ready(lead_import, "validate")
	if not imp.field_key_map():
		frappe.throw(_("Map at least one column before validating."), title=_("Mapping required"))
	return _queue(imp, "validate")


@frappe.whitelist()
def start_import(lead_import):
	"""Queue the live run of a validated file."""
	return _queue(_ready(lead_import, "import"), "import")


@frappe.whitelist()
def stop_import(lead_import):
	"""Stop the run this import has in flight, through the same cancel the partner API presses."""
	from tatva_connect.api.partner_bulk_worker import request_cancel

	imp = _ready(lead_import, "stop")
	job = running_job(imp)
	if not job or frappe.db.get_value("CRM Bulk Job", job, "status") in partner_bulk_job._TERMINAL:
		frappe.throw(_("This import has nothing running."), title=_("Nothing to stop"))
	return request_cancel(job)


# -- mapping -------------------------------------------------------------------

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


@frappe.whitelist()
def preview(lead_import):
	"""The source's first rows under each column's mapping, as the grid stands; nothing is written."""
	imp = _doc(lead_import)
	labels = {field["field_key"]: field["label"] for field in mapping.mappable_fields(contract=imp.contract_doc())}
	keys = imp.field_key_map()
	columns = [{"header": row.source_column, "skip": row.skip,
	            "field": labels.get(keys.get(row.source_column))} for row in imp.columns or []]
	rows = tabular.read(*imp.payload())[:_PREVIEW_ROWS]
	return {"columns": columns, "rows": [[row.get(c["header"]) for c in columns] for row in rows],
	        "row_count": imp.row_count}


# read_columns and describe_grain removed (header read on attach; the contract field shows the grain); archived in .archive/.


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
		grain = (row.vertical or "", row.crm_group or "", row.program or "")
		if entitlement.grain_entitled(grain, frappe.session.user):
			out.append((row.name, f"{row.vertical or '—'} · {row.crm_group or '—'} · {row.program or '—'}"))
	return out[int(start):int(start) + int(page_len)]


# -- template ------------------------------------------------------------------

@frappe.whitelist()
def template_fields(lead_import):
	"""The fields this contract may write, for the template picker, each marked if it names the lead."""
	imp = _doc(lead_import)
	return [{**field, "identity": _is_identity(field["field_key"])}
	        for field in mapping.mappable_fields(contract=imp.contract_doc())]


@frappe.whitelist()
def download_template(lead_import, fmt="xlsx", keys=None):
	"""A blank file of the chosen field keys, or of every key this contract may write; the identity always comes."""
	imp = _doc(lead_import)
	allowed = [field["field_key"] for field in mapping.mappable_fields(contract=imp.contract_doc())]
	chosen = set(frappe.parse_json(keys)) if keys else set(allowed)
	header = [key for key in allowed if key in chosen or _is_identity(key)]  # never trusts the browser's list
	if not header:
		frappe.throw(_("This contract permits no writable fields."), title=_("Nothing to import"))
	tabular.respond(header, [], fmt, f"lead-import-{imp.name}")


@frappe.whitelist()
def download_refused_rows(lead_import, job):
	"""The rows one of this import's runs refused, read back from its own file with the reason; Data Import's failed-rows download."""
	imp = _own_run(lead_import, job)
	raw, fmt = imp.payload()
	if get_content_hash(raw) != imp.file_hash:
		frappe.throw(_("The file changed after this run, so its row numbers no longer match."), title=_("File changed"))
	reasons = dict(frappe.get_list("CRM Bulk Job Result", filters={"job": job, "action": "failed"},
	                               fields=["record_index", "error_message"], as_list=True, limit_page_length=0))
	header, _rows = tabular.peek(raw, fmt)
	rows = [[*(row.get(h) for h in header), reasons[i]] for i, row in enumerate(tabular.read(raw, fmt)) if i in reasons]
	tabular.respond([*header, _("Reason")], rows, fmt, f"lead-import-{imp.name}-refused")


@frappe.whitelist()
def download_import_log(lead_import, job):
	"""Every row of one of this import's runs with its outcome, as Data Import's Export Import Log."""
	imp = _own_run(lead_import, job)
	results = frappe.get_list("CRM Bulk Job Result", filters={"job": job}, order_by="record_index asc", limit_page_length=0,
	                          fields=["record_index", "action", "record_name", "error_message", "error_detail"])
	gone = _removed(job, [r.record_name for r in results if r.action == "created"])
	# frappe's export_import_log columns and wording, as is, plus the rows a Stop took back
	rows = [[r.record_index + 2, _("Failure"), r.error_message, r.error_detail] if r.action == "failed"
	        else [r.record_index + 2, _("Removed"), _("Removed by Stop {0}").format(r.record_name), ""] if r.record_name in gone
	        else [r.record_index + 2, _("Success"), _("Successfully Imported {0}").format(r.record_name), ""] for r in results]
	tabular.respond([_("Row Numbers"), _("Status"), _("Message"), _("Exception")], rows, "csv", f"lead-import-{imp.name}-log")


@frappe.whitelist()
def removed_by_stop(lead_import, job):
	"""The leads one of this import's runs created and its Stop removed again, for the Import Log to name."""
	_own_run(lead_import, job)
	created = frappe.get_list("CRM Bulk Job Result", filters={"job": job, "action": "created"}, pluck="record_name", limit_page_length=0)
	return sorted(_removed(job, created))


def _removed(job, created):
	"""Of a stopped run's created leads, those no longer there: Stop deletes what the run created, so the log must not call them imported."""
	if not created or frappe.db.get_value("CRM Bulk Job", job, "status") != "Aborted":
		return set()
	return set(created) - set(frappe.get_all("CRM Lead", filters={"name": ["in", created]}, pluck="name"))  # authz-ok: tier-b — existence only, of the caller's own run (`_own_run`)


# -- the job -------------------------------------------------------------------

def _queue(imp, stage):
	"""Submit `stage` through the one job path, gated and capped like a partner job; an import must submit the bytes validated."""
	automation.require(_TOGGLE, _("The Desk Bulk Import"))
	user = frappe.session.user
	pressure = partner_bulk_job.queue_pressure(user, True)
	if pressure:
		frappe.throw(pressure[1], title=_("Queue busy"))
	dry_run = int(stage == "validate")
	raw, fmt = imp.payload()
	digest = get_content_hash(raw)
	if not dry_run:
		imp.assert_importable(digest)
	lane = automation.QUIET if dry_run else imp.bulk_lane  # a rehearsal never triggers anything
	# The tick counts only on Live: Quiet already sends nothing, and a tick left over from a Live choice must not ride along.
	skip = cint(imp.get(batch.SKIP_FIELD)) if lane == automation.LIVE else 0
	job = partner_bulk_job.submit_job(user, "lead_import", fmt, raw,
	                                  extra={"source_import": imp.name, "dry_run": dry_run, "bulk_lane": lane, batch.SKIP_FIELD: skip})
	stamped = {"status": _STAGES[stage][1], "dry_run_job" if dry_run else "import_job": job, "file_hash": digest}
	frappe.db.set_value(_IMPORT, imp.name, stamped)  # bumps modified, so a form opened before this cannot save over it
	return job


def follow_job_status(doc, method=None):
	"""CRM Bulk Job on_update: copy a finished lead_import job's outcome onto its import."""
	if doc.operation != "lead_import" or not doc.get("source_import"):
		return
	if doc.status not in partner_bulk_job._TERMINAL:
		return
	imp = frappe.get_doc(_IMPORT, doc.source_import)
	frappe.db.set_value(_IMPORT, imp.name, _terminal_state(doc, imp))
	frappe.publish_realtime("lead_import_refresh", {"lead_import": imp.name}, user=doc.partner, after_commit=True)


def _terminal_state(job, imp):
	"""A dry run validates when any row passed (the refused are skipped on import); a live run imports."""
	state = {"valid_rows": job.succeeded, "invalid_rows": job.failed}
	if job.get("dry_run"):
		passed = job.status == "JobComplete" and job.succeeded
		state["status"] = "Validated" if passed else "Draft" if job.status == "Aborted" else "Validation Failed"
		state["validated_against"] = imp.file_hash if passed else None
		return state
	if job.status == "Aborted":
		state["status"] = "Cancelled"
	elif job.status == "Failed":
		state["status"] = "Import Failed"
	else:
		state["status"] = "Partially Imported" if job.failed else "Imported"
	return state


# -- helpers -------------------------------------------------------------------

def _doc(lead_import, ptype="read"):
	frappe.has_permission(_IMPORT, ptype, doc=lead_import, throw=True)
	return frappe.get_doc(_IMPORT, lead_import)


def _own_run(lead_import, job):
	"""The import, if `job` is one of its own runs."""
	imp = _doc(lead_import)
	if job not in (imp.dry_run_job, imp.import_job):
		frappe.throw(_("That run is not this import's."), title=_("Not this import"))
	return imp


def _ready(lead_import, stage):
	"""The import, if `stage` is the one it offers now."""
	imp = _doc(lead_import, "write")
	if next_stage(imp) != stage:
		frappe.throw(_("This import cannot {0} now. Reload the form.").format(_(stage)), title=_("Not ready"))
	return imp


def _is_identity(field_key):
	"""The key that names a lead, as the import itself splits it: the partner identity, on the lead."""
	return partner._split_keys([field_key])[0] == [partner.LEAD_IDENTITY]
