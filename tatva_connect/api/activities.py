"""Project the lead's full action history into ONE clean Activity-tab audit.

Frappe CRM's native timeline (crm.api.activities.get_activities) is built from version history +
comments + emails + attachments — linked tasks and logged activities never enter it, and a logged
activity's documents/location land as separate, unlinked rows. This override is the single audit
assembler: it folds each logged activity's documents + captured location INTO one chained entry,
relabels stage changes legibly, and drops derived/auto-synced field noise — so the timeline reads
as a per-lead audit. Derived on read, nothing stored. Registered via override_whitelisted_methods.
"""
from collections import Counter

import frappe
from crm.api.activities import _FILE_FIELDS, get_attachments
from crm.api.activities import get_activities as _native_get_activities
from crm.fcrm.doctype.crm_call_log.crm_call_log import parse_call_log
from frappe import _
from frappe.utils import cint

from tatva_connect.activity import timeline
from tatva_connect.activity.api import _blob_key, lead_timeline
from tatva_connect.automation.settings import is_enabled
from tatva_connect.taxonomy import labels
from tatva_connect.taxonomy.labels import LEAD_STAGE

# Lead field-changes we never surface in the audit: derived (custom_stage follows custom_substage)
# or auto-synced headline mirrors (the latest-lab snapshot). Keeps the signal, drops the churn.
_NOISE_FIELDS = {
	"custom_stage",
	"custom_latest_hba1c", "custom_latest_fbs",
	"custom_height_feet", "custom_weight_kg", "custom_last_report_date",
}


def _full_name(user):
	return (user and frappe.db.get_value("User", user, "full_name")) or user


def _stage_label(pk):
	"""A CRM Lead Stage PK (`{program}::{stage}`) to the stage a human reads."""
	return labels.label(pk, LEAD_STAGE)


def _activity_events(entries):
	"""One chained audit row per logged activity — status, location and documents folded in."""
	return [{
		"activity_type": "activity_logged",
		"creation": e["creation"],
		"owner": e["owner"],
		"owner_name": e["owner_name"],
		"verb": "completed" if e.get("done") else "created",
		# The headline a rep reads: the clean type_name, never the composite PK it is keyed by.
		"subject": e["activity_type_label"] or e["activity_type"],
		"status": e.get("status") or "",
		"location": e.get("location"),
		"documents": e.get("documents") or [],
		# Badge automation only while it's still the system's act — an auto-created follow-up.
		# Once a human completes it (done), it's their action, so the badge drops.
		"is_automation": bool(e.get("automated")) and not e.get("done"),
	} for e in entries]


def _task_events(tasks):
	"""Created/closed rows for plain (non-activity) tasks."""
	events = []
	for t in tasks:
		who = t.get("assigned_to") or t.get("owner")
		base = {"owner": who, "owner_name": _full_name(who), "subject": t.get("title")}
		events.append({"activity_type": "task_created", "creation": str(t.get("creation")), **base})
		if (t.get("status") or "").lower() in ("done", "completed"):
			events.append({"activity_type": "task_closed", "creation": str(t.get("modified")), **base})
	return events


def _stage_moved(a):
	"""A native custom_substage change -> a legible stage_moved row (PKs resolved to stage names).
	The rep PICKS the sub-stage (the leaf); the parent custom_stage is derived, so the move that
	matters in the audit is the leaf the rep changed."""
	d = a.get("data") or {}
	return {
		"activity_type": "stage_moved",
		"creation": a["creation"],
		"owner": a["owner"],
		"owner_name": _full_name(a["owner"]),
		"from_stage": _stage_label(d.get("old_value")),
		"to_stage": _stage_label(d.get("value")),
		"is_automation": False,
	}


def _curate_native(activities, doc_keys):
	"""Keep the native rows that belong in the audit; transform/drop the rest:
	- attachment_log for a file folded into an activity -> dropped (shown inside that activity);
	- custom_substage change (the rep's PICK) -> a clean stage_moved row;
	- derived / auto-synced field noise -> dropped.
	"""
	out = []
	for a in activities:
		at = a.get("activity_type")
		d = a.get("data") or {}
		if at == "attachment_log":
			if _blob_key(d.get("file_url")) not in doc_keys:
				out.append(a)
		elif at in ("changed", "added", "removed"):
			field = d.get("field")
			if field == "custom_substage":
				out.append(_stage_moved(a))
			elif field not in _NOISE_FIELDS:
				out.append(a)
		else:
			out.append(a)
	return out


