"""Project the lead's full action history into ONE clean Activity-tab audit.

Frappe CRM's native timeline (crm.api.activities.get_activities) is built from version history +
comments + emails + attachments — linked tasks and logged activities never enter it, and a logged
activity's documents/location land as separate, unlinked rows. This override is the single audit
assembler: it folds each logged activity's documents + captured location INTO one chained entry,
relabels stage changes legibly, and drops derived/auto-synced field noise — so the timeline reads
as a per-lead audit. Derived on read, nothing stored. Registered via override_whitelisted_methods.
"""
from collections import Counter
from itertools import chain

import frappe
from crm.api.activities import _FILE_FIELDS, get_attachments
from crm.api.activities import get_activities as _native_get_activities
from crm.fcrm.doctype.crm_call_log.crm_call_log import parse_call_log
from frappe import _
from frappe.utils import cint

from tatva_connect.activity import timeline
from tatva_connect.activity.api import _blob_key, capture_flags, lead_timeline
from tatva_connect.activity.lead_events import history
from tatva_connect.automation.settings import is_enabled
from tatva_connect.list_engine import derived
from tatva_connect.taxonomy import labels

# Lead field-changes we never surface in the audit: derived (custom_stage follows custom_substage).
# The five headline mirrors left this set on 2026-08-10 with the sync that wrote them — nothing auto-writes them now, so a change to one is a real edit and belongs in the trail.
_NOISE_FIELDS = {
	"custom_stage",
}


def _full_name(user):
	return (user and frappe.db.get_value("User", user, "full_name")) or user


def _stage_label(pk):
	"""A CRM Lead Stage PK (`{program}::{stage}`) to the stage a human reads — `labels.stage_label`, the same
	reading the spotlight index and the hover card use, so one stage is one word on every surface.

	Falls back to the key only when the master row is gone: the generic `labels.label` fell back to it whenever
	`display_label` was blank, which printed `GoodFlip::Consulted` here while the other two printed `Consulted`.
	An audit row must not blank, and where the stage no longer exists its key is the only truth left."""
	return labels.stage_label(pk)[0] or pk or ""


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


def _ungroup(activities):
	"""crm collapses a BURST of edits by one owner into a single row and hangs the rest on `other_versions`
	(crm/api/activities.py:handle_multiple_versions -> parse_grouped_versions), so on a record one person
	edits, every change but the first is hidden behind a key nothing renders: four stage moves thirteen
	seconds apart drew ONE line. The rail's INDEXED supplier never groups — it reads `lead_events` straight
	— so expanding here is what makes the merged supplier and the Activity tab tell the same history."""
	out = []
	for a in activities:
		out.append(a)
		out.extend(a.pop("other_versions", None) or [])
	return out


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
		_curate_native(_ungroup(list(activities)), doc_keys)
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

# Sort is an allowlist, never interpolated, and lists only what an index serves — the paging indexes are (link, creation), so `modified` would filesort the whole filtered set and nothing asks for it.
_ORDER_FIELDS = ("creation",)
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
	# `custom_task_type` is the card's type label AND what decides whether Done opens the capture form.
	"task": (
		"CRM Task",
		"reference_docname",
		["name", "title", "description", "assigned_to", "owner", "due_date", "priority", "status",
		 "custom_task_type", "custom_completed_on", "modified", "modified_by", "creation",
		 "custom_location_latitude", "custom_location_longitude", "custom_location_address",
		 "custom_location_captured_at"],
	),
	# Comments and emails are ordinary tables too. They read the whole-lead payload only because nothing
	# had asked them to page — and that payload drags every call, task and note with it, which is the
	# whole cost. Which ROWS count as a comment / an email is `timeline.PREDICATES`, not restated here.
	"comment": (
		"Comment",
		"reference_name",
		["name", "content", "owner", "creation", "modified", "comment_type"],
	),
	"email": (
		"Communication",
		"reference_name",
		["name", "subject", "content", "sender", "sender_full_name", "recipients", "cc", "bcc",
		 "communication_type", "communication_medium", "communication_date", "read_by_recipient",
		 "delivery_status", "creation", "modified"],
	),
}


