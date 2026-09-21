"""The lead Activity rail's index — ONE declaration of what a rail event is, and the only writer.

WHY THIS EXISTS. The rail shows everything that happened on a lead, newest first, across six source
tables. Read-time merging does not survive the product: every new type (WhatsApp next, and more after)
adds a query leg, so every rail page on every lead for every user touches one more table. Measured on the
fattest dev lead, that read already ships 264 KB and 459 rows to paint 20. So the merge moves to WRITE
time: each event drops one pointer row here, and the rail becomes one indexed seek that costs the same on
a lead with fifty events and one with fifty thousand.

WHAT IT IS NOT. It is not a copy. A row holds which record and when — never the title, the actor or the
status. Those are hydrated from the source row at read time, so renaming a task changes nothing here and
there is no second version of anything to go stale. Only insert and delete touch this table. If it is
ever wrong, `rebuild()` regenerates it from source; nothing can be lost, because nothing is stored.

ONE BRAIN. `event_row()` is the single declaration of what a rail event is. The doc_event hooks call it
and the backfill patch calls it. There is no second place that decides, and no caller writes a row by
hand. `SOURCES` is the single declaration of which doctypes feed the rail — adding WhatsApp is one entry
here plus one hooks.py line, and the read path does not change.

DORMANT BY DEFAULT. This is an operator toggle like every other automation in this app, and it ships OFF.
While it is off nothing is written here and the rail serves from the read-time merge exactly as it does
today; switching it on backfills once in the background and the rail starts reading the index. Switching
it back off returns to the old path with no data loss, because nothing here is a source of truth. That is
what makes this change safe to ship: it is reversible at runtime, not only in a git history.
"""
import time

import frappe

from tatva_connect.activity import lead_events
from tatva_connect.automation.settings import is_enabled
from tatva_connect.propagate import fail_safe

TOGGLE = "Activity::Timeline::indexing"

# Which source doctypes feed the rail, and how each one names the lead it hangs off. The link field is
# NOT uniform — Comment and Communication say `reference_name`, FCRM Note and CRM Task say
# `reference_docname`, File says `attached_to_name`. Same asymmetry `_ATTACHMENT_SOURCES` already documents.
SOURCES = {
	"CRM Call Log": ("call", "reference_docname", "reference_doctype"),
	"FCRM Note": ("note", "reference_docname", "reference_doctype"),
	"CRM Task": ("task", "reference_docname", "reference_doctype"),
	"File": ("file", "attached_to_name", "attached_to_doctype"),
	# comment_type is filtered in _matches: frappe writes Assigned/Shared/Attachment/Like/Deleted as
	# Comment rows too, and only `Comment` is what a rep wrote.
	"Comment": ("comment", "reference_name", "reference_doctype"),
	"Communication": ("email", "reference_name", "reference_doctype"),
	# A message the patient actually received. It had no rail presence at all, so a workflow could WhatsApp
	# her and the audit said nothing happened.
	"WhatsApp Message": ("whatsapp", "reference_name", "reference_doctype"),
	# The Call API node's own record. `integration_request_service` is filtered in _matches: this is
	# frappe's SHARED outbound log and whatsapp/voice/telephony write their transport rows here too.
	"Integration Request": ("api_call", "reference_docname", "reference_doctype"),
	# A SAVE is one event — without it the rail read frappe's ten-row Version window and could never page past it.
	"Version": ("version", "docname", "ref_doctype"),
}

# Whose rail this is. A deal carries the same link fields as a lead (reference_doctype + the same name
# column) and get_attachments already aggregates for both, so it costs one entry, not a second code path.
RAIL_PARENTS = ("CRM Lead", "CRM Deal")

def _file_lead(doc):
	"""The (doctype, name) of the lead a File is an EVENT for — only when it was attached to the lead itself.

	A file that arrived on a message, a comment, a note or a task is a DETAIL of that record: every one of
	those renders its own attachments, so indexing it separately drew the file and the thing it belongs to
	as two rail rows in whatever order they were written. The Attachments tab still lists all of them.
	"""
	parent_doctype, parent_name = doc.get("attached_to_doctype"), doc.get("attached_to_name")
	if not parent_name or parent_doctype not in RAIL_PARENTS:
		return None, None
	return parent_doctype, parent_name


# What a source row must ALSO be to earn a rail line. Applied by the hook, the rebuild, the reconcile AND
# the paged Comments/Emails tabs (api/activities.py reads this map), so every surface agrees on which rows
# exist at all. Frappe writes Assigned/Shared/Attachment/Like/Deleted as Comment rows too, and only
# `Comment` is what a rep wrote; a Communication is an email only for the two types docinfo lists.
PREDICATES = {
	"Comment": {"comment_type": "Comment"},
	"Communication": {"communication_type": ("in", ("Communication", "Automated Message"))},
	# ONE service belongs on a patient's rail. The others in this table are transport plumbing — whatsapp,
	# voice and telephony each log every call they make here, and a rep must never read those.
	"Integration Request": {"integration_request_service": "Workflow Call API"},
}


