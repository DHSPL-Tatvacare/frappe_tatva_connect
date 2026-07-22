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
			lead_total = _count_leads(client)
			by_code = _count_activities(client, resolved)
			payload["lsq_calls"] = client.calls

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


def _count_leads(client: Client) -> int:
	"""Exact, by paging. Only ProspectID is requested, so each page stays small."""
	seen: set[str] = set()
	for page in range(1, MAX_LEAD_PAGES + 1):
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


def _count_activities(client: Client, grain: C.Grain) -> dict:
	"""One call per event code. RecordCount prices the whole code however large."""
	counts = {}
	for code in sorted(grain.mapped_event_codes, key=int):
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


def _crm_activities_by_type(grain: C.Grain) -> dict:
	"""Migrated activities per bare task type, for this grain only."""
	rows = frappe.db.sql(
		"""
		SELECT SUBSTRING_INDEX(t.custom_task_type, '::', -1) AS task_type, COUNT(*) AS n
		FROM `tabCRM Task` t
		JOIN `tabCRM Lead` l ON l.name = t.reference_docname
		WHERE t.reference_doctype = 'CRM Lead'
		  AND t.custom_lsq_activity_id IS NOT NULL
		  AND l.custom_vertical = %s AND l.custom_group = %s
		GROUP BY task_type
		""",
		(grain.vertical, grain.group),
		as_dict=True,
	)
	return {r.task_type: int(r.n) for r in rows}


def _crm_calls(grain: C.Grain) -> int:
	row = frappe.db.sql(
		"""
		SELECT COUNT(*) AS n
		FROM `tabCRM Call Log` c
		JOIN `tabCRM Lead` l ON l.name = c.reference_docname
		WHERE c.reference_doctype = 'CRM Lead'
		  AND l.custom_vertical = %s AND l.custom_group = %s
		  AND l.custom_lsq_prospect_id IS NOT NULL
		""",
		(grain.vertical, grain.group),
		as_dict=True,
	)
	return int(row[0].n) if row else 0


# -- the certificate ---------------------------------------------------------


def _build_rows(grain: C.Grain, lead_total: int, by_code: dict) -> list[dict]:
	rows = [_row("Leads", lead_total, _crm_leads(grain))]

	crm_by_type = _crm_activities_by_type(grain)
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
