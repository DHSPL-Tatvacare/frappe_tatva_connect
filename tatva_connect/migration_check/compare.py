# TEMPORARY — migration reconciliation demo, remove before prod. See REMOVE-ME.md
"""Orchestration: one ProspectID plus one grain in, one comparison out.

The two sides are deliberately separate entry points. The LeadSquared read takes ~2s; the Frappe
read takes ~15s because its list endpoints queue on the partner API's bulk gate (see the note in
`crm.py`). The page calls both at once and paints each as it lands, so the fast side is not held
hostage by the slow one.

THE GRAIN IS NOT COSMETIC. It selects the LeadSquared account, the agreed event codes, the field
map and the partner token. Picking the wrong one would query the wrong LeadSquared account and the
wrong partner scope, and report a perfectly healthy lead as missing — so `locate` checks the lead's
real grain against the chosen one and says so plainly rather than reporting a false gap.
"""

import frappe

from tatva_connect.migration_check import constants as C
from tatva_connect.migration_check.crm import Partner
from tatva_connect.migration_check.lsq import Client, activity_fields, event_code

MATCH = "match"
DIFFER = "differ"
MISSING_IN_CRM = "missing_in_crm"


# -- resolution --------------------------------------------------------------


def locate(prospect_id: str, slug: str) -> dict:
	"""Find the lead in Frappe and check it really belongs to the chosen grain.

	`custom_lsq_prospect_id` is unique across every lead, so the lookup is deliberately NOT filtered
	by grain — that is what lets a wrong selection be named instead of silently reported as missing.
	"""
	row = frappe.db.get_value(
		"CRM Lead",
		{"custom_lsq_prospect_id": prospect_id},
		["name", "custom_vertical", "custom_group", "custom_current_program"],
		as_dict=True,
	)
	if not row:
		return {"lead_name": None, "program": None, "grain_mismatch": None}

	actual = C.for_lead(row.custom_vertical, row.custom_group)
	mismatch = None
	if actual != slug:
		found = C.GRAINS[actual]["label"] if actual else f"{row.custom_vertical} \u2013 {row.custom_group}"
		mismatch = {"expected": C.Grain(slug).label, "actual": found, "actual_slug": actual}

	return {
		"lead_name": row.name,
		"program": row.custom_current_program,
		"grain_mismatch": mismatch,
	}


# -- LeadSquared side --------------------------------------------------------


def lsq_side(creds, grain: C.Grain, prospect_id: str, client: Client | None = None) -> dict:
	"""Counts and the raw lead. Activities are split into agreed codes and everything else.

	Pass `client` to reuse one paced session — the batch checks a hundred leads and must not open
	(or un-pace) a connection per lead.
	"""
	own = client is None
	if own:
		client = Client(creds.lsq_host, creds.lsq_access_key, creds.lsq_secret_key)
	try:
		lead = client.lead(prospect_id)
		if lead is None:
			return {"found": False}
		notes = client.notes(prospect_id)
		tasks = client.tasks(prospect_id)
		activities = client.activities(prospect_id)
		api_calls = client.calls  # LeadSquared requests spent, NOT call-log records
	finally:
		if own:
			client.close()

	in_scope: dict[str, int] = {}
	out_of_scope = calls = files = 0

	for act in activities:
		code = event_code(act)
		if code in grain.call_log_events:
			calls += 1
		elif code in grain.activity_task_types:
			in_scope[code] = in_scope.get(code, 0) + 1
		else:
			out_of_scope += 1
			continue

		fields = activity_fields(act)
		for slot in grain.attachment_slots.get(code, ()):
			if _is_file(fields.get(slot)):
				files += 1

	files += sum(1 for n in notes if (n.get("AttachmentURL") or "").strip())
	done = sum(1 for t in tasks if str(t.get("Status")) in C.COMPLETED_TASK_STATUSES)

	counts = {
		"activities": sum(in_scope.values()),
		"notes": len(notes),
		"files": files,
		"tasks_open": len(tasks) - done,
		"tasks_completed": done,
	}
	if grain.call_log_events:
		counts["calls"] = calls

	return {
		"found": True,
		"lead": lead,
		"calls": api_calls,
		"counts": counts,
		"by_event_code": [
			{
				"code": code,
				"lsq_name": grain.lsq_event_names.get(code, ""),
				"becomes": grain.activity_task_types[code],
				"count": in_scope[code],
			}
			for code in sorted(in_scope, key=lambda c: -in_scope[c])
		],
		"out_of_scope_activities": out_of_scope,
	}


