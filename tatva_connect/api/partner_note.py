"""Gated partner NOTE API: create / read / update / list / delete notes on a lead.

Shares the ONE brain in `tatva_connect.api._base`: the SAME `_resolve_caller` enablement gate (the
single enabled `CRM Lead API Mapping` row + its grain governs notes just like leads/activities/calls/
files, and there is NO per-entity enablement), the SAME `resolve_lead` grain-scoped resolver, the SAME
`_ok`/`_fail` envelope, error codes, rate limit, `_run_bulk` / `_bulk_read` partial-success engines and
`_list_ok` list envelope.

Notes land in frappe/crm's native `FCRM Note`: the SAME doctype the desk writes, and the SAME doctype
the note visibility rule scopes (`Note::FCRM Note::visibility`). No parallel note store.

IDENTITY. A note is addressed by `name`, the FCRM Note primary key, returned when it was created. That
is the only address. `external_id` is the caller's own label (stored in `custom_external_id`): echoed
back on every read, never interpreted, never used to find a note, never a dedup key. A POST creates a
note; a PUT updates one by `name`. Retries are made safe with the Idempotency-Key header.

Lead attribution. `lead`/`mobile_no` -> the shared grain-scoped `resolve_lead`. There is NO phone-guess
fallback here (calls have one, notes do not): a call that cannot be attributed is still a real call and
is kept unlinked. A clinical note attached to the wrong patient, or to nobody, is worse than a
refused write. A note with no resolvable lead is refused.

CONTENT. `content` is an HTML body (Text Editor). Frappe sanitises it on save (`_sanitize_content`), so
no hand-rolled tag stripping happens here. `title` is mandatory on the doctype but OPTIONAL in this
contract: a caller with no title concept gets a metadata header built for them, never the body echoed
back as its own title.

  GET    note_schema       -> discovery: the note payload contract (fields, types, required)
  GET    note_get          -> one note by `name`, grain-scoped, generic not-found
  GET    note_list         -> a lead's notes, paginated
  POST   note_create       -> create a note; returns its `name`
  PUT    note_update       -> update a note by `name`, scope-checked
  DELETE note_delete       -> delete a note by `name`, scope-checked
  POST   note_get_bulk     -> {"names":[...]} (<= 100), partial success
  POST   note_create_bulk  -> {"notes":[...]} (<= 100), partial success
  PUT    note_update_bulk  -> {"updates":[{"name":..,..}]} (<= 100), partial success
  DELETE note_delete_bulk  -> {"names":[...]} (<= 100), partial success
"""
import frappe
from frappe import _
from frappe.utils import get_datetime

from tatva_connect.api._base import (
	ACTION_CREATED,
	ACTION_DELETED,
	ACTION_FETCHED,
	ACTION_UPDATED,
	EXTERNAL_ID_FIELD,
	_api,
	_bulk_read,
	_list_ok,
	_ok,
	_page,
	_read_required_list,
	_resolve_caller,
	_run_bulk,
	_schema_ok,
	resolve_lead,
	scoped_by_lead,
	stamp_external_id,
	throw_field,
	validate_external_id,
)
from tatva_connect.api.field_spec import FieldSpec, collect, describe

# All numeric caps (bulk size, list page sizes) come from the CRM Partner API Settings
# Single via _cfg(). One source of truth, no module-local copy.

# The note's payload contract — declared ONCE, read by `describe` (what note_schema advertises) and by
# `collect` (what the write path accepts). Discovery equals ingestion because neither owns a field list.
# A target-less spec is one this module resolves itself: `mobile_no` finds a lead, `created_at`
# backdates `creation` (a framework default field, not a docfield — see field_spec).
NOTE_FIELDS = (
	FieldSpec("lead",        "Lead",        "reference_docname"),
	FieldSpec("mobile_no",   "Mobile No"),
	FieldSpec("external_id", "External ID", EXTERNAL_ID_FIELD),
	FieldSpec("title",       "Title",       "title"),
	FieldSpec("content",     "Content",     "content", required=True),
	FieldSpec("created_at",  "Created At",  fieldtype="Datetime"),
)

_TITLE_CAP = 140


# -- helpers -----------------------------------------------------------------

