# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""Check-in: a person's status is their latest `Tatva User Checkin` row, read and written only here."""
import frappe
from frappe.utils import cint, get_datetime, get_time, now_datetime

from tatva_connect.access import entitlement, ledger
from tatva_connect.automation import settings as automation

DOCTYPE = "Tatva User Checkin"
# Helpdesk's three `HD Agent Status` categories; the doctype's Select carries the same words.
ACTIVE, AWAY, UNAVAILABLE = "Active", "Away", "Unavailable"
SELF, MANAGER, SHIFT_END, HELPDESK = "Self", "Manager", "Shift End", "Helpdesk"
# The socket event that tells a rep's tabs their check-in card changed; it carries nothing, each tab re-reads get_status.
CARD_EVENT = "tatva_checkin"
HELPDESK_MIRROR = "Lead::Checkin::helpdesk"
MIDNIGHT_CHECKOUT = "Lead::Checkin::midnight"

# The runtime cron jobs below are found again by this method and are owned by this doctype.
CLOSE_JOB = "tatva_connect.lead.checkin.close_ended_shifts"
OPEN_JOB = "tatva_connect.lead.routing.open_shifts"
SHIFT_DT = "Tatva Work Shift"


def latest(user):
	"""The user's newest row as `{name, status, source, owner, creation}`, or None when they never checked in."""
	return frappe.db.get_value(
		DOCTYPE, {"user": user}, ["name", "status", "source", "owner", "creation"], as_dict=True, order_by="creation desc"
	)


def status_of(user):
	row = latest(user)
	return row.status if row else None


def is_active(user):
	return status_of(user) == ACTIVE


def statuses(users):
	"""`{user: status}` from each user's latest row: `status_of` for many people in one query; a user who never checked in is absent."""
	if not users:
		return {}
	from frappe.query_builder.functions import Max

	row = frappe.qb.DocType(DOCTYPE)
	newest = frappe.qb.from_(row).select(row.user, Max(row.creation).as_("at")).where(row.user.isin(list(users))).groupby(row.user).as_("newest")
	latest_rows = (
		frappe.qb.from_(row).join(newest).on((row.user == newest.user) & (row.creation == newest.at))
		.select(row.user, row.status).run(as_dict=True)
	)
	return {r.user: r.status for r in latest_rows}


def active_users(users):
	"""Of `users`, those whose latest row is Active: `is_active` for many people in one query."""
	return {user for user, status in statuses(users).items() if status == ACTIVE}


def record(user, status, source=None):
	"""Append a row unless it repeats the current status; no `source` is a person acting under their own permission, a source is the engine's."""
	if status_of(user) == status:
		return None
	row = frappe.get_doc({"doctype": DOCTYPE, "user": user, "status": status})
	row.flags.checkin_source = source
	row.insert(ignore_permissions=bool(source))  # authz-ok: tier-a — only the shift-end job and the Helpdesk mirror pass a source
	tell_card(user)
	return row


def tell_card(user):
	"""Ask the user's open tabs to re-read their check-in card: one empty event per user per transaction, sent after commit, dropped on rollback."""
	from frappe.realtime import get_user_room

	# The room is named, since inside a job with a task id `user=` would go to the task's room instead.
	frappe.publish_realtime(CARD_EVENT, room=get_user_room(user), after_commit=True)


def on_outbound_call(call, method=None):
	"""CRM Call Log after_insert / on_update: an outbound call on a lead counts as working it, so it may lift the caller's untouched-limit pause."""
	if call.type == "Outgoing" and call.reference_doctype == "CRM Lead" and call.caller:
		tell_card(call.caller)


def on_pool_assignment(todo, method=None):
	"""ToDo after_insert / on_update: a pick, hand-off or removal from a pool that reads check-in or an untouched limit changes the holder's card."""
	if not (todo.assignment_rule and todo.allocated_to and todo.reference_type == "CRM Lead"):
		return
	if any(frappe.get_cached_value("Assignment Rule", todo.assignment_rule, ["require_checkin", "max_untouched"]) or ()):
		tell_card(todo.allocated_to)


def managers():
	"""Roles that may write another person's row: those the ledger lets create one without the own-records limit."""
	return [role for role, perms in ledger.rows_for(DOCTYPE).items() if perms[2] and not (len(perms) > 4 and perms[4])]


def on_shift(user, at=None):
	"""Is one of the user's pool shifts open at `at` with the user not on leave; a user in no pool is never on shift."""
	return _shift_open(_pools(user), user, get_datetime(at or now_datetime()))


