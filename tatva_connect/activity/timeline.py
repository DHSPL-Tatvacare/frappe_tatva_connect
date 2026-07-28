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
import frappe
from crm.api.activities import _ATTACHMENT_SOURCES, get_attachments

from tatva_connect.automation.settings import is_enabled

TOGGLE = "Activity::Timeline::indexing"

# Which source doctypes feed the rail, and how each one names the lead it hangs off. The link field is
# NOT uniform — Comment and Communication say `reference_name`, FCRM Note and CRM Task say
# `reference_docname`, File says `attached_to_name`. Same asymmetry `_ATTACHMENT_SOURCES` already documents.
SOURCES = {
	"CRM Call Log": ("call", "reference_docname", "reference_doctype"),
	"FCRM Note": ("note", "reference_docname", "reference_doctype"),
	"CRM Task": ("task", "reference_docname", "reference_doctype"),
	"File": ("file", "attached_to_name", "attached_to_doctype"),
	"Comment": ("comment", "reference_name", "reference_doctype"),
	"Communication": ("email", "reference_name", "reference_doctype"),
}

# The rail belongs to a lead. A deal gets the same treatment the day it needs one — the row already
# carries reference_doctype, so nothing here changes but this tuple.
RAIL_PARENTS = ("CRM Lead",)

# A FILE is the one type whose parent is usually NOT the lead. Frappe gives a File exactly one parent, and
# each surface parents its own: a comment's file belongs to the Comment, a note's to the FCRM Note, an
# emailed one to the Communication. The Attachments tab shows all of them anyway — deliberately, so a rep
# never has to remember WHERE a document was added — and this index reproduces that, it does not narrow
# it. Which surfaces those are, and the field each uses to name its lead, is already declared ONCE in
# crm's `_ATTACHMENT_SOURCES`; it is READ here, never restated.
_FILE_SURFACES = {child: link_field for child, link_field, _label in _ATTACHMENT_SOURCES}


def _file_lead(doc):
	"""The (doctype, name) of the lead a File belongs on — directly, or through the surface it arrived on.

	Returns (None, None) for a file that hangs off nothing a rail cares about, which is most files on a
	site (avatars, letterheads, an import's source sheet).
	"""
	parent_doctype, parent_name = doc.get("attached_to_doctype"), doc.get("attached_to_name")
	if not parent_name:
		return None, None
	if parent_doctype in RAIL_PARENTS:
		return parent_doctype, parent_name
	link_field = _FILE_SURFACES.get(parent_doctype)
	if not link_field:
		return None, None
	surface = frappe.db.get_value(
		parent_doctype, parent_name, ["reference_doctype", link_field], as_dict=True
	)
	if not surface or surface.get("reference_doctype") not in RAIL_PARENTS:
		return None, None
	return surface.get("reference_doctype"), surface.get(link_field)


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
	if parent not in RAIL_PARENTS or not name:
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


def index_event(doc, method=None):
	"""doc_event: after_insert on every SOURCES doctype. A no-op for anything not on a lead's rail."""
	if not is_enabled(TOGGLE):
		return
	row = event_row(doc)
	if not row:
		return
	_write(row)


def drop_event(doc, method=None):
	"""doc_event: on_trash. The pointer dies with the row it points at — a rail must not cite a ghost.

	NOT gated on the toggle: rows written while it was on must still die when their source does, or
	flipping the switch off and on again would leave the index citing deleted records.
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
	frappe.db.delete("CRM Timeline Event", {"reference_name": reference_name})
	written = 0
	for doctype in SOURCES:
		for name in _source_names(doctype, reference_name):
			row = event_row(frappe.get_doc(doctype, name))
			if row:
				_write(row)
				written += 1
	return written


def _source_names(doctype: str, reference_name: str) -> list:
	"""Which rows of `doctype` belong to this lead — the same answer the tab gives, from the same brain.

	Files go through `get_attachments`, crm's own read-side union, because a lead's documents are not the
	ones filed against the lead: they are those PLUS everything parented to its comments, emails, WhatsApp
	messages, notes and tasks. Asking the File table for `attached_to_name = <lead>` would index a
	fraction of them and the rail would quietly lose the rest.
	"""
	if doctype == "File":
		return [f["name"] for p in RAIL_PARENTS for f in get_attachments(p, reference_name)]
	_kind, link_field, parent_field = SOURCES[doctype]
	return frappe.get_all(
		doctype,
		filters={link_field: reference_name, parent_field: ["in", RAIL_PARENTS]},
		pluck="name",
	)


def activate(enabled):
	"""Automation activator — on enable, fill the index once in the background.

	The build is enqueued, never inline: a migrate or a settings save must not block on every lead on
	the site. Re-enabling after a spell off is safe — `rebuild()` clears a lead before regenerating it,
	so the pass repairs whatever was missed while the switch was down rather than duplicating.
	"""
	if not enabled:
		return
	frappe.enqueue(
		"tatva_connect.activity.timeline.build_all", queue="long", timeout=14400, enqueue_after_commit=True
	)


def build_all(chunk: int = 500) -> int:
	"""Fill the index for every lead, in chunks. The activator's job, and the patch's."""
	start, total = 0, 0
	while True:
		leads = frappe.get_all(
			"CRM Lead", pluck="name", order_by="creation asc", start=start, page_length=chunk
		)
		if not leads:
			break
		for lead in leads:
			try:
				total += rebuild(lead)
			except Exception:
				frappe.log_error(title=f"Timeline backfill failed for {lead}", message=frappe.get_traceback())
		# Commit per chunk — a pass over every lead on the site must not hold one transaction.
		frappe.db.commit()
		start += chunk
	frappe.logger().info(f"CRM Timeline Event: indexed {total} events")
	return total


def reconcile(reference_name: str) -> dict:
	"""Source rows vs index rows for one lead, per kind. Drift is checkable on demand, not discovered by
	a user. Returns {kind: {"source": n, "index": n}} — equal everywhere means the index is honest."""
	out = {}
	for doctype, (kind, _link_field, _parent_field) in SOURCES.items():
		out[kind] = {
			# Counted through the SAME resolver the rebuild uses, so a file reachable only through a note
			# is counted on both sides — a reconcile that counted differently would report false drift.
			"source": len(_source_names(doctype, reference_name)),
			"index": frappe.db.count(
				"CRM Timeline Event", {"reference_name": reference_name, "source_doctype": doctype}
			),
		}
	return out
