# TEMPORARY — migration reconciliation demo, remove before prod. See REMOVE-ME.md
"""Control totals per grain — the certificate an operator signs.

WHAT IS EXACT AND WHAT IS NOT, stated plainly because the certificate is worthless if it quietly
mixes the two:

  * LEADS are exact. `Leads.Get` returns no count, so the whole set is paged with a one-column
    projection and the distinct ProspectIDs are counted.
  * ACTIVITIES are exact and nearly free. `RetrieveByActivityEvent` returns `RecordCount` on a
    PageSize:5 call, so one call prices an entire event code however large it is.
  * NOTES and TASKS are NOT available at account level. LeadSquared exposes them per lead only,
    which would be one call per lead. They are left out of the certificate and covered by the
    sample instead. Completed tasks are already counted as activities, so the activity total
    carries the bulk of task volume anyway.

The Frappe side is read internally: the per-lead checker already proves the partner API and the
database agree, and a control total should not be priced at one HTTP round trip per figure.
"""

import frappe
from frappe.query_builder.functions import Count

from tatva_connect.migration_check import audit, guard, storage
from tatva_connect.migration_check import constants as C
from tatva_connect.migration_check.lsq import BATCH_DELAY, Client

LEAD_PAGE_SIZE = 1000
MAX_LEAD_PAGES = 200  # backstop: 200,000 leads
ALL_TIME = "2000-01-01 00:00:00"
# LeadSquared insists on exactly yyyy-MM-dd HH:mm:ss and rejects anything carrying microseconds,
# which is what frappe.utils.now() returns.
LSQ_TIME = "%Y-%m-%d %H:%M:%S"


def run_id_for(grain: str) -> str:
	stamp = frappe.utils.now_datetime().strftime("%Y%m%d-%H%M%S")
	return f"totals-{grain}-{stamp}"


def execute(run_id: str, grain: str, user: str) -> None:
	"""Background entry point. Writes progress to the run file as it goes."""
	from tatva_connect.migration_check import jobs

	resolved = C.Grain(grain)
	payload = {
		"kind": "totals",
		"run_id": run_id,
		"grain": grain,
		"grain_label": resolved.label,
		"status": "running",
		"requested_by": user,
		"started_at": frappe.utils.now(),
		"finished_at": None,
		"rows": [],
		"summary": {},
		"lsq_calls": 0,
		"error": None,
	}
	storage.write(run_id, payload)

	try:
		creds = guard.credentials(grain)
		with Client(creds.lsq_host, creds.lsq_access_key, creds.lsq_secret_key, delay=BATCH_DELAY) as client:
			lead_total = _count_leads(client, run_id)
			by_code = _count_activities(client, resolved, run_id)
			payload["lsq_calls"] = client.calls

		if jobs.abort_requested(run_id):
			payload["status"] = "aborted"
			payload["error"] = "Aborted by the operator."
			storage.write(run_id, payload)
			return

		payload["rows"] = _build_rows(resolved, lead_total, by_code)
		payload["summary"] = _summarise(payload["rows"])
		payload["status"] = "complete"
		audit.log_call(
			"totals_run",
			{"run_id": run_id, "grain": grain},
			output=payload["summary"],
			calls=payload["lsq_calls"],
		)
	except Exception as exc:
		payload["status"] = "failed"
		payload["error"] = str(exc)[:300]
		audit.log_error("totals")
		audit.log_call("totals_run", {"run_id": run_id, "grain": grain}, error=payload["error"])
	finally:
		payload["finished_at"] = frappe.utils.now()
		storage.write(run_id, payload)
		jobs.release_lock(run_id)


# -- LeadSquared -------------------------------------------------------------


def _count_leads(client: Client, run_id: str | None = None) -> int:
	"""Exact, by paging. Only ProspectID is requested, so each page stays small."""
	from tatva_connect.migration_check import jobs

	seen: set[str] = set()
	for page in range(1, MAX_LEAD_PAGES + 1):
		if run_id and jobs.abort_requested(run_id):
			break
		rows = client._call(
			"LeadManagement.svc/Leads.Get",
			body={
				"Parameter": {
					"LookupName": "ModifiedOn",
					"LookupValue": ALL_TIME,
					"SqlOperator": ">=",
				},
				"Columns": {"Include_CSV": "ProspectID"},
				"Sorting": {"ColumnName": "ProspectID", "Direction": 0},
				"Paging": {"PageIndex": page, "PageSize": LEAD_PAGE_SIZE},
			},
		)
		if not rows:
			break
		before = len(seen)
		seen.update(r.get("ProspectID") for r in rows if r.get("ProspectID"))
		# A repeating page would otherwise spin to the backstop.
		if len(seen) == before or len(rows) < LEAD_PAGE_SIZE:
			break
	return len(seen)


def _count_activities(client: Client, grain: C.Grain, run_id: str | None = None) -> dict:
	"""One call per event code. RecordCount prices the whole code however large."""
	from tatva_connect.migration_check import jobs

	counts = {}
	for code in sorted(grain.mapped_event_codes, key=int):
		if run_id and jobs.abort_requested(run_id):
			break
		payload = client._call(
			"ProspectActivity.svc/CustomActivity/RetrieveByActivityEvent",
			body={
				"Parameter": {
					"FromDate": ALL_TIME,
					"ToDate": frappe.utils.now_datetime().strftime(LSQ_TIME),
					"ActivityEvent": int(code),
				},
				"Paging": {"PageIndex": 1, "PageSize": 5},
			},
		)
		counts[code] = int(payload.get("RecordCount") or 0)
	return counts