# A call the provider says was answered. Anything else was never handled by a person.
_ANSWERED = ("Completed", "In Progress")


def _label_external_agents(calls):
	"""Name the rep 'External' when a call was answered by someone outside the CRM.

	A provider account can be shared: agents from another company answer some of these calls, and they
	will never be CRM users. Their call still belongs on the lead, but `parse_call_log` leaves the rep
	label empty, which renders as a blank avatar — indistinguishable from a call nobody answered.

	The two are very different things, so the answered-but-unattributed case says so. Derived from what
	is already stored; no field is added and the fork is untouched.
	"""
	# Translated per request, not at import: the locale is not set when the module loads.
	external = _("External")
	for call in calls:
		if call.get("status") not in _ANSWERED:
			continue
		if call.get("type") == "Incoming" and not call.get("receiver"):
			call.setdefault("_receiver", {})["label"] = external
		elif call.get("type") == "Outgoing" and not call.get("caller"):
			call.setdefault("_caller", {})["label"] = external
	return calls


@frappe.whitelist()
def get_activities(name: str):
	activities, calls, notes, tasks, attachments = _native_get_activities(name)
	calls = _label_external_agents(list(calls))
	logged = lead_timeline(name)
	logged_names = {e["name"] for e in logged}
	plain_tasks = [t for t in tasks if t.get("name") not in logged_names]
	doc_keys = {_blob_key(doc["file_url"]) for e in logged for doc in (e.get("documents") or [])}
	doc_keys.discard("")

	activities = (
		_curate_native(list(activities), doc_keys)
		+ _activity_events(logged)
		+ _task_events(plain_tasks)
	)
	activities.sort(key=lambda x: str(x["creation"]), reverse=True)
	_annotate_attachments(notes, "FCRM Note")
	_annotate_automation(tasks, "CRM Task")
	_annotate_task_due(tasks)
	return activities, calls, notes, tasks, attachments


# ---------------------------------------------------------------------------------------------------
# Server-paged card tabs.
#
# The lead's Calls/Notes/Tasks/Attachments tabs page the way the LEADS LIST PAGE pages, because that is
# this app's one pagination pattern and it is copied, not adapted:
#   * params  {filters, order_by, page_length, page_length_count}      ViewControls.vue:526
#   * envelope {data, page_length, page_length_count, total_count, row_count}   crm/api/doc.py:533
#   * Load More REFETCHES 0..N with a bigger page_length                ViewControls.vue:1058
# There is no cursor and no append anywhere in this app, and none is introduced here.
#
# The FIELD LISTS below are crm's own (get_linked_calls / get_linked_notes / get_linked_tasks), so every
# card renders exactly as it does today — only the page is new.
# ---------------------------------------------------------------------------------------------------

# Sort is an allowlist, never interpolated. "When it happened" and "when it last changed" is the whole
# vocabulary these tabs offer, and both columns are indexed on every table below.
_ORDER_FIELDS = ("creation", "modified")
_ORDER_DIRECTIONS = ("asc", "desc")
_DEFAULT_ORDER = "creation desc"

_TABS = {
	"call": (
		"CRM Call Log",
		"reference_docname",
		["name", "caller", "receiver", "from", "to", "duration", "start_time", "end_time",
		 "status", "type", "recording_url", "creation", "modified", "note"],
	),
	"note": (
		"FCRM Note",
		"reference_docname",
		["name", "title", "content", "owner", "modified", "creation"],
	),
	"task": (
		"CRM Task",
		"reference_docname",
		["name", "title", "description", "assigned_to", "due_date", "priority", "status",
		 "modified", "creation"],
	),
}


def _order(order_by: str) -> str:
	"""An allowlisted `<field> <direction>`, or the default. Never the caller's string."""
	field, _sep, direction = (order_by or "").strip().partition(" ")
	if field in _ORDER_FIELDS and direction.lower() in _ORDER_DIRECTIONS:
		return f"{field} {direction.lower()}"
	return _DEFAULT_ORDER


def _envelope(data, page_length, page_length_count, total_count):
	"""crm/api/doc.py:533's shape, so ListFooter binds to it with no translation."""
	return {
		"data": data,
		"page_length": page_length,
		"page_length_count": page_length_count,
		"total_count": total_count,
		"row_count": len(data),
	}