# What a free-text search looks at, per tab. The tabs searched these same fields on the client before the
# page moved to the server; the list is kept here so one page and one count agree on what "matches" means.
# A dict of filters is ANDed by frappe, so a search across several fields is `or_filters`, not `filters`.
_SEARCH_FIELDS = {
	"call": ["status", "type", "from", "to"],
	"note": ["title", "content"],
	"task": ["title", "description", "status", "priority"],
	# The client searched comments over author + body; only the body is a column, and the author is
	# already a Filter on this tab, so the server searches what it can actually index.
	"comment": ["content"],
	"email": ["subject", "content", "sender", "sender_full_name"],
}

# What the Filter button may narrow on, per tab — the catalog the frontend publishes (ACTIVITY_FILTERS),
# and nothing else. An unlisted key is dropped rather than trusted.
_FILTERABLE = {
	"call": ("type", "status"),
	"note": ("owner",),
	"task": ("status", "priority", "assigned_to", "custom_task_type"),
	"attachment": ("file_type", "is_private"),
	"comment": ("owner",),
}

# A page is a page. Without a ceiling `page_length` is a request for the whole table.
_MAX_PAGE = 500


def _condition(doctype, field, wanted):
	"""One `where` entry as a frappe tuple. A value may carry its own operator, the way timeline.PREDICATES writes `("in", (...))`."""
	if isinstance(wanted, (list, tuple)) and len(wanted) == 2 and isinstance(wanted[0], str):
		return [doctype, field, wanted[0], wanted[1]]
	return [doctype, field, "=", wanted]


def _with_derived(doctype, picked, where):
	"""`where` plus any DERIVED field picked — through the one resolver that already turns a bucket into real column tuples, so Task Status narrows this tab without its rule being written twice. What may be picked is what the doctype DECLARES, asked of the registry rather than restated; nothing picked leaves the dict untouched, which is every tab but Tasks."""
	declared = {f.fieldname: f for f in derived.for_doctype(doctype)}
	wanted = [(declared[f], v) for f, v in picked.items() if f in declared]
	if not wanted:
		return where
	snap = derived.snapshot()
	conditions = [_condition(doctype, f, v) for f, v in where.items()]
	for field, value in wanted:
		bucket = field.bucket(value)
		if bucket:
			conditions += derived.resolve(field, bucket, snap)
	return conditions


def _scope(doctype, name):
	"""The (doctype, name) records a surface answers for — a deal answers for itself AND its lead."""
	if doctype != "CRM Deal":
		return [(doctype, name)]
	lead = frappe.db.get_value("CRM Deal", name, "lead")
	return [(doctype, name), ("CRM Lead", lead)] if lead else [(doctype, name)]


def _search_or_filters(kind, search):
	term = (search or "").strip()
	if not term:
		return None
	return [[field, "like", f"%{term}%"] for field in _SEARCH_FIELDS.get(kind, [])] or None


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
		_annotate_task_type(rows)
		_annotate_task_rep(rows)
	elif kind == "comment":
		_attach_files(rows, "Comment")
		for r in rows:
			r["activity_type"] = "comment"
	elif kind == "email":
		return _as_email_activities(rows)
	return rows


def _as_email_activities(rows):
	"""A Communication row -> the feed entry the Emails tab renders, field for field what
	get_lead_activities built. The nested `data` is not decoration: EmailArea reads `activity.data.*`."""
	_attach_files(rows, "Communication")
	return [{
		"name": r["name"],
		"activity_type": "communication",
		"communication_type": r["communication_type"],
		"communication_date": r["communication_date"] or r["creation"],
		"creation": r["creation"],
		"data": {
			"subject": r["subject"],
			"content": r["content"],
			"sender_full_name": r["sender_full_name"],
			"sender": r["sender"],
			"recipients": r["recipients"],
			"cc": r["cc"],
			"bcc": r["bcc"],
			"attachments": r["attachments"],
			"read_by_recipient": r["read_by_recipient"],
			"delivery_status": r["delivery_status"],
		},
	} for r in rows]


