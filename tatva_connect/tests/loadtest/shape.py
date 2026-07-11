# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Turn one LSQ bundle into the request bodies the partner API accepts.

The field map is the migration's signed-off mapping.json, read as data. Only the parts a partner
could legitimately send are used: the API refuses to be told who owns a record or when it was made,
so no audit field is shaped here. Notes have no endpoint and are not shaped at all.
"""
import json
import re
from urllib.parse import urlparse

FILE_URL = re.compile(r"https?://[^\s\"'\\,{]+", re.I)
FILE_HINT = ("leadsquaredcdn", "s3.amazon", "amazonaws", "lsq-private-storage", "blob.core",
             "attachment", ".pdf", ".jpg", ".jpeg", ".png", ".doc")
CALL_STATUS = {"answered": "Completed", "notanswered": "No Answer", "not answered": "No Answer",
               "busy": "Busy", "failed": "Failed", "missed": "No Answer", "noanswer": "No Answer",
               "cancelled": "Canceled", "canceled": "Canceled"}
GENDER = {"F": "Female", "M": "Male", "FEMALE": "Female", "MALE": "Male", "OTHER": "Other"}
SOURCE_DATA = re.compile(r"SourceData\{=\}(\{.*?\})\{next\}")


def _blank(v):
	return v in (None, "", [], {})


def _program(rule, lead):
	"""The lead's program, from the lead's own data. Pinned wins, then Source, then a drug signal.
	No signal means no program: it is never guessed."""
	if not rule:
		return None
	if rule.get("pinned"):
		return rule["pinned"]
	source = (lead.get("Source") or "").strip()
	by_source = rule.get("by_source") or {}
	if source in by_source:
		return by_source[source]
	by_drug = rule.get("by_drug") or {}
	for field, value_map in (by_drug.get("value_map") or {}).items():
		value = (lead.get(field) or "").strip()
		if value and value in value_map:
			return value_map[value]
	for field, program in (by_drug.get("presence_map") or {}).items():
		if (lead.get(field) or "").strip():
			return program
	return None


# A multi-row child is addressed by a key the caller must supply. LSQ's lead is flat -- it holds one
# drug profile and no cycle date -- so the key is taken from the drug's own chemo date, which is the
# date that cycle actually ran. A lead with no chemo date on record yields no key, and its row is
# dropped rather than given an invented one: a fabricated clinical date is worse than a missing row.
KEY_SOURCES = {
	"cycle_date": ("custom_nivo_chemo_date", "custom_ujvira_chemo_date", "custom_tucatinib_chemo_date",
	               "custom_nivolumab_order_placed_date", "custom_tukavo_order_placed_date"),
}


def _row_key(key_field, row):
	for candidate in KEY_SOURCES.get(key_field, ()):
		value = row.get(candidate)
		if not _blank(value):
			return value
	return None


def lead_body(bundle, fmap, children_spec=None, dropped=None):
	"""The lead as the API accepts it. `children_spec` is what lead_schema declares, so the multi-row
	contract is honoured by discovery rather than by assumption."""
	lead = bundle["lead"]
	body = {frappe_field: lead[lsq_field]
	        for lsq_field, frappe_field in fmap["lead_core"].items()
	        if not _blank(lead.get(lsq_field))}
	body.update(fmap["routing"])

	children_spec = children_spec or {}
	for child_field, spec in (fmap.get("children") or {}).items():
		row = {frappe_field: lead[lsq_field]
		       for lsq_field, frappe_field in spec["fields"].items()
		       if not _blank(lead.get(lsq_field))}
		if not row:
			continue

		declared = children_spec.get(child_field) or {}
		key_field = declared.get("key_field") if declared.get("multi_row") else None
		if key_field:
			key = _row_key(key_field, row)
			if key is None:
				if dropped is not None:
					dropped[child_field] += 1
				continue
			row[key_field] = key

		body[child_field] = [row]

	program = _program(fmap.get("program"), lead)
	if program:
		body["custom_current_program"] = program

	for field in ("gender", "custom_gender", "custom_patient_gender"):
		value = body.get(field)
		if isinstance(value, str) and value.strip():
			body[field] = GENDER.get(value.strip().upper(), value)

	body["external_id"] = str(bundle["prospect_id"])
	return body


def event_code(act):
	return str(act.get("ActivityEvent") or act.get("EventCode") or "")


def source_id(act):
	return str(act.get("ProspectActivityId") or act.get("Id") or "")


def activity_bodies(bundle, fmap, lead_name):
	"""One activity per LSQ activity whose event code the map keeps. Call events are not activities.

	The bulk read returns an activity's slots flat on the row, and omits a slot it has no value for,
	so a mapped slot that is absent simply does not become a field.
	"""
	types = fmap["activity_task_types"]
	fields = fmap["activity_fields"]
	call_events = set(fmap.get("call_log_events") or {})
	out = []
	for act in bundle.get("activities") or []:
		code = event_code(act)
		if code in call_events or code not in types:
			continue
		values = {frappe_field: act[slot]
		          for slot, frappe_field in (fields.get(code) or {}).items()
		          if not _blank(act.get(slot))}
		sid = source_id(act)
		out.append({
			"lead": lead_name,
			"task_type": types[code],
			"values": values,
			"external_id": sid,
			"_source_id": sid,
		})
	return out


def call_bodies(bundle, fmap, lead_name):
	"""LSQ models a call as an activity on a call event code. The API models it as a call."""
	events = fmap.get("call_log_events") or {}
	if not events:
		return []
	out = []
	for act in bundle.get("activities") or []:
		direction = events.get(event_code(act))
		if not direction:
			continue
		match = SOURCE_DATA.search(act.get("ActivityEvent_Note") or "")
		source = {}
		if match:
			try:
				source = json.loads(match.group(1))
			except (ValueError, TypeError):
				source = {}
		status = str(source.get("Status") or "").strip().lower()
		sid = source_id(act)
		out.append({
			"lead": lead_name,
			"direction": "Incoming" if direction == "Incoming" else "Outgoing",
			"from_number": str(source.get("SourceNumber") or "")[:20],
			"to_number": str(source.get("DestinationNumber") or "")[:20],
			"status": CALL_STATUS.get(status, "Completed"),
			"duration": int(source.get("CallDuration") or 0),
			"started_at": act.get("CreatedOn"),
			"external_id": sid,
			"_source_id": sid,
		})
	return out


def file_bodies(bundle, fmap, lead_name, activity_names):
	"""Every presigned URL LSQ hands back on a kept activity, homed on the activity it came from.

	LSQ packs several URLs comma-joined into one slot, so the URLs are split out rather than taken
	whole. The storage path is the stable part of a presigned URL and is used as the label.
	"""
	types = fmap["activity_task_types"]
	out = []
	for act in bundle.get("activities") or []:
		if event_code(act) not in types:
			continue
		target = activity_names.get(source_id(act))
		if not target:
			continue
		seen = set()
		for value in act.values():
			if not isinstance(value, str) or "http" not in value:
				continue
			for url in FILE_URL.findall(value):
				if not any(hint in url.lower() for hint in FILE_HINT):
					continue
				path = urlparse(url).path
				if path in seen:
					continue
				seen.add(path)
				out.append({
					"lead": lead_name,
					"activity": target,
					"file_url": url,
					"filename": (path.split("/")[-1] or "lsq_attachment"),
					"external_id": path,
					"_source_id": path,
				})
	return out