def _derive_title(fields, data):
	"""FCRM Note.title is mandatory on the doctype but OPTIONAL in this contract, so a caller with no
	title concept still gets a real label. It is a metadata HEADER, never the body: deriving a title
	from the content would just print the note twice.

	`fields` is `collect(NOTE_FIELDS, data)` — the title override is read THROUGH it, not off raw
	`data`, so a future read_only/hidden title spec is honoured here too. `created_at` stays a raw
	`data` read: it is target-less (backdates `creation`, a framework default field), so collect()
	never carries it — that is the resource's own business, same as everywhere else in this module."""
	title = (fields.get("title") or "").strip()
	if title:
		return title[:_TITLE_CAP]
	created = data.get("created_at")
	if created:
		dt = get_datetime(created)
		if dt:
			return f"Note added on {frappe.utils.formatdate(dt, 'd MMM yyyy')}"[:_TITLE_CAP]
	return "Note"


# The ONE output declaration — (public key, source columns, resolve(doc) or None): `_note_view` builds its dict from it and `note_list` selects exactly its columns, so the two cannot drift.
_VIEW_FIELDS = (
	("name",        ("name",), None),
	("external_id", (EXTERNAL_ID_FIELD,), None),
	("lead",        ("reference_doctype", "reference_docname"),
	                lambda doc: doc.reference_docname if doc.reference_doctype == "CRM Lead" else None),
	("title",       ("title",), None),
	("content",     ("content",), None),
	("created_at",  ("creation",), lambda doc: str(doc.get("creation")) if doc.get("creation") else None),
)

# The columns note_list must select — the flattened, deduped union of every _VIEW_FIELDS dependency.
_LIST_COLUMNS = tuple(dict.fromkeys(c for _key, cols, _resolve in _VIEW_FIELDS for c in cols))


def _note_view(doc):
	"""The partner-facing shape of an FCRM Note row — built from _VIEW_FIELDS, the SAME structure
	note_list selects its columns from."""
	return {
		key: (resolve(doc) if resolve else doc.get(cols[0]))
		for key, cols, resolve in _VIEW_FIELDS
	}


def _scoped_note(name, mp, is_sysmgr):
	"""Load an FCRM Note by name, scope-checked through its lead by the shared `scoped_by_lead` brain."""
	if not name:
		throw_field(_(
			"No note was named. Send `name`, the FCRM Note id returned when the note was created; it is "
			"also carried by every row of a note_list response."
		), ["name"])
	doc = frappe.db.exists("FCRM Note", name) and frappe.get_doc("FCRM Note", name)
	if not doc:
		throw_field(_(
			"No note on this API key's line has the id `{0}`. Check the value against a note_list "
			"response for the lead it was created on."
		).format(name), ["name"], frappe.DoesNotExistError)
	lead = doc.reference_docname if doc.reference_doctype == "CRM Lead" else None
	scoped_by_lead(lead, mp, is_sysmgr, "Note")
	return doc


def _apply_fields(doc, data, lead_name):
	"""Overlay the partner payload onto an FCRM Note doc (create or update path).

	Only a field the caller actually sent a VALUE for is written. An empty string is treated as "not
	sent", never as "erase this": a create refuses blank content, so an update must not accept it either,
	or the API would refuse to make a record it is willing to destroy. Many clients serialize an absent
	field as "", and a blanked clinical note is not recoverable.

	`collect` decides WHAT may land and on which column; this decides how. `reference_docname` is the one
	target it never takes from the caller — `lead` is an input to the grain-scoped `resolve_lead`, and
	writing the raw value would attach the note to a lead off the caller's line.

	Returns `fields` (the collected dict) so a caller building a NEW doc (create) can reuse it for the
	mandatory-title fallback without recomputing collect().
	"""
	fields = collect(NOTE_FIELDS, data, "FCRM Note")
	if fields.get("title"):
		doc.title = _derive_title(fields, data)
	# Presence, not truthiness: `collect` already drops a blank string, so this only states that rule.
	if "content" in fields:
		doc.content = fields["content"]  # frappe sanitises Text Editor on save (_sanitize_content)
	if lead_name:
		doc.reference_doctype = "CRM Lead"
		doc.reference_docname = lead_name
	return fields


# -- per-record core (shared by singular + bulk) -----------------------------