def _attach_files(rows, doctype):
	"""Fold the attached File ROWS onto each row (in place) — the list, not a count, because a comment and
	an email render their attachments inline. ONE query for the whole page, never one per row."""
	names = [r["name"] for r in rows]
	if not names:
		return
	by_parent = {}
	for f in frappe.get_all(
		"File",
		filters={"attached_to_doctype": doctype, "attached_to_name": ["in", names]},
		fields=[*_FILE_FIELDS, "attached_to_name"],
	):
		by_parent.setdefault(f["attached_to_name"], []).append(f)
	for r in rows:
		r["attachments"] = by_parent.get(r["name"], [])


def _attachment_page(lead, order_by, page_length, picked=None, search=None, doctype="CRM Lead"):
	"""Attachments are a read-side UNION across every surface a document can arrive through
	(get_attachments), which is a deliberate feature — a rep should not have to remember whether a file
	was added on the note, the task or the lead. That union is not SQL-pageable, so it is sliced here.

	Measured before choosing: 9 File rows on the whole dev site, at most 3 on any one lead. Files are
	structurally the smallest leg — one per document a rep uploads — so paging them in SQL would be
	optimising the thing that is not the problem. If that ever stops being true the union belongs in the
	timeline index, which already models exactly this shape.
	"""
	# One call per record in scope: a deal shows its own files and its lead's, same rule as every other tab.
	rows = [r for dt, n in _scope(doctype, lead) for r in get_attachments(dt, n)]
	picked = {f: v for f, v in (picked or {}).items() if f in _FILTERABLE["attachment"]}
	# Narrowed in Python for the same reason the page is sliced here: the set is a union, not a table.
	for field_name, wanted in picked.items():
		rows = [r for r in rows if str(r.get(field_name) or "") == str(wanted)]
	term = (search or "").strip().lower()
	if term:
		rows = [r for r in rows
				if term in f"{r.get('file_name') or ''} {r.get('file_type') or ''}".lower()]
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

# Subjects that appear ONLY on the rail — no tab lists them, so there is no `_TABS` row to read their
# shape from. Declared once, in the SAME (doctype, link_field, fields) shape a tab uses, and read by BOTH
# suppliers: the index path hydrates through `_rail_fields` and the merge path queries through
# `_rail_only_rows`. Two suppliers, one declaration, so flipping the index toggle cannot change the rail.
# Which ROWS of these tables belong to a rail at all is `timeline.PREDICATES`, never restated here.
_RAIL_ONLY = {
	# `profile_name` and `owner` are the row's two candidate ACTORS, and both are needed: with neither
	# fetched, the rail's `getUser(undefined)` fell back to the SESSION user, so every reader saw their
	# own name on every message. `from` is the fallback label for a patient WATI sent no display name for.
	"whatsapp": (
		"WhatsApp Message", "reference_name",
		["name", "message", "message_type", "content_type", "from", "to", "type", "status",
		 "profile_name", "owner", "custom_workflow_correlation", "creation", "modified"],
	),
	# The Call API node's own record. `error` rides along because a failed outbound call is the whole
	# reason a reader opens this row.
	"api_call": (
		"Integration Request", "reference_docname",
		["name", "integration_request_service", "url", "status", "error", "creation", "modified"],
	),
}


# What the rail hydrates for each kind, keyed by the source doctype the index points at. Same field lists
# the single-kind tabs use, so a card renders identically whether it came from a tab or the rail.
def _rail_fields(source_doctype):
	if source_doctype == "File":
		return _FILE_FIELDS
	for _kind, (doctype, _link, fields) in _TABS.items():
		if doctype == source_doctype:
			return fields
	for _kind, (doctype, _link, fields) in _RAIL_ONLY.items():
		if doctype == source_doctype:
			return fields
	return ["name", "creation", "modified", "owner"]