def lead_flow(user, at=None):
	"""Why the user's check-in pools give them no lead now, read from the draw's own `why_not` (eligible in any pool wins), with when their shift ends and the next opens; None outside any pool."""
	from tatva_connect.lead import routing
	from tatva_connect.lead.assignment_rule import UNTOUCHED_LIMIT

	at = get_datetime(at or now_datetime())
	# No axes: entitlement depends on each lead's grain, so a per-rep card leaves it to the draw.
	verdicts = [(rule, member, rule.why_not(member, (), at)) for rule, _row in _pools(user)
	            for member in rule.weighted_users if member.user == user]
	if not verdicts:
		return None
	eligible = [(rule, member) for rule, member, why in verdicts if why is None]
	reason = None if eligible else verdicts[0][2]
	# The countdown follows only pools that would give the rep a lead; the next opening is the earliest across every pool.
	ends = [end for rule, member in eligible if (end := rule.shift_bounds(member.work_shift, at, user)[0])]
	nexts = [nxt for rule, member, _why in verdicts if (nxt := rule.shift_bounds(member.work_shift, at, user)[1])]
	flow = {"reason": reason, "ends_at": max(ends) if ends else None, "next_at": min(nexts) if nexts else None}
	if reason == UNTOUCHED_LIMIT:
		rule = verdicts[0][0]
		flow.update(untouched=routing.untouched_count(rule.name, user), limit=cint(rule.get("max_untouched")))
	return flow


def uses_checkin(user):
	"""Does any business line the user is entitled to have check-in on; the lines come from `access.entitlement`, the one grain reader."""
	lines = set(frappe.get_all("CRM Vertical", filters={"checkin_enabled": 1}, pluck="name"))  # authz-ok: tier-a — a switch on the business lines, read to answer a status
	if not lines:
		return False
	grains = entitlement.entitled_grains(user)
	# Only a grant naming a switched-on line counts: an all-grains admin or a blank-vertical manager is not a rep checking in.
	return grains != entitlement.ALL_GRAINS and any(vertical in lines for vertical, _group, _program in grains)


def wants_check_in(user, at=None):
	"""The login prompt's rule: check-in is on for the user, they are not Active, and a pool shift of theirs is open or, in no pool, it is their first login of a day with no row of theirs."""
	if not uses_checkin(user) or is_active(user):
		return False
	at = get_datetime(at or now_datetime())
	if pools := _pools(user):
		# The shift test already refuses a user on leave that day.
		return _shift_open(pools, user, at)
	from tatva_connect.lead.assignment_rule import on_leave

	midnight = get_datetime(at.date())
	return not on_leave(user, at.date()) and _first_login(user, midnight) and not _row_since(user, midnight)


def close_ended_shifts():
	"""Runs at each shift end: check out every Active user who is in a pool and on none of its shifts now."""
	_check_out({row.user for row in _member_rows()})


def close_the_day():
	"""Runs just after midnight: check out every check-in user still Active and on none of their pool shifts now."""
	if not automation.is_enabled(MIDNIGHT_CHECKOUT):
		return
	users = set(frappe.get_all(DOCTYPE, pluck="user", distinct=True))  # authz-ok: tier-a — scheduler, reads whose status to settle
	_check_out({user for user in users if uses_checkin(user)})


def _check_out(users):
	"""The one check-out loop both jobs share: each Active user on none of their pool shifts now gets an Unavailable row."""
	at = now_datetime()
	for user in sorted(users):
		if not is_active(user) or _shift_open(_pools(user), user, at):
			continue
		frappe.db.savepoint("checkin_shift_end")
		try:
			record(user, UNAVAILABLE, SHIFT_END)
		except Exception:
			frappe.db.rollback(save_point="checkin_shift_end")
			frappe.log_error(title="checkin: shift-end check-out failed", reference_doctype="User", reference_name=user)


def sync_shift_jobs():
	"""One runtime cron job per distinct shift end (check-out) and start (wake waiting pools), booked as core books its own."""
	hours = frappe.get_all(
		"CRM Service Day", filters={"parenttype": SHIFT_DT, "parentfield": "working_hours"}, fields=["start_time", "end_time"]
	)
	_sync_crons(CLOSE_JOB, {_cron(row.end_time) for row in hours})
	_sync_crons(OPEN_JOB, {_cron(row.start_time) for row in hours})