def _create_one(data, mp, is_sysmgr):
	"""Create ONE note. Returns (note_view, "created"). A create creates: there is no upsert on a caller
	key, so a re-POST yields a second note. Retries are made safe with Idempotency-Key."""
	content = data.get("content")
	if not content or not str(content).strip():
		throw_field(_(
			"A note with no body is not stored. Send `content` with the note's text; it is an HTML body "
			"and plain text is accepted."
		), ["content"])
	validate_external_id("FCRM Note", data.get("external_id"))

	# A note is never left unattached: unlike a call, an orphan note has no value and a misattached one is a clinical hazard. resolve_lead raises the generic not-found when the lead is off the line.
	lead_name = resolve_lead(mp, is_sysmgr, data)

	doc = frappe.new_doc("FCRM Note")
	# _apply_fields routes every field through collect(NOTE_FIELDS, data), the same path _update_one takes, so a future read_only/hidden spec is honoured on create too.
	fields = _apply_fields(doc, data, lead_name)
	if not doc.title:  # the mandatory-title fallback: a caller with no title concept still gets one
		doc.title = _derive_title(fields, data)
	doc.insert(ignore_permissions=True)  # authz-ok: tier-b — gated by _resolve_caller + resolve_lead, before the save

	stamp_external_id("FCRM Note", doc.name, data.get("external_id"))
	# Backdate creation from created_at (historical load), mirroring the activity + call APIs.
	if data.get("created_at"):
		dt = get_datetime(data.get("created_at"))
		if dt:
			frappe.db.set_value("FCRM Note", doc.name, "creation", dt, update_modified=False)
	return _note_view(frappe.get_doc("FCRM Note", doc.name)), ACTION_CREATED


def _update_one(name, data, mp, is_sysmgr):
	"""Update ONE note by `name`, scope-checked. Only the fields present in the payload change.
	Returns (note_view, "updated")."""
	doc = _scoped_note(name, mp, is_sysmgr)
	validate_external_id("FCRM Note", data.get("external_id"))
	# Re-attribution only when the caller explicitly names a lead; a payload that omits lead/mobile_no leaves the existing link alone.
	lead_name = resolve_lead(mp, is_sysmgr, data) if (data.get("lead") or data.get("mobile_no")) else None
	_apply_fields(doc, data, lead_name)
	doc.save(ignore_permissions=True)  # authz-ok: tier-b — gated by _resolve_caller + _scoped_note, before the save
	if data.get("external_id") is not None:
		stamp_external_id("FCRM Note", doc.name, data.get("external_id"))
	return _note_view(frappe.get_doc("FCRM Note", doc.name)), ACTION_UPDATED


def _delete_one(name, mp, is_sysmgr):
	"""Delete one note by `name`, scope-checked (generic not-found)."""
	doc = _scoped_note(name, mp, is_sysmgr)
	frappe.delete_doc("FCRM Note", doc.name, ignore_permissions=True)  # authz-ok: tier-b — gated by _resolve_caller + _scoped_note


def _read_one(name, mp, is_sysmgr):
	"""Load one note by `name` -> the partner view. The per-record loader both note_get and
	note_get_bulk call, so a single read and a bulk read can never diverge."""
	return _note_view(_scoped_note(name, mp, is_sysmgr))


# -- discovery ---------------------------------------------------------------

@frappe.whitelist(methods=["GET"])
@_api(read=True)
def note_schema(**_kwargs):
	"""Discovery: the note payload contract, every field a caller may send, its type, and whether it is
	required. The shape is fixed (it does not vary by partner or grain), but it is discoverable, so an
	integrator never hardcodes a field list."""
	_resolve_caller()
	_schema_ok(
		"note",
		dedup=(
			"None. Every POST creates a new note and returns a new `name`. Retries are made safe with "
			"the Idempotency-Key header; `external_id` does not deduplicate."
		),
		fields=describe(NOTE_FIELDS, "FCRM Note"),
		attribution=(
			"`lead` or `mobile_no` attaches the note to a lead on the caller's line. A note is never "
			"left unattached and is never matched by guesswork: a payload that names no reachable lead "
			"is refused."
		),
	)


# -- singular endpoints ------------------------------------------------------

@frappe.whitelist(methods=["GET"])
@_api(read=True)
def note_get(**_kwargs):
	"""Read one note by `name`, grain-scoped (own line only). Out-of-scope/missing -> the SAME generic
	not-found."""
	_user, mp, is_sysmgr = _resolve_caller()
	_ok(action=ACTION_FETCHED, data=_read_one(frappe.form_dict.get("name"), mp, is_sysmgr))


