# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""A departing user's separation: their open work moves to new assignees through the list views' own bulk doors, then
their routing and wiring is cleaned up through each record's own save, account last. The map behind every choice here
is docs/investigations/2026-10-15-user-separation-footprint.md; history, `owner` and `modified_by` are never touched."""
from collections import defaultdict

import frappe
from frappe import _
from frappe.utils import cint, create_batch, cstr, getdate, now_datetime
from frappe.utils.background_jobs import get_queue_list, is_job_enqueued

from tatva_connect import bulk_actions, bulk_actions_run, job_feed
from tatva_connect.access import entitlement
from tatva_connect.authoring.lifecycle import ACTIVE, PUBLISHED
from tatva_connect.lead import assignment
from tatva_connect.lead.assignment_rule import on_leave
from tatva_connect.notifications import batch
from tatva_connect.tatva_connect.doctype.crm_list_action_job.crm_list_action_job import BULK_TIMEOUT
from tatva_connect.taxonomy import grain as taxonomy_grain
from tatva_connect.telephony import resolve as seats
from tatva_connect.utils import wait_for_room

DOCTYPE = "Tatva User Separation"
REFRESH = "user_separation_refresh"

# The owner column the CRM list's Edit moves along with the assignee; a record type absent here has none.
OWNER_FIELD = {"CRM Lead": "lead_owner", "CRM Deal": "deal_owner"}
# The status master whose Won / Lost rows close a record held only by its owner column.
_STATUS_MASTER = {"CRM Lead": "CRM Lead Status", "CRM Deal": "CRM Deal Status"}
_CLOSED_TASK = ("Done", "Canceled")


# -- what the user holds ----------------------------------------------------------------------------------------

def held(user):
	"""{doctype: {name}} of the user's open work: an open assignment, an open lead or deal they own, an open task in their name."""
	found = defaultdict(set)
	for t in frappe.get_all("ToDo", filters={"allocated_to": user, "status": "Open", "reference_type": ["is", "set"]},  # authz-ok: tier-b — every caller asked the separation's own permission
	                        fields=["reference_type", "reference_name"]):
		found[t.reference_type].add(cstr(t.reference_name))
	for dt, field in OWNER_FIELD.items():
		found[dt].update(map(cstr, frappe.get_all(dt, filters={field: user, "status": ["not in", _closed(dt)]}, pluck="name")))  # authz-ok: tier-b — gated by the caller
	# cstr: a task name is an int, and its ToDo stores it as text.
	found["CRM Task"].update(map(cstr, frappe.get_all("CRM Task", filters={"assigned_to": user, "status": ["not in", _CLOSED_TASK]}, pluck="name")))  # authz-ok: tier-b — gated by the caller
	_drop_tasks_that_follow(found)
	return {dt: names for dt, names in found.items() if names}


def _closed(doctype):
	"""The statuses whose type is Won or Lost: a record in one is closed, held only by its owner column."""
	return frappe.get_all(_STATUS_MASTER[doctype], filters={"type": ["in", ("Won", "Lost")]}, pluck="name") or [""]  # authz-ok: tier-b — status masters, gated by the caller


def _drop_tasks_that_follow(found):
	"""A task whose lead is moving goes with it (`tasks.on_lead_reassignment_handover`), so it is not work of its own."""
	if not found.get("CRM Task") or not found.get("CRM Lead"):
		return
	following = frappe.get_all("CRM Task", filters={"name": ["in", list(found["CRM Task"])], "reference_doctype": "CRM Lead",  # authz-ok: tier-b — gated by the caller
	                                                "reference_docname": ["in", list(found["CRM Lead"])]}, pluck="name")
	found["CRM Task"] -= set(map(cstr, following))


def groups(user):
	"""{(doctype, grain): [names]} — the user's open work split by the grain each record carries, one read per doctype."""
	out = defaultdict(list)
	for dt, names in held(user).items():
		cols = taxonomy_grain.own_columns(dt)
		wanted = [c for c in cols if c]
		for row in frappe.get_all(dt, filters={"name": ["in", list(names)]}, fields=["name", *wanted]):  # authz-ok: tier-b — gated by the caller
			out[(dt, tuple((row.get(c) or "") if c else "" for c in cols))].append(cstr(row.name))
	return {key: sorted(names) for key, names in out.items()}


def label(grain):
	return " / ".join(axis or "*" for axis in grain)


def _member_of(child, parenttype, user):
	return sorted(set(frappe.get_all(child, filters={"parenttype": parenttype, "user": user}, pluck="parent")))  # authz-ok: tier-b — gated by the caller


