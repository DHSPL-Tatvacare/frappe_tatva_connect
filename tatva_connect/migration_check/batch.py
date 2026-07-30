# TEMPORARY — migration reconciliation demo, remove before prod. See REMOVE-ME.md
"""Sample runner — checks up to 100 leads and tallies the result.

WHY THIS READS FRAPPE INTERNALLY. The interactive page goes through the partner API on purpose: a
single lookup should exercise the same path a partner uses. A hundred of them would be ~2,000
seconds, almost all of it queueing on the partner API's bulk gate, for figures the page has already
shown to match the database. So the batch reads Frappe directly and re-checks a few leads through
the API as a CONTROL, printing that agreement on the run. The shortcut is evidenced, not assumed.

Each lead costs 4 LeadSquared calls (lead, notes, tasks, activities), paced. 100 leads ≈ 400 calls
≈ 6 minutes. The ceiling, the daily budget and the single-run lock live in `jobs.py`.

Every lead lands in one of three buckets:
  exact       — every compared figure agrees
  explained   — the only differences are ones the exception register already covers
  investigate — a real difference, listed with its reason so it is actionable
"""

import frappe
from frappe.query_builder.functions import Count

from tatva_connect.migration_check import audit, guard, jobs, storage, totals
from tatva_connect.migration_check import constants as C
from tatva_connect.migration_check.compare import lsq_side
from tatva_connect.migration_check.lsq import BATCH_DELAY, Client

EXACT = "exact"
EXPLAINED = "explained"
INVESTIGATE = "investigate"
NOT_IN_CRM = "not_in_crm"
NOT_IN_LSQ = "not_in_lsq"

WRITE_EVERY = 5  # rewrite the run file every N leads; progress rides Redis
CONTROL_SAMPLE = 3  # leads re-read through the partner API to prove it agrees with the database


def run_id_for(grain: str) -> str:
	stamp = frappe.utils.now_datetime().strftime("%Y%m%d-%H%M%S")
	return f"batch-{grain}-{stamp}"


def execute(run_id: str, grain: str, prospect_ids: list, user: str) -> None:
	"""Background entry point. The run file is rewritten after every lead so the page can follow."""
	resolved = C.Grain(grain)
	payload = {
		"kind": "batch",
		"run_id": run_id,
		"grain": grain,
		"grain_label": resolved.label,
		"status": "running",
		"requested_by": user,
		"started_at": frappe.utils.now(),
		"finished_at": None,
		"total": len(prospect_ids),
		"done": 0,
		"leads": [],
		"summary": {},
		"control": None,
		"lsq_calls": 0,
		"error": None,
	}
	storage.write(run_id, payload)

	try:
		creds = guard.credentials(grain)
		with Client(creds.lsq_host, creds.lsq_access_key, creds.lsq_secret_key, delay=BATCH_DELAY) as client:
			for index, pid in enumerate(prospect_ids, start=1):
				if jobs.abort_requested(run_id):
					payload["status"] = "aborted"
					payload["error"] = f"Aborted by the operator after {index - 1} of {len(prospect_ids)} leads."
					storage.write(run_id, payload)
					return
				payload["leads"].append(_check_one(client, resolved, pid))
				payload["done"] = index
				payload["lsq_calls"] = client.calls
				payload["summary"] = _tally(payload["leads"])
				storage.progress(run_id, index, len(prospect_ids), payload["summary"])
				# The whole run is rewritten periodically, not per lead: at 100 leads that would be
				# a hundred writes of a growing file. Live progress rides the cheap counter above.
				if index % WRITE_EVERY == 0:
					storage.write(run_id, payload)

		payload["control"] = _control(resolved, creds, payload["leads"])
		payload["status"] = "complete"
		audit.log_call(
			"batch_run",
			{"run_id": run_id, "grain": grain, "leads": len(prospect_ids)},
			output=payload["summary"],
			calls=payload["lsq_calls"],
		)
	except Exception as exc:
		payload["status"] = "failed"
		payload["error"] = str(exc)[:300]
		audit.log_error("batch")
		audit.log_call(
			"batch_run",
			{"run_id": run_id, "grain": grain},
			error=payload["error"],
			calls=payload.get("lsq_calls") or 0,
		)
	finally:
		payload["finished_at"] = frappe.utils.now()
		payload["summary"] = _tally(payload["leads"])
		storage.write(run_id, payload)
		storage.clear_progress(run_id)
		jobs.release_lock(run_id)


# -- one lead ----------------------------------------------------------------


def _check_one(client: Client, grain: C.Grain, prospect_id: str) -> dict:
	row = {
		"prospect_id": prospect_id,
		"lead_name": None,
		"verdict": INVESTIGATE,
		"reasons": [],
		"lsq": {},
		"crm": {},
		"by_type": [],
	}

	lead = frappe.db.get_value(
		"CRM Lead",
		{"custom_lsq_prospect_id": prospect_id},
		["name", "custom_vertical", "custom_group"],
		as_dict=True,
	)

	try:
		side = lsq_side(None, grain, prospect_id, client=client)
	except Exception as exc:
		row["reasons"].append(f"LeadSquared read failed: {str(exc)[:120]}")
		return row

	if not side.get("found"):
		row["verdict"] = NOT_IN_LSQ
		row["reasons"].append("No lead with this ProspectID in LeadSquared.")
		return row

	row["lsq"] = side["counts"]

	if not lead:
		row["verdict"] = NOT_IN_CRM
		row["reasons"].append("No lead in Frappe carries this ProspectID.")
		return row

	row["lead_name"] = lead.name
	if C.for_lead(lead.custom_vertical, lead.custom_group) != grain.slug:
		row["reasons"].append(
			f"Lead sits under {lead.custom_vertical} \u2013 {lead.custom_group}, not the selected programme."
		)
		return row

	row["crm"] = _crm_counts(grain, lead.name)
	row["by_type"] = _by_type(grain, lead.name, side.get("by_event_code") or [])
	row["verdict"], reasons = _judge(grain, row["lsq"], row["crm"], row["by_type"])
	row["reasons"].extend(reasons)
	return row