# -- Frappe ------------------------------------------------------------------


def _crm_leads(grain: C.Grain) -> int:
	return frappe.db.count(
		"CRM Lead",
		{
			"custom_vertical": grain.vertical,
			"custom_group": grain.group,
			"custom_lsq_prospect_id": ["is", "set"],
		},
	)


def crm_tasks_by_bare_type(grain: C.Grain, lead_name: str | None = None) -> dict:
	"""Activities per bare task type — the ONE query both the certificate (per grain) and the bulk
	check (per lead) count through, so the two tabs can never disagree on what "counted" means.

	COUNTS EVERY TASK OF AN AGREED TYPE, migrated or not, and the pages say so. It used to filter
	`custom_lsq_activity_id IS NOT NULL` — the provenance stamp that would tell the two apart — but
	the migration never writes that column: measured 3,046 of 3,046 tasks NULL, so the predicate
	excluded every row and the Frappe side of every activity line read a structural zero while the
	calls line (keyed off the LEAD's prospect id, which IS written) reconciled fine. Counting what
	can honestly be counted beats reporting a zero that looks like a migration failure. Stamping
	provenance is the real fix and is raised in docs/pending.

	The composite key is split in PYTHON, not by a SQL function: a grain has tens of task types, so
	the grouping is small, and it keeps the whole read inside the query builder."""
	task, lead = frappe.qb.DocType("CRM Task"), frappe.qb.DocType("CRM Lead")
	query = (
		frappe.qb.from_(task)
		.join(lead)
		.on(lead.name == task.reference_docname)
		.select(task.custom_task_type, Count("*").as_("n"))
		.where(task.reference_doctype == "CRM Lead")
		.where(task.custom_task_type.isnotnull())
		.where(lead.custom_vertical == grain.vertical)
		.where(lead.custom_group == grain.group)
		.groupby(task.custom_task_type)
	)
	if lead_name:
		query = query.where(task.reference_docname == lead_name)
	rows = query.run(as_dict=True)

	out: dict[str, int] = {}
	for row in rows:
		bare = (row.get("custom_task_type") or "").rsplit(C.KEY_SEPARATOR, 1)[-1]
		out[bare] = out.get(bare, 0) + int(row.get("n") or 0)
	return out


def _crm_calls(grain: C.Grain) -> int:
	call, lead = frappe.qb.DocType("CRM Call Log"), frappe.qb.DocType("CRM Lead")
	rows = (
		frappe.qb.from_(call)
		.join(lead)
		.on(lead.name == call.reference_docname)
		.select(Count("*").as_("n"))
		.where(call.reference_doctype == "CRM Lead")
		.where(lead.custom_vertical == grain.vertical)
		.where(lead.custom_group == grain.group)
		.where(lead.custom_lsq_prospect_id.isnotnull())
	).run(as_dict=True)
	return int(rows[0].get("n") or 0) if rows else 0


# -- the certificate ---------------------------------------------------------


def _build_rows(grain: C.Grain, lead_total: int, by_code: dict) -> list[dict]:
	rows = [_row("Leads", lead_total, _crm_leads(grain))]

	crm_by_type = crm_tasks_by_bare_type(grain)
	activity_codes = [c for c in by_code if c in grain.activity_task_types]
	call_codes = [c for c in by_code if c in grain.call_log_events]

	lsq_activities = sum(by_code[c] for c in activity_codes)
	crm_activities = sum(crm_by_type.get(grain.activity_task_types[c], 0) for c in activity_codes)
	rows.append(_row("Activities (all agreed types)", lsq_activities, crm_activities))

	for code in sorted(activity_codes, key=lambda c: -by_code[c]):
		becomes = grain.activity_task_types[code]
		rows.append(
			_row(
				f"    {grain.lsq_event_names.get(code) or becomes}",
				by_code[code],
				crm_by_type.get(becomes, 0),
				note=f"code {code} → {becomes}",
			)
		)

	if call_codes:
		lsq_calls = sum(by_code[c] for c in call_codes)
		rows.append(
			_row(
				"Calls",
				lsq_calls,
				_crm_calls(grain),
				note="LeadSquared writes some calls twice; duplicates are collapsed on the way in",
			)
		)

	rows.append(
		{
			"label": "Notes, open tasks and files",
			"lsq": None,
			"crm": None,
			"variance": None,
			"note": "LeadSquared reports these per lead only, so they are verified by sample rather "
			"than counted here. Completed tasks are already included in Activities.",
		}
	)
	return rows


def _row(label: str, lsq: int, crm: int, note: str = "") -> dict:
	return {"label": label, "lsq": lsq, "crm": crm, "variance": crm - lsq, "note": note}


def _summarise(rows: list[dict]) -> dict:
	scored = [r for r in rows if r.get("variance") is not None]
	return {
		"lines": len(scored),
		"matching": sum(1 for r in scored if r["variance"] == 0),
		"variances": sum(1 for r in scored if r["variance"] != 0),
	}