def _decorate(kind, rows):
	"""Exactly the annotations get_activities already applies to each list — no more, no less, so a
	paged tab and the old whole-list payload render identically."""
	if kind == "call":
		return _label_external_agents([parse_call_log(r) for r in rows])
	if kind == "note":
		_annotate_attachments(rows, "FCRM Note")
	elif kind == "task":
		_annotate_automation(rows, "CRM Task")
		_annotate_task_due(rows)
	return rows


def _attachment_page(lead, order_by, page_length):
	"""Attachments are a read-side UNION across every surface a document can arrive through
	(get_attachments), which is a deliberate feature — a rep should not have to remember whether a file
	was added on the note, the task or the lead. That union is not SQL-pageable, so it is sliced here.

	Measured before choosing: 9 File rows on the whole dev site, at most 3 on any one lead. Files are
	structurally the smallest leg — one per document a rep uploads — so paging them in SQL would be
	optimising the thing that is not the problem. If that ever stops being true the union belongs in the
	timeline index, which already models exactly this shape.
	"""
	rows = get_attachments("CRM Lead", lead)
	field, direction = _order(order_by).split(" ")
	rows.sort(key=lambda r: str(r.get(field) or ""), reverse=direction == "desc")
	return rows[:page_length], len(rows)


# The rail's own kind. It is the ONE tab that spans types, so its page is assembled rather than queried.
_RAIL = "all"

# The pure-event rows the rail carries alongside the records. Mirrors the frontend's RAIL_EVENT_TYPES —
# the redundant one-liners the rich cards already say (task_created, attachment_log, activity_logged,
# task_closed) are deliberately absent, so nothing double-counts.
RAIL_EVENT_TYPES = (
	"stage_moved", "changed", "added", "removed", "comment", "communication", "creation",
)

# What the rail hydrates for each kind, keyed by the source doctype the index points at. Same field lists
# the single-kind tabs use, so a card renders identically whether it came from a tab or the rail.
def _rail_fields(source_doctype):
	if source_doctype == "File":
		return _FILE_FIELDS
	for _kind, (doctype, _link, fields) in _TABS.items():
		if doctype == source_doctype:
			return fields
	return ["name", "creation", "modified", "owner"]


def _hydrate(pointers):
	"""Pointer rows -> full rows, in the pointers' order. ONE query per source doctype PRESENT in the
	page — never one per row. The count is bounded by how many types appear in these twenty, so a lead
	with fifty events and one with fifty thousand cost the same."""
	by_doctype = {}
	for p in pointers:
		by_doctype.setdefault(p["source_doctype"], []).append(p["source_name"])

	loaded = {}
	for doctype, names in by_doctype.items():
		rows = frappe.get_all(doctype, filters={"name": ["in", names]}, fields=_rail_fields(doctype))
		kind = next((k for k, (dt, _l, _f) in _TABS.items() if dt == doctype), None)
		if kind:
			rows = _decorate(kind, rows)
		loaded[doctype] = {r["name"]: r for r in rows}

	out = []
	for p in pointers:
		row = loaded.get(p["source_doctype"], {}).get(p["source_name"])
		if row:
			out.append({**row, "kind": p["kind"]})
	return out


def _rail_from_index(lead, page_length, order_by):
	"""The rail as ONE indexed seek. Newest -> oldest across every type, because every type shares one
	`event_on` column. Load More asks for a bigger page of the same query."""
	where = {"reference_doctype": "CRM Lead", "reference_name": lead}
	_field, direction = _order(order_by).split(" ")
	pointers = frappe.get_all(
		"CRM Timeline Event", filters=where,
		fields=["kind", "source_doctype", "source_name", "event_on"],
		# The index is ordered by when the thing HAPPENED; `modified` has no meaning for a pointer.
		order_by=f"event_on {direction}", limit=page_length,
	)
	return _hydrate(pointers), frappe.db.count("CRM Timeline Event", where)