def _matches(doctype, row) -> bool:
	for field, wanted in PREDICATES.get(doctype, {}).items():
		value = row.get(field)
		if isinstance(wanted, tuple) and wanted[0] == "in":
			if value not in wanted[1]:
				return False
		elif value != wanted:
			return False
	return True


def event_row(doc) -> dict | None:
	"""A source document to its rail event, or None when it does not belong on a rail.

	The ONE declaration. Called by the doc_event hooks and by the backfill — never re-implemented.
	"""
	spec = SOURCES.get(doc.doctype)
	if not spec:
		return None
	kind, link_field, parent_field = spec
	if doc.doctype == "File":
		parent, name = _file_lead(doc)
	else:
		parent, name = doc.get(parent_field), doc.get(link_field)
	if parent not in RAIL_PARENTS or not name or not _matches(doc.doctype, doc):
		return None
	# A save whose every changed field is derived draws nothing, so it is not an event — the rail's own answer, asked here.
	if doc.doctype == "Version" and not lead_events.rail_changes(parent, doc):
		return None
	return {
		"doctype": "CRM Timeline Event",
		"reference_doctype": parent,
		"reference_name": name,
		"event_on": doc.creation,
		"kind": kind,
		"source_doctype": doc.doctype,
		"source_name": doc.name,
	}


# PROPAGATE (@fail_safe): the rail is a pointer index, and `rebuild()` regenerates it from source — so a
# lost pointer costs a rail line until the next rebuild, never the note/call/task the rep was writing.
@fail_safe
def index_event(doc, method=None):
	"""doc_event: after_insert on every SOURCES doctype. A no-op for anything not on a lead's rail."""
	# The toggle first — it is cached, and with the index off a save must not pay for building its rail lines.
	if not is_enabled(TOGGLE):
		return
	row = event_row(doc)
	if row:
		_write(row)


@fail_safe
def drop_event(doc, method=None):
	"""doc_event: on_trash. The pointer dies with the row it points at — a rail must not cite a ghost.

	NOT gated on the toggle: rows written while it was on must still die when their source does, or
	flipping the switch off and on again would leave the index citing deleted records.

	The failed delete used to be caught HERE and logged, which is the shape `propagate.fail_safe` exists
	to replace: a delete that failed inside the database left the transaction poisoned, so swallowing it
	moved the crash onto the caller's own delete. `@fail_safe` undoes it at a savepoint and logs the same.
	"""
	if doc.doctype not in SOURCES:
		return
	# Frappe already enforced the source doc's delete permission to reach on_trash; this drops the
	# derived pointer only, never a business record.
	frappe.db.delete("CRM Timeline Event", {"source_doctype": doc.doctype, "source_name": doc.name})


def _write(row: dict):
	"""Insert the pointer, tolerating the row already being there.

	The unique (source_doctype, source_name) index is what makes this safe: a re-run of the backfill, a
	hook that fires twice, and a restored document all land on the same row instead of growing a second
	one. The duplicate is caught rather than pre-checked, so two concurrent writers cannot both pass a
	check and then both insert.
	"""
	try:
		frappe.get_doc(row).insert(ignore_permissions=True)  # authz-ok: tier-a — writes only this app's derived index, never a business record; the source doc's own create permission was already enforced to reach after_insert
	except frappe.DuplicateEntryError:
		frappe.clear_last_message()
	except Exception:
		# The rail is derived. A failure here must never fail the user's save — it costs a missing row,
		# which `rebuild()` restores and `reconcile()` reports.
		frappe.log_error(title="Timeline event index failed", message=frappe.get_traceback())


def rebuild(reference_name: str) -> int:
	"""Regenerate one lead's index from source. The repair path, and what makes this table disposable."""
	return _rebuild_chunk([reference_name])


def _rebuild_chunk(names: list) -> int:
	"""Regenerate many leads' pointers in one pass: one delete, one read per source, one bulk insert."""
	# Both columns, so `ix_timeline_ref_event` serves it — on reference_name alone this was a table scan.
	frappe.db.delete("CRM Timeline Event", {"reference_doctype": ["in", RAIL_PARENTS], "reference_name": ["in", names]})
	rows = [row for doctype in SOURCES for doc in _source_rows(doctype, names) if (row := event_row(doc))]
	if rows:
		now, actor = frappe.utils.now(), frappe.session.user
		frappe.db.bulk_insert(
			"CRM Timeline Event", ["name", "owner", "creation", "modified", "modified_by", *_POINTER_FIELDS],
			[(frappe.generate_hash(length=10), actor, now, now, actor, *(r[f] for f in _POINTER_FIELDS)) for r in rows],
			ignore_duplicates=True,  # `ix_timeline_source_unique`: a pointer already written is the same pointer
		)
	return len(rows)


# The columns a pointer carries — `event_row`'s own keys, less the doctype.
_POINTER_FIELDS = ("reference_doctype", "reference_name", "event_on", "kind", "source_doctype", "source_name")