def _is_file(value) -> bool:
	return isinstance(value, str) and value.strip().lower().startswith("http")


# -- Frappe side -------------------------------------------------------------


def crm_count(creds, lead_name: str, resource: str, base_url: str, host: str | None) -> dict:
	"""One figure, one short request. The page ticks a step for each.

	Activities also return their split by task type, so the breakdown reconciles type by type
	rather than only in total. That costs one paged read, not one call per type.
	"""
	with Partner(base_url, creds.partner_token, host) as partner:
		if resource == "activities":
			grouped = partner.activity_types(lead_name)
			return {"count": grouped["total"], "by_type": grouped["by_type"]}
		return {"count": partner.count(resource, lead_name), "by_type": None}


def field_comparison(
	creds, grain: C.Grain, prospect_id: str, lead_name: str, base_url: str, host: str | None
) -> tuple[list[dict], list[dict]]:
	"""Both lead records, then a field-by-field comparison. `lead_get` is not bulk-metered."""
	with Client(creds.lsq_host, creds.lsq_access_key, creds.lsq_secret_key) as client:
		lsq_lead = client.lead(prospect_id)
	if not lsq_lead:
		return [], []

	with Partner(base_url, creds.partner_token, host) as partner:
		crm_lead = partner.lead(lead_name)

	return compare_fields(grain, lsq_lead, crm_lead)


# -- field comparison --------------------------------------------------------


def compare_fields(grain: C.Grain, lsq_lead: dict, crm_lead: dict) -> tuple[list[dict], list[dict]]:
	"""One row per LeadSquared field holding a value. Returns (migrated, not migrated)."""
	mapped: list[dict] = []
	unmapped: list[dict] = []

	for lsq_field, raw in sorted(lsq_lead.items()):
		lsq_value = _clean(raw)
		if not lsq_value:
			continue

		target = grain.field_target(lsq_field)
		if target is None:
			unmapped.append({"lsq_field": lsq_field, "lsq_value": lsq_value})
			continue

		child_table, crm_field = target
		crm_value = _crm_value(crm_lead, child_table, crm_field)
		mapped.append(
			{
				"lsq_field": lsq_field,
				"lsq_value": lsq_value,
				"crm_field": f"{child_table}.{crm_field}" if child_table else crm_field,
				"crm_value": crm_value,
				"status": _status(lsq_value, crm_value),
			}
		)

	return mapped, unmapped


def _crm_value(crm_lead: dict, child_table: str | None, field: str) -> str:
	if not child_table:
		return _clean(crm_lead.get(field))

	rows = crm_lead.get(child_table) or []
	if not isinstance(rows, list):
		return ""
	# Multi-row sections show the latest row, matching every other consumer.
	for row in reversed(rows):
		value = _clean(row.get(field))
		if value:
			return value
	return ""


def _status(lsq_value: str, crm_value: str) -> str:
	if not crm_value:
		return MISSING_IN_CRM
	return MATCH if _comparable(lsq_value) == _comparable(crm_value) else DIFFER


def _clean(value) -> str:
	"""LeadSquared sends numbers, nulls and HTML. Flatten to a trimmed display string."""
	if value is None or value is False:
		return ""
	if value is True:
		return "Yes"
	text = str(value).strip()
	return "" if text.lower() in ("", "null", "none") else text


def _comparable(value: str) -> str:
	"""Compare on meaning, not formatting: case, spacing, LeadSquared's midnight suffix — and the
	grain-composite primary key. A Frappe Link to a grain master stores `{vertical}::{group}::{program}::{name}`
	(taxonomy/labels.py) while LeadSquared holds the bare name, so every such field read as "different"
	when the two values meant the same thing. The answer is the LAST segment, whatever the key's arity."""
	text = " ".join(value.split()).lower()
	if C.KEY_SEPARATOR in text:
		text = text.rsplit(C.KEY_SEPARATOR, 1)[-1].strip()
	for suffix in (" 00:00:00.000", " 00:00:00", ".000"):
		if text.endswith(suffix):
			text = text[: -len(suffix)]
	return text
