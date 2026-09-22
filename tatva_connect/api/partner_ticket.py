"""Gated partner TICKET API: HD Ticket and HD Ticket Comment on the same gate, envelope, limiter, idempotency, bulk and list engines as every partner resource."""
import frappe
from frappe import _

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
	_order_by,
	_page,
	_resolve_caller,
	_run_bulk,
	_schema_ok,
	grain_fence,
	not_found_message,
	read_bulk_list,
	resolve_lead,
	stamp_external_id,
	throw_field,
	trusted_permissions,
	validate_external_id,
)
from tatva_connect.api.field_spec import FieldSpec, collect, describe
from tatva_connect.api.partner import _allowed_programs
from tatva_connect.helpdesk.contact import contact_for_phone
from tatva_connect.taxonomy import grain
from tatva_connect.taxonomy.program_mode import resolve_program

TICKET = "HD Ticket"
COMMENT = "HD Ticket Comment"

# The ticket's payload contract, declared ONCE: `describe` advertises it, `collect` ingests it. Target-less specs are resolved here.
TICKET_FIELDS = (
	FieldSpec("subject",      "Subject",      "subject", required=True),
	FieldSpec("description",  "Description",  "description"),
	FieldSpec("mobile_no",    "Mobile No",    required=True),
	FieldSpec("contact_name", "Contact Name"),
	FieldSpec("email",        "Email",        "raised_by"),
	FieldSpec("lead",         "Lead"),
	FieldSpec("program",      "Program"),
	FieldSpec("ticket_type",  "Ticket Type",  "ticket_type"),
	FieldSpec("priority",     "Priority",     "priority"),
	FieldSpec("status",       "Status",       "status"),
	FieldSpec("agent_group",  "Team",         "agent_group"),
	FieldSpec("external_id",  "External ID",  EXTERNAL_ID_FIELD),
)

COMMENT_FIELDS = (
	FieldSpec("ticket",      "Ticket",      required=True),
	FieldSpec("content",     "Content",     "content", required=True),
	FieldSpec("is_pinned",   "Pinned",      "is_pinned"),
	FieldSpec("external_id", "External ID", EXTERNAL_ID_FIELD),
)


def _created_at(doc):
	return str(doc.get("creation")) if doc.get("creation") else None


# The ONE output declaration per resource — (public key, source columns, resolve(doc) or None): the view builds from it and the list selects exactly its columns.
_TICKET_VIEW_FIELDS = (
	("name",        ("name",), None),
	("external_id", (EXTERNAL_ID_FIELD,), None),
	("subject",     ("subject",), None),
	("description", ("description",), None),
	("status",      ("status",), None),
	("priority",    ("priority",), None),
	("ticket_type", ("ticket_type",), None),
	("agent_group", ("agent_group",), None),
	("contact",     ("contact",), None),
	("email",       ("raised_by",), None),
	("lead",        ("custom_lead",), None),
	("program",     ("custom_current_program",), None),
	("created_at",  ("creation",), _created_at),
)
_COMMENT_VIEW_FIELDS = (
	("name",         ("name",), None),
	("external_id",  (EXTERNAL_ID_FIELD,), None),
	("ticket",       ("reference_ticket",), None),
	("content",      ("content",), None),
	("is_pinned",    ("is_pinned",), None),
	("commented_by", ("commented_by",), None),
	("created_at",   ("creation",), _created_at),
)

_TICKET_LIST_COLUMNS = tuple(dict.fromkeys(c for _key, cols, _resolve in _TICKET_VIEW_FIELDS for c in cols))
_COMMENT_LIST_COLUMNS = tuple(dict.fromkeys(c for _key, cols, _resolve in _COMMENT_VIEW_FIELDS for c in cols))

# The ticket list's optional filters: public key -> column. Anything else in the query is ignored, never interpreted.
_TICKET_FILTERS = {"status": "status", "priority": "priority", "ticket_type": "ticket_type", "agent_group": "agent_group"}