def _rail_from_merge(lead, page_length, order_by):
	"""The rail while the index is dormant — assembled from the existing whole-lead payload and sliced.

	Same envelope, same row shape, so the client never learns which path served it. This is what makes
	the index a switch an operator can flip rather than a deploy: off, the app behaves exactly as it did.
	"""
	activities, calls, notes, tasks, attachments = get_activities(lead)
	rows = (
		# The PURE events — a stage move, a comment, an email, a field change, the lead being created.
		# They are what makes this an audit rather than a list of records, and dropping them would be a
		# silent regression on the path that serves every site until an operator flips the index on.
		[{**r, "kind": "event"} for r in activities if r.get("activity_type") in RAIL_EVENT_TYPES]
		+ [{**r, "kind": "call"} for r in calls]
		+ [{**r, "kind": "note"} for r in notes]
		+ [{**r, "kind": "task"} for r in tasks]
		+ [{**r, "kind": "file"} for r in attachments]
	)
	field, direction = _order(order_by).split(" ")
	rows.sort(key=lambda r: str(r.get(field) or r.get("creation") or ""), reverse=direction == "desc")
	return rows[:page_length], len(rows)


@frappe.whitelist()
def lead_activity(lead: str, kind: str, page_length=20, page_length_count=20,
				  order_by=_DEFAULT_ORDER, filters=None):
	"""One page of one lead's activity tab, in the Leads list envelope.

	Visibility is decided by the LEAD, exactly as the native timeline decides it
	(crm/api/activities.py:176) — a sub-row is visible iff the lead it hangs off is.
	"""
	if not frappe.has_permission("CRM Lead", "read", lead):
		frappe.throw(_("Not permitted"), frappe.PermissionError)

	page_length = cint(page_length) or 20
	page_length_count = cint(page_length_count) or 20

	if kind == _RAIL:
		# The index is an operator toggle and ships dormant, so the rail has two suppliers and ONE
		# contract. The client is never told which one answered.
		rows, total = (
			_rail_from_index(lead, page_length, order_by)
			if is_enabled(timeline.TOGGLE)
			else _rail_from_merge(lead, page_length, order_by)
		)
		return _envelope(rows, page_length, page_length_count, total)

	if kind == "attachment":
		rows, total = _attachment_page(lead, order_by, page_length)
		return _envelope(rows, page_length, page_length_count, total)

	if kind not in _TABS:
		frappe.throw(_("Unknown activity kind {0}").format(kind))

	doctype, link_field, fields = _TABS[kind]
	where = {link_field: lead, **(frappe.parse_json(filters) if filters else {})}
	# `limit` internally, `page_length` on the wire: the param name matches get_data so the frontend is
	# unchanged, while get_all takes the name frappe has not deprecated.
	rows = frappe.get_all(doctype, filters=where, fields=fields,
						  order_by=_order(order_by), limit=page_length)
	total = frappe.db.count(doctype, where)
	return _envelope(_decorate(kind, rows), page_length, page_length_count, total)


def _annotate_task_due(rows):
	"""Fold a display `due` (date + time) onto each rail task row (in place) — the native task carries a raw
	`due_date` datetime but no formatted field, so the rail card's flavor had no date to show."""
	from frappe.utils import format_datetime

	for r in rows:
		r["due"] = format_datetime(r["due_date"], "d MMM yyyy · h:mm a") if r.get("due_date") else None


def _annotate_automation(rows, doctype):
	"""Fold `row["automation"] = {label, run}` onto each row the engine stamped (in place) — drives the
	"Workflow: {label}" attribution + deep-link on the unified cards/rail. Read-only, via the ONE resolver
	(automation.origin); an unstamped row is left as-is (its human owner, exactly as today). No-op for a
	doctype the resolver does not know a stamp for."""
	from tatva_connect.automation.origin import automation_origin

	for r in rows:
		origin = automation_origin(doctype, r.get("name"))
		if origin:
			r["automation"] = origin


def _annotate_attachments(rows, doctype):
	"""Fold an attachment count onto each row (in place) — drives the paperclip indicator on the
	unified note/task cards. ONE File query, counted in Python (this frappe rejects SQL functions
	as string fields, and the row set is small). No-op when there are no rows."""
	names = [r["name"] for r in rows]
	if not names:
		return
	counts = Counter(
		f.attached_to_name
		for f in frappe.get_all(
			"File",
			filters={"attached_to_doctype": doctype, "attached_to_name": ["in", names]},
			fields=["attached_to_name"],
		)
	)
	for r in rows:
		r["attachments"] = counts.get(r["name"], 0)