def _event_fields(doctype: str) -> list:
	"""The columns `event_row` reads for this source — so a rebuild loads ROWS, not documents.

	`frappe.get_doc` per source row loaded a whole document, with its children and its controller, to read
	four columns off it; over every lead on a live site that is a million document loads to write pointers.
	The list is derived from the same `SOURCES` and `PREDICATES` the writer reads, never typed twice."""
	_kind, link_field, parent_field = SOURCES[doctype]
	fields = {"name", "creation", link_field, parent_field, *PREDICATES.get(doctype, {})}
	if doctype == "Version":
		# `rail_changes` reads the save itself, and the line it builds names who saved.
		fields |= {"data", "owner"}
	return sorted(fields)


def _source_rows(doctype: str, names: list) -> list:
	"""The rows of `doctype` on these leads, in the shape `event_row` reads — ONE query, no document loads.

	A File is asked for by its own parent columns, the same rule `_file_lead` applies to a live insert:
	a file parented to a message or a note is that record's detail, not an event. The Attachments tab still
	lists every document a lead holds, whatever it arrived on.
	"""
	_kind, link_field, parent_field = SOURCES[doctype]
	return [
		frappe._dict(r, doctype=doctype)
		for r in frappe.get_all(
			doctype,
			filters={link_field: ["in", names], parent_field: ["in", RAIL_PARENTS],
					 **PREDICATES.get(doctype, {})},
			fields=_event_fields(doctype),
		)
	]


def activate(enabled):
	"""Automation activator — on a real switch-on, fill the index once in the background.

	Re-enabling after a spell off is safe — `rebuild()` clears a lead before regenerating it, so the pass
	repairs whatever was missed while the switch was down rather than duplicating.
	"""
	# Every migrate re-runs every activator; the index is kept live by its doc_events, so a deploy never rebuilds it.
	if not enabled or frappe.flags.in_migrate:
		return
	enqueue_build()


# The job's method, how far the backfill has got, and whether a slice of it is alive right now.
BUILD_JOB = "tatva_connect.activity.timeline.build_all"
_BUILD_CURSOR = "tatva_connect:timeline_build_cursor"
_BUILD_ALIVE = "tatva_connect:timeline_build_alive"
_BUILD_CHUNK = 100  # leads per commit — one delete, one read per source and one insert each
_BUILD_SLICE = 300  # seconds one job works before it hands the rest to the next
_BUILD_PAUSE = 0.5  # seconds between chunks, so live traffic always has the database back
_BUILD_ALIVE_TTL = 3 * _BUILD_SLICE  # a slice silent this long is dead, and the next start resumes its cursor


def enqueue_build(after=None):
	"""The ONE way the backfill is queued — long queue, one job per slice; a start beside a live slice is a no-op, one after a dead slice resumes it."""
	cache = frappe.cache()
	if after is None:
		if cache.get_value(_BUILD_ALIVE):
			return
		after = cache.get_value(_BUILD_CURSOR) or ""
	cache.set_value(_BUILD_ALIVE, 1, expires_in_sec=_BUILD_ALIVE_TTL)
	frappe.enqueue(
		BUILD_JOB, queue="long", timeout=_BUILD_ALIVE_TTL, job_id=f"{BUILD_JOB}:{after}", deduplicate=True,
		enqueue_after_commit=True, after=after,
	)


def build_all(after: str = "") -> int:
	"""One slice of the backfill: leads in name order past `after`, a commit and a pause per chunk; stops when the toggle is off, else queues the next slice."""
	cache, until, total = frappe.cache(), time.monotonic() + _BUILD_SLICE, 0
	while time.monotonic() < until:
		leads = frappe.get_all("CRM Lead", filters={"name": [">", after]}, order_by="name asc", pluck="name",
							   limit=_BUILD_CHUNK) if is_enabled(TOGGLE) else []
		if not leads:
			cache.delete_value([_BUILD_CURSOR, _BUILD_ALIVE])
			frappe.logger().info(f"CRM Timeline Event: backfill stopped at {after or 'the start'}, {total} events in its last slice")
			return total
		try:
			total += _rebuild_chunk(leads)
		except Exception:
			frappe.db.rollback()
			frappe.log_error(title=f"Timeline backfill failed after {after}", message=frappe.get_traceback())
		after = leads[-1]
		frappe.db.commit()
		cache.set_value(_BUILD_CURSOR, after)
		cache.set_value(_BUILD_ALIVE, 1, expires_in_sec=_BUILD_ALIVE_TTL)
		time.sleep(_BUILD_PAUSE)
	enqueue_build(after)
	frappe.db.commit()
	return total


def reconcile(reference_name: str) -> dict:
	"""Source rows vs index rows for one lead, per kind. Drift is checkable on demand, not discovered by
	a user. Returns {kind: {"source": n, "index": n}} — equal everywhere means the index is honest."""
	out = {}
	for doctype, (kind, _link_field, _parent_field) in SOURCES.items():
		out[kind] = {
			# The SAME resolver the rebuild writes from, so a reconcile cannot report drift the rebuild would not fix.
			"source": len(_source_rows(doctype, [reference_name])),
			"index": frappe.db.count(
				"CRM Timeline Event", {"reference_name": reference_name, "source_doctype": doctype}
			),
		}
	return out