def _project(doc, view_fields):
	"""A row in the partner-facing shape its view declares."""
	return {key: (resolve(doc) if resolve else doc.get(cols[0])) for key, cols, resolve in view_fields}


def _ticket_view(doc):
	return _project(doc, _TICKET_VIEW_FIELDS)


def _comment_view(doc):
	return _project(doc, _COMMENT_VIEW_FIELDS)


# -- tickets -----------------------------------------------------------------

def _on_line(ticket, mp):
	"""The ONE visibility rule for this module: a ticket, and every comment on it, is reachable iff the ticket is on the caller's line."""
	return bool(ticket) and bool(frappe.db.exists(TICKET, {"name": ticket, **grain_fence(mp, TICKET)}))


def _scoped_ticket(name, mp):
	"""Load an HD Ticket by name inside the caller's grain fence; missing and out-of-line answer the same."""
	if not name:
		throw_field(_("No ticket was named. Send `name`, the ticket id returned when it was created."), ["name"])
	if not _on_line(name, mp):
		throw_field(not_found_message("ticket", hint=_("Check the value against a ticket_list response.")),
		            ["name"], frappe.DoesNotExistError)
	return frappe.get_doc(TICKET, name)


def _program(data, mp):
	"""The ticket's program by the key's mode — the SAME resolver lead create and the enrolment form use."""
	if not mp:
		return data.get("program")
	return resolve_program(
		mp.program, _allowed_programs(frappe.session.user, True), data.get("program"),
		field_label="program", source_label="key", optional=bool(mp.get("program_optional")),
	)


def _apply_ticket(doc, data, mp, is_sysmgr, creating=False):
	"""Overlay a payload on a ticket: declared columns via `collect`, then contact, lead and program resolved through their shared brains."""
	doc.update(collect(TICKET_FIELDS, data, TICKET, creating=creating))
	if data.get("mobile_no"):
		with trusted_permissions():  # authz-ok: caller pre-gated by _resolve_caller (mapping+grain)
			doc.contact = contact_for_phone(data["mobile_no"], data.get("contact_name"), data.get("email"))
	if data.get("lead"):
		doc.custom_lead = resolve_lead(mp, is_sysmgr, {"lead": data["lead"]})
	if creating or data.get("program"):
		doc.set(grain.columns(TICKET)[grain.AXES.index("program")], _program(data, mp))


def _create_ticket(data, mp, is_sysmgr):
	"""Create ONE ticket in the caller's line. A create creates; retries are made safe with Idempotency-Key."""
	validate_external_id(TICKET, data.get("external_id"))
	doc = frappe.new_doc(TICKET)
	doc.update(grain_fence(mp, TICKET))
	doc.via_customer_portal = 1  # the partner is a portal caller, exactly as helpdesk's own `new` marks one
	_apply_ticket(doc, data, mp, is_sysmgr, creating=True)
	doc.insert(ignore_permissions=True)  # authz-ok: tier-b — gated by _resolve_caller, grain stamped from the mapping
	stamp_external_id(TICKET, doc.name, data.get("external_id"))
	return _ticket_view(frappe.get_doc(TICKET, doc.name)), ACTION_CREATED


def _update_ticket(name, data, mp, is_sysmgr):
	"""Update ONE ticket by `name`; only the fields sent change."""
	doc = _scoped_ticket(name, mp)
	validate_external_id(TICKET, data.get("external_id"))
	_apply_ticket(doc, data, mp, is_sysmgr)
	doc.save(ignore_permissions=True)  # authz-ok: tier-b — gated by _resolve_caller + _scoped_ticket
	if data.get("external_id") is not None:
		stamp_external_id(TICKET, doc.name, data.get("external_id"))
	return _ticket_view(frappe.get_doc(TICKET, doc.name)), ACTION_UPDATED