def _rail_only_rows(scoped, page_length, order_by):
	"""The rail-only subjects, for the MERGE supplier — one query each, scoped to the lead and narrowed by
	the same `timeline.PREDICATES` the index writer applies. Capped at the page length: a page can show no
	more than that however many rows a subject holds."""
	rows = []
	for kind, (doctype, link_field, fields) in _RAIL_ONLY.items():
		anchor = scoped[0][1] if len(scoped) == 1 else ["in", [n for _dt, n in scoped]]
		where = {link_field: anchor, **timeline.PREDICATES.get(doctype, {})}
		rows += [{**r, "kind": kind} for r in frappe.get_all(  # authz-ok: tier-b — scoped to records the caller was authorised for above
			doctype, filters=where, fields=fields, order_by=_order(order_by), limit=page_length,
		)]
	return rows


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
		# str() BOTH sides: CRM Task is autoincrement, so its `name` is an int while the pointer
		# stores varchar. Keying on the raw value silently dropped every task from the rail.
		loaded[doctype] = {str(r["name"]): r for r in rows}

	# `activity_type` is stamped by _decorate, the same call the tabs make — a comment and an email carry
	# it because the rail renders them through the very components those tabs use.
	out = []
	for p in pointers:
		row = loaded.get(p["source_doctype"], {}).get(str(p["source_name"]))
		if row:
			out.append({**row, "kind": p["kind"]})
	return out


def _rail_from_index(lead, page_length, order_by, doctype="CRM Lead"):
	"""The rail as ONE indexed seek over the RECORDS, merged with the record's own history.

	Two legs, because a rail line is one of two things. A call, note, task, file, comment or email IS a
	record — it owns a row, so the index points at it and the seek costs the same on a lead with fifty
	events and one with fifty thousand. "Changed Patient Age" is NOT a record — it is derived from a
	`Version` at read time, so no pointer can exist for it and one bounded query fetches it instead
	(frappe shows ten edits and no more, and this inherits that window rather than choosing its own).

	The history leg is fully materialised — eleven rows at most — so merging it in and slicing gives the
	true top of the union, not an approximation, and the footer count stays exact.
	"""
	# A deal's rail carries its lead's events too — the same both-records scope every other tab uses.
	scoped = _scope(doctype, lead)
	where = (
		{"reference_doctype": doctype, "reference_name": lead}
		if len(scoped) == 1
		else {"reference_doctype": ["in", [dt for dt, _n in scoped]], "reference_name": ["in", [n for _dt, n in scoped]]}
	)
	field, direction = _order(order_by).split(" ")
	pointers = frappe.get_all(
		"CRM Timeline Event", filters=where,
		fields=["kind", "source_doctype", "source_name", "event_on"],
		# The index is ordered by when the thing HAPPENED; `modified` has no meaning for a pointer.
		order_by=f"event_on {direction}", limit=page_length,
	)
	events = [{**r, "kind": "event"} for r in history(doctype, lead)
			  if r.get("activity_type") in RAIL_EVENT_TYPES]
	rows = _hydrate(pointers) + events
	# Same key the merge supplier sorts by, so both paths order identically.
	rows.sort(key=lambda r: str(r.get(field) or r.get("creation") or ""), reverse=direction == "desc")
	return rows[:page_length], frappe.db.count("CRM Timeline Event", where) + len(events)