def _open_seats(user):
	at = seats.moment()
	return [r for r in frappe.get_all(seats.SEAT_CHILD, filters={"parenttype": seats.AGENT_DOCTYPE, "parent": user},  # authz-ok: tier-b — gated by the caller
	                                  fields=["valid_from", "valid_to"]) if seats.held_at(r, at)]


def _place(user):
	"""Their sales hierarchy node, who it reports to and who reports to it; None when they have none."""
	place = frappe.db.get_value("CRM Sales Hierarchy", {"user": user}, ["name", "reports_to"], as_dict=True)
	if place:
		place.reports = frappe.get_all("CRM Sales Hierarchy", filters={"reports_to": place.name}, pluck="name")  # authz-ok: tier-b — gated by the caller
	return place


def cleanup_counts(user):
	"""{step: count} for each cleanup step that has something to do."""
	place = _place(user)
	counts = {
		"Remove from assignment rules": len(_member_of("Assignment Rule User", "Assignment Rule", user)),
		"Remove from helpdesk teams": len(_member_of("HD Team Member", "HD Team", user)),
		"Deactivate helpdesk agent": frappe.db.count("HD Agent", {"user": user, "is_active": 1}),
		"End telephony extensions": len(_open_seats(user)),
		"Move reports up": len(place.reports) if place else 0,
		"Disable account": frappe.db.count("User", {"name": user, "enabled": 1}),
	}
	return {step: n for step, n in counts.items() if n}


# -- plan and review ----------------------------------------------------------------------------------------------

def plan(doc):
	"""Fill a new separation: one work row per (doctype, grain), assigned to Reassign To, and the cleanup steps."""
	order = {"CRM Lead": 0, "CRM Deal": 1, "HD Ticket": 2}
	for (dt, grain), names in sorted(groups(doc.user).items(), key=lambda kv: (order.get(kv[0][0], 9), kv[0])):
		v, g, p = grain
		_add_row(doc, "work", ref_doctype=dt, grain=label(grain), vertical=v or None, crm_group=g or None,
		         program=p or None, records=len(names), assign_to=doc.reassign_to)
	for step, count in cleanup_counts(doc.user).items():
		_add_row(doc, "steps", step=step, records=count)


def _add_row(doc, table, **values):
	"""A row built by frappe.new_doc, so its schema defaults apply: rows added during validate miss insert's own defaults pass."""
	return doc.append(table, frappe.new_doc(doc.meta.get_field(table).options, parent_doc=doc, parentfield=table, **values))


def _grain(row):
	return (row.vertical or "", row.crm_group or "", row.program or "")


def _share(names, doc, row):
	"""This row's part of its group: what is still open dealt out evenly across the rows not yet done, by position."""
	group = [r for r in doc.work if r.ref_doctype == row.ref_doctype and _grain(r) == _grain(row) and r.status != "Done"]
	return names[group.index(row)::len(group)]


def share_out(doc):
	"""Each row not yet run shows its own share, so a split row counts what it will move, not the whole group."""
	current = groups(doc.user)
	for row in doc.work:
		if row.status == "Pending":
			row.records = len(_share(current.get((row.ref_doctype, _grain(row)), []), doc, row))