def _delete_ticket(name, mp):
	frappe.delete_doc(TICKET, _scoped_ticket(name, mp).name, ignore_permissions=True)  # authz-ok: tier-b — gated by _resolve_caller + _scoped_ticket


def _read_ticket(name, mp):
	return _ticket_view(_scoped_ticket(name, mp))


@frappe.whitelist(methods=["GET"])
@_api(read=True)
def ticket_schema(**_kwargs):
	"""Discovery: every ticket field a caller may send, its type, allowed values and whether it is required."""
	_resolve_caller()
	_schema_ok(
		"ticket",
		dedup=_("None. Every POST creates a new ticket and returns a new `name`. Retries are made safe with the "
		        "Idempotency-Key header; `external_id` does not deduplicate."),
		fields=describe(TICKET_FIELDS, TICKET),
		attribution=_("`mobile_no` names the person: the contact holding that number, or a new one created with it. "
		              "The ticket lands on the caller's line; `lead` links a lead on that line."),
	)


@frappe.whitelist(methods=["GET"])
@_api(read=True)
def ticket_get(**_kwargs):
	"""Read one ticket by `name`, own line only."""
	_user, mp, _is_sysmgr = _resolve_caller()
	_ok(action=ACTION_FETCHED, data=_read_ticket(frappe.form_dict.get("name"), mp))


@frappe.whitelist(methods=["POST"])
@_api
def ticket_create(**_kwargs):
	"""Create a ticket; returns the `name` to address it by."""
	_user, mp, is_sysmgr = _resolve_caller()
	view, action = _create_ticket(frappe.form_dict, mp, is_sysmgr)
	_ok(action=action, data=view)


@frappe.whitelist(methods=["PUT"])
@_api
def ticket_update(**_kwargs):
	"""Update a ticket by `name`; only the fields sent change."""
	_user, mp, is_sysmgr = _resolve_caller()
	view, action = _update_ticket(frappe.form_dict.get("name"), frappe.form_dict, mp, is_sysmgr)
	_ok(action=action, data=view)


@frappe.whitelist(methods=["DELETE"])
@_api
def ticket_delete(**_kwargs):
	"""Delete a ticket by `name`, own line only."""
	_user, mp, _is_sysmgr = _resolve_caller()
	name = frappe.form_dict.get("name")
	_delete_ticket(name, mp)
	_ok(action=ACTION_DELETED, data={"name": name})


@frappe.whitelist(methods=["POST"])
@_api(bulk=True, read=True)
def ticket_get_bulk(**_kwargs):
	"""Read many tickets by `names`; unreachable names are reported not_found in place."""
	_user, mp, _is_sysmgr = _resolve_caller()
	return _bulk_read(read_bulk_list("ticket", "get"), lambda name: _read_ticket(name, mp))


@frappe.whitelist(methods=["POST"])
@_api(bulk=True)
def ticket_create_bulk(**_kwargs):
	"""Create many tickets, each in its own savepoint: partial success."""
	_user, mp, is_sysmgr = _resolve_caller()

	def one(i, item):
		view, action = _create_ticket(item, mp, is_sysmgr)
		return {"index": i, "status": "success", "action": action, "data": view}

	return _run_bulk(read_bulk_list("ticket", "create"), one)


@frappe.whitelist(methods=["PUT"])
@_api(bulk=True)
def ticket_update_bulk(**_kwargs):
	"""Update many tickets by `name`: partial success."""
	_user, mp, is_sysmgr = _resolve_caller()

	def one(i, item):
		view, action = _update_ticket((item or {}).get("name"), item, mp, is_sysmgr)
		return {"index": i, "status": "success", "action": action, "data": view}

	return _run_bulk(read_bulk_list("ticket", "update"), one)