def _crm_counts(grain: C.Grain, lead_name: str) -> dict:
	"""One figure per record type. Activities are summed over the AGREED types only, through the same
	query brain the control-totals certificate counts with (`totals.crm_tasks_by_bare_type`) — so a
	lead that tallies here can never contradict the whole-programme line, and a task of an unmapped
	type (a rep's own to-do) never inflates the comparison against LeadSquared's agreed codes."""
	agreed = set(grain.activity_task_types.values())
	by_type = totals.crm_tasks_by_bare_type(grain, lead_name)
	counts = {
		"activities": sum(n for t, n in by_type.items() if t in agreed),
		"notes": frappe.db.count("FCRM Note", {"reference_docname": lead_name}),
		"files": _file_count(lead_name),
	}
	if grain.call_log_events:
		counts["calls"] = frappe.db.count(
			"CRM Call Log", {"reference_doctype": "CRM Lead", "reference_docname": lead_name}
		)
	return counts


def _file_count(lead_name: str) -> int:
	"""Files homed on the lead or on any of its notes and activities."""
	file, note, task = frappe.qb.DocType("File"), frappe.qb.DocType("FCRM Note"), frappe.qb.DocType("CRM Task")
	notes_of = frappe.qb.from_(note).select(note.name).where(note.reference_docname == lead_name)
	tasks_of = frappe.qb.from_(task).select(task.name).where(task.reference_docname == lead_name)
	rows = (
		frappe.qb.from_(file)
		.select(Count("*").as_("n"))
		.where(
			(file.attached_to_name == lead_name)
			| file.attached_to_name.isin(notes_of)
			| file.attached_to_name.isin(tasks_of)
		)
	).run(as_dict=True)
	return int(rows[0].get("n") or 0) if rows else 0


def _by_type(grain: C.Grain, lead_name: str, lsq_types: list) -> list:
	crm = totals.crm_tasks_by_bare_type(grain, lead_name)

	out, seen = [], set()
	for entry in lsq_types:
		becomes = entry["becomes"]
		seen.add(becomes)
		out.append(
			{
				"type": becomes,
				"code": entry["code"],
				"lsq": entry["count"],
				"crm": crm.get(becomes, 0),
			}
		)
	for task_type, n in crm.items():
		if task_type not in seen:
			out.append({"type": task_type, "code": None, "lsq": 0, "crm": n})
	return out


# -- the verdict -------------------------------------------------------------


def _judge(grain: C.Grain, lsq: dict, crm: dict, by_type: list) -> tuple[str, list]:
	"""Compare only what both systems can count, and name every difference."""
	reasons, explained = [], []

	for key in grain.compares:
		a, b = lsq.get(key), crm.get(key)
		if a is None or b is None or a == b:
			continue
		note = C.EXPECTED_DIFFERENCES.get(key)
		label = C.RESOURCE_LABELS.get(key, key)
		if note:
			explained.append(f"{label}: LeadSquared {a}, Frappe {b} — {note}")
		else:
			reasons.append(f"{label}: LeadSquared {a}, Frappe {b}")

	for row in by_type:
		if row["lsq"] != row["crm"]:
			reasons.append(f"{row['type']}: LeadSquared {row['lsq']}, Frappe {row['crm']}")

	if reasons:
		return INVESTIGATE, reasons
	if explained:
		return EXPLAINED, explained
	return EXACT, []


def _tally(leads: list) -> dict:
	out = {EXACT: 0, EXPLAINED: 0, INVESTIGATE: 0, NOT_IN_CRM: 0, NOT_IN_LSQ: 0}
	for lead in leads:
		out[lead["verdict"]] = out.get(lead["verdict"], 0) + 1
	return out


# -- the control -------------------------------------------------------------


def _control(grain: C.Grain, creds, leads: list) -> dict:
	"""Re-read a few leads through the partner API, to show it agrees with the database.

	This is what makes reading internally defensible rather than merely faster.
	"""
	from tatva_connect.migration_check import api
	from tatva_connect.migration_check.crm import Partner

	checked = [row for row in leads if row.get("lead_name")][:CONTROL_SAMPLE]
	if not checked:
		return {"checked": 0, "agree": 0, "note": "No migrated lead in this run to control against."}

	agree = 0
	try:
		with Partner(api._base_url(), creds.partner_token, api._host()) as partner:
			for row in checked:
				via_api = partner.count("activities", row["lead_name"])
				if via_api == row["crm"].get("activities"):
					agree += 1
	except Exception as exc:
		return {"checked": len(checked), "agree": agree, "note": f"Control incomplete: {str(exc)[:120]}"}

	return {
		"checked": len(checked),
		"agree": agree,
		"note": "Activity counts re-read through the partner API matched the database."
		if agree == len(checked)
		else "The partner API and the database disagreed — investigate before relying on this run.",
	}