def review(doc):
	"""Every reason the separation cannot run yet (red) or should be looked at (orange), as (level, text, fix): fix is where it is put right."""
	found, today = [], getdate()

	def add(level, text, fix=None):  # fix: {"row": idx} | {"form": [doctype, name]} | {"list": [doctype, filters]}
		found.append((level, text, fix))

	people = {row.assign_to for row in doc.work if row.assign_to}
	enabled = set(frappe.get_all("User", filters={"name": ["in", list(people) or [""]], "enabled": 1}, pluck="name"))  # authz-ok: tier-b — gated by the caller
	for row in doc.work:
		if row.status == "Done":
			continue
		where, who, fix = _("Row {0} ({1}, {2})").format(row.idx, _(row.ref_doctype), row.grain), row.assign_to, {"row": row.idx}
		if not who:
			add("red", _("{0}: pick who it is assigned to.").format(where), fix)
		elif who == doc.user or who not in enabled:
			add("red", _("{0}: {1} cannot take it over; pick an active, different user.").format(where, who), fix)
		elif not frappe.has_permission(row.ref_doctype, "write", user=who):
			add("red", _("{0}: {1} cannot edit {2}.").format(where, who, _(row.ref_doctype)), fix)
		elif not entitlement.grain_entitled(_grain(row), user=who):
			add("red", _("{0}: {1} has no access to {2}.").format(where, who, row.grain), fix)
		elif on_leave(who, today):
			add("orange", _("{0}: {1} is on leave today.").format(where, who), fix)
	planned = {(row.ref_doctype, _grain(row)) for row in doc.work}
	for dt, grain in groups(doc.user):
		if (dt, grain) not in planned:
			add("red", _("{0} in {1} have no row; start a new separation to plan them.").format(_(dt), label(grain)))
	for flow, step in _workflows_naming(doc.user):
		add("red", _("Workflow {0}: its {1} step assigns work to this user; re-point it first.").format(flow, _(step)), {"form": ["CRM Workflow", flow]})
	for parent_doctype, name in _left_empty(doc.user):
		add("red", _("{0} {1} would have nobody left; add someone to it first, or what it routes lands on no one.").format(_(parent_doctype), name),
		    {"form": [parent_doctype, name]})
	place = _place(doc.user)
	if place and place.reports and not place.reports_to:
		add("orange", _("{0} people report to this user and nobody is above them, so they would keep reporting to a disabled user; set their new manager.").format(len(place.reports)),
		    {"list": ["CRM Sales Hierarchy", {"reports_to": place.name}]})
	for dt, field in OWNER_FIELD.items():
		closed = {field: doc.user, "status": ["in", _closed(dt)]}
		if owned := frappe.db.count(dt, closed):
			add("orange", _("{0} closed {1} stay owned by this user (history is not moved); a workflow that assigns to the owner would fail on them.").format(owned, _(dt)),
			    {"list": [dt, closed]})
	if frappe.db.exists("CRM Lead API Mapping", {"partner_user": doc.user}):
		add("red", _("This is a partner API login; it is retired from its API mapping, not separated."), {"list": ["CRM Lead API Mapping", {"partner_user": doc.user}]})
	return found


def _workflows_naming(user):
	"""(workflow, step type) of every live workflow step that names the user as its assignee — Assign to User, Create Task, any step with the control."""
	live = frappe.get_all("CRM Workflow", filters={"lifecycle_state": ["in", (PUBLISHED, ACTIVE)]}, pluck="name")  # authz-ok: tier-b — gated by the caller
	if not live:
		return []
	nodes = frappe.get_all("CRM Workflow Node", filters={"workflow": ["in", live]}, fields=["workflow", "node_type", "config_json"])  # authz-ok: tier-b — gated by the caller
	return sorted({(n.workflow, n.node_type) for n in nodes if (frappe.parse_json(n.config_json or "{}") or {}).get("assign_to_user") == user})


def _left_empty(user):
	"""(doctype, name) of each enabled assignment rule and helpdesk team the user is in that would have no other member left."""
	empty = []
	for parent_doctype, child, live in (("Assignment Rule", "Assignment Rule User", {"disabled": 0}), ("HD Team", "HD Team Member", {})):
		parents = frappe.get_all(parent_doctype, filters={"name": ["in", _member_of(child, parent_doctype, user) or [""]], **live}, pluck="name")  # authz-ok: tier-b — gated by the caller
		others = {r.parent for r in frappe.get_all(child, filters={"parenttype": parent_doctype, "parent": ["in", parents or [""]], "user": ["!=", user]},  # authz-ok: tier-b — gated by the caller
		                                          fields=["parent"])}
		empty += [(parent_doctype, name) for name in parents if name not in others]
	return empty


# -- run ----------------------------------------------------------------------------------------------------------