@frappe.whitelist(methods=["DELETE"])
@_api(bulk=True)
def ticket_delete_bulk(**_kwargs):
	"""Delete many tickets by `names`: partial success."""
	_user, mp, _is_sysmgr = _resolve_caller()

	def one(i, name):
		_delete_ticket(name, mp)
		return {"index": i, "status": "success", "action": ACTION_DELETED, "data": {"name": name}}

	return _run_bulk(read_bulk_list("ticket", "delete"), one)


@frappe.whitelist(methods=["GET"])
@_api(bulk=True, read=True)
def ticket_list(**_kwargs):
	"""The caller's tickets, newest first, paginated. Optional: status, priority, ticket_type, agent_group, lead."""
	_user, mp, is_sysmgr = _resolve_caller()
	data = frappe.form_dict
	filters = {**grain_fence(mp, TICKET), **{col: data[key] for key, col in _TICKET_FILTERS.items() if data.get(key)}}
	if data.get("lead"):
		filters["custom_lead"] = resolve_lead(mp, is_sysmgr, {"lead": data["lead"]})
	limit, offset = _page(data)
	total = frappe.db.count(TICKET, filters)
	rows = frappe.get_all(TICKET, filters=filters, fields=list(_TICKET_LIST_COLUMNS),
	                      limit_page_length=limit, limit_start=offset, order_by=_order_by("creation"))
	_list_ok("tickets", [_ticket_view(frappe._dict(r)) for r in rows], total, offset, limit)


# -- comments ----------------------------------------------------------------

def _scoped_comment(name, mp):
	"""Load a comment by name; visible iff its ticket is on the caller's line."""
	if not name:
		throw_field(_("No comment was named. Send `name`, the comment id returned when it was created."), ["name"])
	if not _on_line(frappe.db.get_value(COMMENT, name, "reference_ticket"), mp):
		throw_field(not_found_message("comment", hint=_("Check the value against a comment_list response.")),
		            ["name"], frappe.DoesNotExistError)
	return frappe.get_doc(COMMENT, name)


def _create_comment(data, mp):
	"""Add ONE comment to a ticket on the caller's line, authored by the calling key."""
	validate_external_id(COMMENT, data.get("external_id"))
	doc = frappe.new_doc(COMMENT)
	doc.update(collect(COMMENT_FIELDS, data, COMMENT, creating=True))
	doc.reference_ticket = _scoped_ticket(data.get("ticket"), mp).name
	doc.commented_by = frappe.session.user
	doc.insert(ignore_permissions=True)  # authz-ok: tier-b — gated by _resolve_caller + _scoped_ticket
	stamp_external_id(COMMENT, doc.name, data.get("external_id"))
	return _comment_view(frappe.get_doc(COMMENT, doc.name)), ACTION_CREATED


def _update_comment(name, data, mp):
	"""Update ONE comment by `name`; only the fields sent change and it never moves ticket."""
	doc = _scoped_comment(name, mp)
	validate_external_id(COMMENT, data.get("external_id"))
	doc.update(collect(COMMENT_FIELDS, data, COMMENT))
	doc.save(ignore_permissions=True)  # authz-ok: tier-b — gated by _resolve_caller + _scoped_comment
	if data.get("external_id") is not None:
		stamp_external_id(COMMENT, doc.name, data.get("external_id"))
	return _comment_view(frappe.get_doc(COMMENT, doc.name)), ACTION_UPDATED


def _delete_comment(name, mp):
	frappe.delete_doc(COMMENT, _scoped_comment(name, mp).name, ignore_permissions=True)  # authz-ok: tier-b — gated by _resolve_caller + _scoped_comment


def _read_comment(name, mp):
	return _comment_view(_scoped_comment(name, mp))


@frappe.whitelist(methods=["GET"])
@_api(read=True)
def comment_schema(**_kwargs):
	"""Discovery: every comment field a caller may send."""
	_resolve_caller()
	_schema_ok(
		"comment",
		dedup=_("None. Every POST adds a new comment and returns a new `name`. Retries are made safe with the "
		        "Idempotency-Key header."),
		fields=describe(COMMENT_FIELDS, COMMENT),
		attribution=_("`ticket` names a ticket on the caller's line; the comment is authored by the calling key."),
	)