def _sync_crons(method, wanted):
	"""Make the `method`'s runtime cron jobs exactly `wanted`: drop the ones no shift needs, add the missing ones."""
	held = frappe.get_all(
		"Scheduled Job Type", filters={"method": method, "scheduler_event": ["is", "set"]},
		fields=["name", "cron_format", "scheduler_event"],
	)
	for job in held:
		if job.cron_format not in wanted:
			# The job first: it links the event, and frappe refuses to delete a linked row.
			frappe.delete_doc("Scheduled Job Type", job.name, ignore_permissions=True)  # authz-ok: tier-a — derived from shift rows the caller already saved
			frappe.delete_doc("Scheduler Event", job.scheduler_event, ignore_permissions=True)  # authz-ok: tier-a — same
	for cron in wanted - {job.cron_format for job in held}:
		event = frappe.get_doc({"doctype": "Scheduler Event", "scheduled_against": SHIFT_DT, "method": method})
		event.insert(ignore_permissions=True)  # authz-ok: tier-a — derived from shift rows the caller already saved
		frappe.get_doc({
			"doctype": "Scheduled Job Type", "frequency": "Cron", "cron_format": cron,
			"method": method, "scheduler_event": event.name,
		}).insert(ignore_permissions=True)  # authz-ok: tier-a — same


def mirror_to_helpdesk(row):
	"""Set the user's HD Agent availability to the first enabled status of the row's category, through HD Agent's own save."""
	if not _mirrors():
		return
	agent = frappe.db.get_value("HD Agent", {"user": row.user}, ["name", "availability"], as_dict=True)
	if not agent or _category(agent.availability) == row.status:
		return
	status = frappe.db.get_value(
		"HD Agent Status", {"category": row.status, "enable": 1}, "name", order_by="status_order asc"
	)
	if not status:
		return
	doc = frappe.get_doc("HD Agent", agent.name)
	doc.availability = status
	doc.save(ignore_permissions=True)  # authz-ok: tier-a — the check-in row this mirrors was itself permission-checked


def from_helpdesk(hd_agent, method=None):
	"""HD Agent.on_update: an availability change made in Helpdesk becomes a row; the same status writes nothing, so the two never loop."""
	if not _mirrors() or not hd_agent.availability_changed():
		return
	if category := _category(hd_agent.availability):
		record(hd_agent.user, category, HELPDESK)


def _pools(user):
	"""`[(rule, member row)]` for every enabled weighted pool listing `user` that requires check-in, in pool-name order; read once per request (A.6: none while no pool does)."""
	from tatva_connect.access import request_cache

	return request_cache("tatva_connect:checkin_pools", user,
	                     lambda: [(frappe.get_cached_doc("Assignment Rule", row.parent), row) for row in _member_rows(user)])


def _member_rows(user=None):
	"""Weighted member rows on enabled pools that require check-in, for one user or all."""
	filters = {"parenttype": "Assignment Rule", "parentfield": "weighted_users"}
	if user:
		filters["user"] = user
	pools = frappe.get_all("Assignment Rule", filters={"disabled": 0, "require_checkin": 1}, pluck="name")  # authz-ok: tier-a — pool settings, read to answer a status
	if not pools:
		return []
	return frappe.get_all(  # authz-ok: tier-a — membership rows, read to answer a status
		"Assignment Rule User", filters={**filters, "parent": ["in", pools]}, fields=["parent", "user", "work_shift"], order_by="parent asc"
	)


def _shift_open(pools, user, at):
	return any(rule.on_shift_now(row.work_shift, at, user) for rule, row in pools)


def _first_login(user, since):
	"""Is this the user's only login since `since`, counted from frappe's own login feed (`Activity Log`, written on every new session)."""
	return frappe.db.count("Activity Log", {"user": user, "operation": "Login", "status": "Success", "creation": [">=", since]}) <= 1


def _row_since(user, since):
	"""Has the user a row since `since`; a Shift End row is the engine's check-out, not theirs, so it does not count."""
	return bool(frappe.db.exists(DOCTYPE, {"user": user, "creation": [">=", since], "source": ["!=", SHIFT_END]}))


def _cron(end_time):
	"""`MM HH * * *` for the first whole minute at or after the time: an end job never runs inside its shift, a start job never before it."""
	end = get_time(end_time)
	minutes = (end.hour * 60 + end.minute + (1 if end.second else 0)) % 1440
	return f"{minutes % 60} {minutes // 60} * * *"


def _mirrors():
	return "helpdesk" in frappe.get_installed_apps() and automation.is_enabled(HELPDESK_MIRROR)


def _category(status):
	return frappe.get_cached_value("HD Agent Status", status, "category") if status else None