def _rail_from_merge(lead, page_length, order_by, doctype="CRM Lead"):
	"""The rail while the index is dormant — assembled from the existing whole-lead payload and sliced.

	Same envelope, same row shape, so the client never learns which path served it. This is what makes
	the index a switch an operator can flip rather than a deploy: off, the app behaves exactly as it did.
	"""
	# One payload per record in scope, so this path carries a deal's lead exactly as the index path does.
	legs = [get_activities(n) for _dt, n in _scope(doctype, lead)]
	activities, calls, notes, tasks, attachments = [list(chain(*parts)) for parts in zip(*legs, strict=True)]
	rows = (
		# The PURE events — a stage move, a comment, an email, a field change, the lead being created.
		# They are what makes this an audit rather than a list of records, and dropping them would be a
		# silent regression on the path that serves every site until an operator flips the index on.
		[{**r, "kind": "event"} for r in activities if r.get("activity_type") in RAIL_EVENT_TYPES]
		+ [{**r, "kind": "call"} for r in calls]
		+ [{**r, "kind": "note"} for r in notes]
		+ [{**r, "kind": "task"} for r in tasks]
		+ [{**r, "kind": "file"} for r in attachments]
		# The rail-only subjects. `get_activities` is crm's own and knows nothing about them, so the merge
		# path asks for them directly — the index path gets them from its pointers and asks for nothing.
		+ _rail_only_rows(_scope(doctype, lead), page_length, order_by)
	)
	field, direction = _order(order_by).split(" ")
	rows.sort(key=lambda r: str(r.get(field) or r.get("creation") or ""), reverse=direction == "desc")
	return rows[:page_length], len(rows)


@frappe.whitelist()
def lead_activity(lead: str, kind: str, page_length=20, page_length_count=20,
				  order_by=_DEFAULT_ORDER, filters=None, search=None, doctype="CRM Lead"):
	"""One page of one lead's activity tab, in the Leads list envelope.

	Visibility is decided by the LEAD, exactly as the native timeline decides it
	(crm/api/activities.py:176) — a sub-row is visible iff the lead it hangs off is.

	`search` and `filters` are applied HERE, not on the client. They used to run over the whole loaded
	list; once the server pages, a client-side filter would only ever see the twenty rows in hand and
	quietly report "no matches" for a record sitting on page three.
	"""
	# The Activity component serves deals from this same endpoint. Resolving a deal id against CRM Lead
	# 404s a rep and — for a caller `has_permission` short-circuits — renders an empty tab instead.
	if doctype not in timeline.RAIL_PARENTS:
		frappe.throw(_("Unsupported record type {0}").format(doctype))
	if not frappe.has_permission(doctype, "read", lead):
		frappe.throw(_("Not permitted"), frappe.PermissionError)

	page_length = min(cint(page_length) or 20, _MAX_PAGE)
	page_length_count = cint(page_length_count) or 20
	picked = frappe.parse_json(filters) if filters else {}

	if kind == _RAIL:
		# The index is an operator toggle and ships dormant, so the rail has two suppliers and ONE
		# contract. The client is never told which one answered.
		rows, total = (
			_rail_from_index(lead, page_length, order_by, doctype)
			if is_enabled(timeline.TOGGLE)
			else _rail_from_merge(lead, page_length, order_by, doctype)
		)
		return _envelope(rows, page_length, page_length_count, total)

	if kind == "attachment":
		rows, total = _attachment_page(lead, order_by, page_length, picked, search, doctype)
		return _envelope(rows, page_length, page_length_count, total)

	if kind not in _TABS:
		frappe.throw(_("Unknown activity kind {0}").format(kind))

	# Captured before the unpack rebinds `doctype` to the CHILD doctype this tab lists.
	scoped = _scope(doctype, lead)
	doctype, link_field, fields = _TABS[kind]
	# The lead scope is written LAST so a caller's filter can never displace it. Spread the other way and
	# `filters={"reference_docname": "<someone else's lead>"}` returns that lead's rows to anyone who can
	# read this one. Only the fields a tab publishes are accepted at all.
	allowed = {f: v for f, v in picked.items() if f in _FILTERABLE.get(kind, ())}
	# The lead scope AND the row predicate are written last: which rows are a comment or an email at all
	# is declared once, in timeline.PREDICATES, and read here rather than restated.
	anchor = scoped[0][1] if len(scoped) == 1 else ["in", [n for _dt, n in scoped]]
	where = _with_derived(doctype, picked, {**allowed, link_field: anchor, **timeline.PREDICATES.get(doctype, {})})
	matching = _search_or_filters(kind, search)
	# `limit` internally, `page_length` on the wire: the param name matches get_data so the frontend is
	# unchanged, while get_all takes the name frappe has not deprecated.
	rows = frappe.get_all(doctype, filters=where, or_filters=matching, fields=fields,
						  order_by=_order(order_by), limit=page_length)
	# The count carries the SAME narrowing as the page, so "20 of 103" never contradicts what is on screen.
	total = (
		frappe.db.count(doctype, where)
		if not matching
		# A count, not a pluck: the doctype's default `ORDER BY modified` made the plucked variant
		# abandon the index entirely on CRM Call Log (type=ALL + filesort).
		# The dict form is the only one frappe accepts (query.py:2135); the string 500'd every search.
		else frappe.get_all(doctype, filters=where, or_filters=matching,
							fields=[{"COUNT": "name", "as": "n"}], order_by=None)[0]["n"]
	)
	return _envelope(_decorate(kind, rows), page_length, page_length_count, total)