@frappe.whitelist(methods=["POST"])
def run(separation, rows=None):
	"""Queue the picked work rows, or every row not yet done and then the cleanup, on ONE long-lane job run as the caller."""
	doc = frappe.get_doc(DOCTYPE, separation)
	doc.check_permission("write")
	if doc.status == "Running" and not stranded(doc):
		frappe.throw(_("This separation is already running."))
	blocking = [text for level, text, _fix in review(doc) if level == "red"]
	if blocking:
		frappe.throw("<br>".join(blocking), title=_("Fix these first"))
	picked = set(frappe.parse_json(rows) or [])
	for row in doc.work:
		if row.status != "Done" and (not picked or row.name in picked):
			row.status, row.result = "Running", None
	if not picked:
		for step in doc.steps:
			if step.status != "Done":
				step.status, step.result = "Running", None
	doc.status = "Running"
	doc.save()
	batches = sum(-(-(row.records or 1) // bulk_actions.MAX_ROWS) for row in doc.work if row.status == "Running")
	frappe.enqueue(f"{__name__}.work", queue="long", timeout=BULK_TIMEOUT * max(batches, 1), enqueue_after_commit=True,
	               job_id=job_feed.rq_key(DOCTYPE, separation), deduplicate=True, separation=separation)


def stranded(doc):
	"""A run whose job RQ no longer holds, queued or running: its worker is gone, so the record is free to Retry."""
	return doc.status == "Running" and not is_job_enqueued(job_feed.rq_key(DOCTYPE, doc.name))


def work(separation):
	"""The worker: the running work rows, then — only when every work row is Done — the cleanup, account last; one bell at the end."""
	doc = frappe.get_doc(DOCTYPE, separation)
	doc.flags.separation_worker = True  # its own saves pass the hold a run puts on the record
	current = groups(doc.user)
	for row in [r for r in doc.work if r.status == "Running"]:
		_land(doc, row, *_safely(lambda row=row: _hand_over(doc, row, current)))
	unplanned = _still_open(doc)
	all_done = all(r.status == "Done" for r in doc.work) and not unplanned
	waits = _("Waits: open work arrived with no row ({0}); start a new separation for it.").format(", ".join(unplanned)) if unplanned \
		else _("Waits until every work row is done.")
	for step in [s for s in doc.steps if s.status == "Running"]:
		_land(doc, step, *(_safely(lambda step=step: _STEPS[step.step](doc.user)) if all_done else ("Pending", waits)))
	rows = [*doc.work, *doc.steps]
	failed = any(r.status in ("Failed", "Partly done") for r in rows)
	finished = all(r.status in ("Done", "Skipped") for r in rows)
	doc.status = "Completed" if finished else ("Completed with Errors" if failed else "Draft")
	doc.save()  # a normal save, so the finish is in the record's Version history like every other change
	frappe.db.commit()
	_notify(doc, failed)
	frappe.publish_realtime(REFRESH, {"separation": doc.name}, user=frappe.session.user)


def _still_open(doc):
	"""Before the cleanup, re-read what the user still holds: a done group with records left reopens its last row; a group with no row is returned."""
	unplanned = []
	for (dt, grain), names in groups(doc.user).items():
		rows = [r for r in doc.work if r.ref_doctype == dt and _grain(r) == grain]
		if not rows:
			unplanned.append(f"{_(dt)} {label(grain)}")
		elif all(r.status == "Done" for r in rows):
			rows[-1].status, rows[-1].result = "Partly done", _("{0} still open on this user (refused, or arrived during the run); Retry moves them.").format(len(names))
	return unplanned


def _safely(act):
	"""One row's outcome; a failure is logged and shown on the row, never stopping the rows after it."""
	try:
		return act()
	except Exception as e:
		frappe.db.rollback()
		frappe.log_error("user separation: a row failed")
		return "Failed", bulk_actions_run._why(e)


def _land(doc, row, status, result):
	"""Write one row's outcome through the record's own save, so its Version history shows who, when and what."""
	row.status, row.result = status, result
	doc.save()
	frappe.db.commit()
	frappe.publish_realtime(REFRESH, {"separation": doc.name}, user=frappe.session.user)


def _hand_over(doc, row, current):
	"""This row's share of its group, moved through the list view's own bulk door in batches."""
	names = _share(current.get((row.ref_doctype, _grain(row)), []), doc, row)
	if not names:
		return "Done", _("Nothing left here; it moved with its lead or was closed.")
	moved, refused, why = 0, 0, {}
	with batch.holding(doc.doctype, doc.name):  # the new holder is told once per wave, the departing one never
		for rows in create_batch(names, bulk_actions.MAX_ROWS):
			wait_for_room(*get_queue_list())  # each batch queues notifications and index work on the other lanes
			out = _DOORS.get(row.ref_doctype, _reassign)(row.ref_doctype, rows, row.assign_to, doc.user)
			moved, refused = moved + out["succeeded"], refused + out["failed"]
			why.update(out.get("reasons") or {})
	return _outcome(moved, refused, why.values())


def _reassign(doctype, names, to, departing):
	"""The CRM list's Reassign, then its Edit of the owner column for the records the departing user owns."""
	out = bulk_actions_run.run_reassign(doctype, names, {"assign_to": [to]})
	if field := OWNER_FIELD.get(doctype):
		owned = frappe.get_all(doctype, filters={"name": ["in", names], field: departing}, pluck="name")  # authz-ok: tier-b — gated by the caller
		if owned:
			bulk_actions_run.run_bulk_edit(doctype, owned, {"field": field, "value": to})
	return out


def _helpdesk_assign(doctype, names, to, departing):
	"""Each ticket the runner may read to the new agent through the one assignment door, then off the departing agent, silently."""
	def hand_over(name):
		bulk_actions_run._assign_row(doctype, name, [to])  # asks the runner's read, as Assign does; counted into the hand-over's wave
		assignment.unassign(doctype, name, departing, notify=False)

	why = {}
	succeeded, failed = bulk_actions_run._per_row(doctype, names, hand_over, "separation hand-over", why=why)
	return bulk_actions_run._summary(names, succeeded, failed, why)


_DOORS = {"HD Ticket": _helpdesk_assign}


# -- cleanup, each through the record's own save --------------------------------------------------------------------

def _drop_member(parent_doctype, child_doctype, user):
	"""Take the user out of every grid of `child_doctype` on each parent that lists them; the parent stays as it is otherwise."""
	parents = _member_of(child_doctype, parent_doctype, user)
	for name in parents:
		parent = frappe.get_doc(parent_doctype, name)
		for df in parent.meta.get_table_fields():
			if df.options == child_doctype:
				parent.set(df.fieldname, [r for r in parent.get(df.fieldname) if r.user != user])
		parent.save()
	return "Done", _("Removed from {0}.").format(", ".join(parents)) if parents else _("Nothing to remove.")


def _deactivate_agent(user):
	agent = frappe.get_doc("HD Agent", {"user": user})
	agent.is_active = 0
	agent.save()
	return "Done", _("Deactivated.")


def _end_extensions(user):
	agent, now = frappe.get_doc(seats.AGENT_DOCTYPE, {"user": user}), now_datetime()
	ended = [seat for seat in agent.get(seats.SEAT_FIELD) or [] if seats.held_at(seat, now)]
	for seat in ended:
		seat.valid_to = now
	agent.save()
	return "Done", _("{0} ended.").format(len(ended))


def _move_reports_up(user):
	place = _place(user)
	if not place.reports_to:
		return "Skipped", _("Nobody is above them, so their reports stay where they are.")
	for name in place.reports:
		report = frappe.get_doc("CRM Sales Hierarchy", name)
		report.reports_to = place.reports_to
		report.save()
	return "Done", _("{0} moved up.").format(len(place.reports))


def _disable_account(user):
	account = frappe.get_doc("User", user)
	account.enabled = 0
	account.save()
	return "Done", _("Disabled.")


_STEPS = {
	"Remove from assignment rules": lambda user: _drop_member("Assignment Rule", "Assignment Rule User", user),
	"Remove from helpdesk teams": lambda user: _drop_member("HD Team", "HD Team Member", user),
	"Deactivate helpdesk agent": _deactivate_agent,
	"End telephony extensions": _end_extensions,
	"Move reports up": _move_reports_up,
	"Disable account": _disable_account,
}


def _outcome(done, refused, reasons):
	status = "Done" if not refused else ("Failed" if not done else "Partly done")
	reasons = list(dict.fromkeys(r for r in reasons if r))[:3]
	return status, " ".join([_("{0} moved, {1} refused.").format(done, refused), *reasons])


def _notify(doc, failed):
	"""One Desk bell for the person who ran it."""
	from frappe.desk.doctype.notification_log.notification_log import enqueue_create_notification

	subject = _("Separation of {0} needs another look.") if failed else _("Separation of {0} finished.")
	enqueue_create_notification(frappe.session.user, {"type": "Alert", "subject": subject.format(doc.full_name or doc.user),
	                                                  "document_type": DOCTYPE, "document_name": doc.name})


@frappe.whitelist()
@frappe.validate_and_sanitize_search_inputs
def assignee_query(doctype, txt, searchfield, start, page_len, filters):
	"""Assign To's picker: active users who may edit the row's record type and are entitled to its grain."""
	frappe.has_permission(DOCTYPE, "write", throw=True)
	grain = (filters.get("vertical") or "", filters.get("crm_group") or "", filters.get("program") or "")
	users = [u for u in entitlement.users_entitled_to(grain, txt=txt, limit=50)
	         if frappe.has_permission(filters.get("ref_doctype"), "write", user=u) and entitlement.grain_entitled(grain, user=u)]
	start = cint(start)
	return entitlement.link_rows(users[start:start + cint(page_len)])