@frappe.whitelist(methods=["POST"])
@_api
def note_create(**_kwargs):
	"""Create a note. Body: {lead|mobile_no, external_id?, title?, content, created_at?}. Returns the
	`name` to address the note by from now on."""
	_user, mp, is_sysmgr = _resolve_caller()
	view, action = _create_one(frappe.form_dict, mp, is_sysmgr)
	_ok(action=action, data=view)


@frappe.whitelist(methods=["PUT"])
@_api
def note_update(**_kwargs):
	"""Update a note by `name`. Only the fields present in the body change. Scope-checked."""
	_user, mp, is_sysmgr = _resolve_caller()
	view, action = _update_one(frappe.form_dict.get("name"), frappe.form_dict, mp, is_sysmgr)
	_ok(action=action, data=view)


@frappe.whitelist(methods=["DELETE"])
@_api
def note_delete(**_kwargs):
	"""Delete one note by `name`, scope-checked (own line only). Out-of-scope/missing -> the SAME
	generic not-found."""
	_user, mp, is_sysmgr = _resolve_caller()
	name = frappe.form_dict.get("name")
	_delete_one(name, mp, is_sysmgr)
	_ok(action=ACTION_DELETED, data={"name": name})


# -- bulk / query endpoints --------------------------------------------------

@frappe.whitelist(methods=["POST"])
@_api(bulk=True, read=True)
def note_get_bulk(**_kwargs):
	"""Read many notes by `names` (<= 100). Input-ordered; out-of-scope/unknown names are reported
	not_found in place."""
	_user, mp, is_sysmgr = _resolve_caller()
	names = _read_required_list(frappe.form_dict, "names")
	return _bulk_read(names, lambda name: _read_one(name, mp, is_sysmgr))


@frappe.whitelist(methods=["POST"])
@_api(bulk=True)
def note_create_bulk(**_kwargs):
	"""Create many notes. Body: {"notes":[{...}, ...]} (<= 100). Each record is enforced in its own
	savepoint -> partial success."""
	_user, mp, is_sysmgr = _resolve_caller()
	notes = _read_required_list(frappe.form_dict, "notes")

	def one(i, item):
		view, action = _create_one(item, mp, is_sysmgr)
		return {"index": i, "status": "success", "action": action, "data": view}

	return _run_bulk(notes, one)


@frappe.whitelist(methods=["PUT"])
@_api(bulk=True)
def note_update_bulk(**_kwargs):
	"""Update many notes. Body: {"updates":[{"name":.., ...}, ...]} (<= 100). Partial success."""
	_user, mp, is_sysmgr = _resolve_caller()
	updates = _read_required_list(frappe.form_dict, "updates")

	def one(i, item):
		view, action = _update_one((item or {}).get("name"), item, mp, is_sysmgr)
		return {"index": i, "status": "success", "action": action, "data": view}

	return _run_bulk(updates, one)


@frappe.whitelist(methods=["DELETE"])
@_api(bulk=True)
def note_delete_bulk(**_kwargs):
	"""Delete many notes. Body: {"names":[...]} (<= 100). Partial success."""
	_user, mp, is_sysmgr = _resolve_caller()
	names = _read_required_list(frappe.form_dict, "names")

	def one(i, name):
		_delete_one(name, mp, is_sysmgr)
		return {"index": i, "status": "success", "action": ACTION_DELETED, "data": {"name": name}}

	return _run_bulk(names, one)


@frappe.whitelist(methods=["GET"])
@_api(bulk=True, read=True)
def note_list(**_kwargs):
	"""List a lead's notes, paginated. Query: lead|mobile_no (grain-scoped), limit (<=200, default 20),
	offset."""
	_user, mp, is_sysmgr = _resolve_caller()
	data = frappe.form_dict
	lead = resolve_lead(mp, is_sysmgr, data)

	filters = {"reference_doctype": "CRM Lead", "reference_docname": lead}
	limit, offset = _page(data)
	total = frappe.db.count("FCRM Note", filters)
	rows = frappe.get_all(
		"FCRM Note", filters=filters,
		fields=list(_LIST_COLUMNS),
		limit_page_length=limit, limit_start=offset, order_by="creation desc",
	)
	_list_ok("notes", [_note_view(frappe._dict(r)) for r in rows], total, offset, limit)