def _annotate_task_rep(rows):
	"""Everything the task CARD reads that the row itself does not carry, folded on in place — two
	queries for the whole page, never one per row.

	Each is the same answer `lead_task_board` gave before this tab served it: the person is
	`assigned_to or owner`, a completion is stamped only when the task is done, the location is the
	captured fix or nothing, and the attachment count is one grouped read. Diffing what the card reads
	against what the page sent is how the missing avatar was found — do that, not this list, when a
	field goes blank."""
	from frappe.utils import flt, format_datetime

	done_states = ("Done", "Canceled")
	who = {r.get("assigned_to") or r.get("owner") for r in rows}
	who |= {r.get("modified_by") for r in rows if r.get("status") in done_states}
	who.discard(None)
	users = {
		u.name: u for u in frappe.get_all(
			"User", filters={"name": ["in", sorted(who)]}, fields=["name", "full_name", "user_image"],
		)
	} if who else {}
	names = [r.get("name") for r in rows if r.get("name")]
	attachments = Counter(
		f.attached_to_name for f in frappe.get_all(
			"File", filters={"attached_to_doctype": "CRM Task", "attached_to_name": ["in", names]},
			fields=["attached_to_name"],
		)
	) if names else Counter()

	for row in rows:
		rep = row.get("assigned_to") or row.get("owner")
		spec = users.get(rep)
		row["rep"] = rep
		row["rep_name"] = (spec.full_name if spec else None) or rep
		row["rep_image"] = spec.user_image if spec else None

		done = row.get("status") in done_states
		stamped = row.get("custom_completed_on") or (row.get("modified") if done else None)
		completer = row.get("modified_by") if done else None
		row["completed_on"] = format_datetime(stamped, "d MMM yyyy") if (done and stamped) else None
		row["completed_by"] = (
			(users.get(completer) or {}).get("full_name") or completer if completer else None
		)

		lat, lng = row.get("custom_location_latitude"), row.get("custom_location_longitude")
		row["location"] = {
			"lat": flt(lat), "lng": flt(lng),
			"address": row.get("custom_location_address") or "",
			"captured_at": str(row["custom_location_captured_at"]) if row.get("custom_location_captured_at") else None,
		} if (lat and lng) else None
		row["attachments"] = attachments.get(row.get("name"), 0)
	return rows


def _annotate_task_type(rows):
	"""Fold each task's type label + `needs_capture` onto the row (in place), from the ONE reader that
	owns what a type means. The full field schema is never fetched here — the modal loads it for the
	single type it is opening."""
	flags = capture_flags(r.get("custom_task_type") for r in rows)
	for row in rows:
		label, needs = flags.get(row.get("custom_task_type"), (None, False))
		# Named for the column it labels (the pair `activity.api` already writes), and the fallback reads a LABEL, never the key that used to reach a rep's card.
		row["custom_task_type_label"] = label or labels.label(row.get("custom_task_type"), labels.TASK_TYPE)
		row["needs_capture"] = needs
	return rows


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