@frappe.whitelist(methods=["GET"])
@_api(read=True)
def comment_get(**_kwargs):
	"""Read one comment by `name`, own line only."""
	_user, mp, _is_sysmgr = _resolve_caller()
	_ok(action=ACTION_FETCHED, data=_read_comment(frappe.form_dict.get("name"), mp))


@frappe.whitelist(methods=["POST"])
@_api
def comment_create(**_kwargs):
	"""Add a comment to a ticket; returns its `name`."""
	_user, mp, _is_sysmgr = _resolve_caller()
	view, action = _create_comment(frappe.form_dict, mp)
	_ok(action=action, data=view)


@frappe.whitelist(methods=["PUT"])
@_api
def comment_update(**_kwargs):
	"""Update a comment by `name`."""
	_user, mp, _is_sysmgr = _resolve_caller()
	view, action = _update_comment(frappe.form_dict.get("name"), frappe.form_dict, mp)
	_ok(action=action, data=view)


@frappe.whitelist(methods=["DELETE"])
@_api
def comment_delete(**_kwargs):
	"""Delete a comment by `name`, own line only."""
	_user, mp, _is_sysmgr = _resolve_caller()
	name = frappe.form_dict.get("name")
	_delete_comment(name, mp)
	_ok(action=ACTION_DELETED, data={"name": name})


@frappe.whitelist(methods=["POST"])
@_api(bulk=True, read=True)
def comment_get_bulk(**_kwargs):
	"""Read many comments by `names`."""
	_user, mp, _is_sysmgr = _resolve_caller()
	return _bulk_read(read_bulk_list("comment", "get"), lambda name: _read_comment(name, mp))


@frappe.whitelist(methods=["POST"])
@_api(bulk=True)
def comment_create_bulk(**_kwargs):
	"""Add many comments: partial success."""
	_user, mp, _is_sysmgr = _resolve_caller()

	def one(i, item):
		view, action = _create_comment(item, mp)
		return {"index": i, "status": "success", "action": action, "data": view}

	return _run_bulk(read_bulk_list("comment", "create"), one)


@frappe.whitelist(methods=["PUT"])
@_api(bulk=True)
def comment_update_bulk(**_kwargs):
	"""Update many comments by `name`: partial success."""
	_user, mp, _is_sysmgr = _resolve_caller()

	def one(i, item):
		view, action = _update_comment((item or {}).get("name"), item, mp)
		return {"index": i, "status": "success", "action": action, "data": view}

	return _run_bulk(read_bulk_list("comment", "update"), one)


@frappe.whitelist(methods=["DELETE"])
@_api(bulk=True)
def comment_delete_bulk(**_kwargs):
	"""Delete many comments by `names`: partial success."""
	_user, mp, _is_sysmgr = _resolve_caller()

	def one(i, name):
		_delete_comment(name, mp)
		return {"index": i, "status": "success", "action": ACTION_DELETED, "data": {"name": name}}

	return _run_bulk(read_bulk_list("comment", "delete"), one)


@frappe.whitelist(methods=["GET"])
@_api(bulk=True, read=True)
def comment_list(**_kwargs):
	"""A ticket's comments, newest first, paginated. Query: ticket (own line only)."""
	_user, mp, _is_sysmgr = _resolve_caller()
	data = frappe.form_dict
	filters = {"reference_ticket": _scoped_ticket(data.get("ticket"), mp).name}
	limit, offset = _page(data)
	total = frappe.db.count(COMMENT, filters)
	rows = frappe.get_all(COMMENT, filters=filters, fields=list(_COMMENT_LIST_COLUMNS),
	                      limit_page_length=limit, limit_start=offset, order_by=_order_by("creation"))
	_list_ok("comments", [_comment_view(frappe._dict(r)) for r in rows], total, offset, limit)
