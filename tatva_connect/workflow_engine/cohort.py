# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Scheduled Triggers: when a workflow is next due, how many leads it selects, and the walk that starts them.

Each selected lead gets its own ordinary journey through `triggers.start_journey`, the entry a save uses. The walk
runs inside the workflow drain's pass (`drain.run`), so the drain lock serialises it and the site pace sizes it.
The preview and the walk read the same selector (`matching_leads`): grain in SQL, criteria through
`rules.predicate_match`. The cursor commits with each journey started, and a workflow stays due until its walk
finishes, so a pass that dies resumes where it stopped.
"""
import time
from datetime import datetime, timedelta

import frappe
from croniter import croniter
from frappe import _
from frappe.utils import get_datetime, get_time, getdate, now_datetime

from tatva_connect.taxonomy import grain
from tatva_connect.workflow_engine import registry, thresholds

_WORKFLOW_DT = "CRM Workflow"

# The index behind "which workflows are due?" — equality on mode, then range on the clock, in that order.
DUE_INDEX = "ix_workflow_due"
DUE_FIELDS = ["trigger_mode", "trigger_next_run_at"]

# A preview that walks a million rows to be exact is the outage it exists to prevent; past this it bounds.
PREVIEW_CAP = 5000
_PAGE = 500


def next_run_at(config, after=None):
	"""When a Trigger carrying this config is next due after `after` (default now), or None when it has no run ahead.

	FRAPPE NATIVE, and the API rejected is named: `Scheduled Job Type` is a site-level singleton keyed by method path,
	so every workflow would become a row inside frappe's scheduler loop, out of reach of our switch, grain and abort.
	What we take from it is its vocabulary and arithmetic: its frequency names and crons (`registry.SCHEDULES`),
	walked by `croniter`, frappe's own dependency. A dated Once run, a start and an end bound that walk; no date
	maths of our own.
	"""
	config = config or {}
	if config.get("mode") != registry.MODE_SCHEDULE:
		return None
	after = get_datetime(after) if after else now_datetime()
	at = _time_of_day(config.get("schedule_time"))
	if config.get("schedule") == registry.ONCE:
		day = config.get("schedule_date")
		run = datetime.combine(getdate(day), at) if day else None
		return run if run and run > after else None
	cron = _cron(config, at)
	if not cron:
		return None  # an unreadable schedule is the publish gate's business, not a guess made here
	start = config.get("schedule_start")
	if start:
		after = max(after, datetime.combine(getdate(start), datetime.min.time()) - timedelta(seconds=1))
	run = croniter(cron, after).get_next(datetime)
	end = config.get("schedule_end")
	return None if end and run.date() > getdate(end) else run


def _time_of_day(schedule_time):
	"""The author's time of day, read by `frappe.utils.get_time`; blank or unreadable keeps midnight, which publish refuses."""
	try:
		return get_time(schedule_time) if schedule_time else datetime.min.time()
	except ValueError:
		return datetime.min.time()  # a draft may hold a half-typed time; `registry._time_problems` refuses it at publish


def _cron(config, at):
	"""Frappe's cron for the frequency, moved to the author's time, weekdays and day of month."""
	base = registry.SCHEDULES.get(config.get("schedule"))
	if not base:
		return None
	_minute, _hour, day_of_month, month, day_of_week = base.split(" ")
	weekdays = [day for day in config.get("schedule_weekdays") or [] if day in registry.WEEKDAYS]
	if config.get("schedule") == registry.WEEKLY and weekdays:
		# The calendar counts from Monday and cron from Sunday.
		day_of_week = ",".join(str((registry.WEEKDAYS.index(day) + 1) % len(registry.WEEKDAYS)) for day in weekdays)
	month_day = config.get("schedule_month_day")
	if config.get("schedule") == registry.MONTHLY and month_day:
		day_of_month = "L" if month_day == registry.LAST_DAY else month_day
	return f"{at.minute} {at.hour} {day_of_month} {month} {day_of_week}"


@frappe.whitelist()
def schedule_readout(config):
	"""What a scheduled Trigger will do as configured — its next run and the cohort it takes now, as the inspector's rows."""
	config = frappe.parse_json(config) if isinstance(config, str) and config.strip() else (config or {})
	counted = preview(config)
	run = next_run_at(config)
	size = frappe.format(counted["count"] or 0, {"fieldtype": "Int"})
	return [
		{"label": _("Next run"), "value": frappe.utils.format_datetime(run) if run else _("No more runs")},
		{"label": _("Matches now"), "value": f"{size}+" if counted["capped"] else size},
	]


@frappe.whitelist()
def preview(config, cap=PREVIEW_CAP):
	"""How many leads this Trigger's criteria would take, counted the way the engine really judges them.

	`{count, capped, subject}` — `count` is None for a record-event Trigger, which starts one journey per save
	and has no cohort to count. At these volumes the number IS the safety feature: *"this will start 3,140
	journeys"* is the difference between a mistake and an incident, and an author who cannot see it before
	arming is guessing.

    Counted, never estimated: grain narrows in SQL (it is columns on the lead), then each candidate is
	judged by `rules.predicate_match` — the SAME call `triggers._predicate_holds` makes. Capped, because a
	preview is not allowed to become the outage it exists to prevent.
	"""
	config = frappe.parse_json(config) if isinstance(config, str) and config.strip() else (config or {})
	if not frappe.has_permission("CRM Workflow", "read"):
		frappe.throw(_("Not permitted"), frappe.PermissionError)
	if config.get("mode") != registry.MODE_SCHEDULE:
		return {"count": None, "capped": False, "subject": config.get("subject_doctype")}

	subject = config.get("subject_doctype") or "CRM Lead"
	cap = int(cap or PREVIEW_CAP)
	count, capped = _count_matching(subject, config, cap)
	return {"count": count, "capped": capped, "subject": subject}


def matching_leads(subject, config, after=None, limit=_PAGE):
	"""`(matched, scanned_to)` — up to `limit` leads this Trigger's criteria SELECT, and how far we read.

	THE ONE SELECTOR: the preview counts through it and the drain walks through it, so the number an
	author is shown and the people who are actually started cannot come from two answers.

	TWO NUMBERS, AND CONFLATING THEM TRUNCATES A COHORT. `limit` is how many MATCHES the caller wants;
	`scanned_to` is how far down the grain this got to find them. An earlier cut applied `limit` to the
	grain query and filtered afterwards, so a chunk that happened to open on non-matching leads came back
	empty and the drain concluded the cohort was finished — a 2,000-lead cohort could start nobody and
	report done. The grain narrows in SQL because it is columns on the lead; the criteria are judged by
	the SAME `rules.predicate_match` the record-event lane runs, never a SQL translation of the predicate.

	`after` is the cursor — a lead name, exclusive. Keyset, never OFFSET.

	The page carries the subject's own COLUMNS, so the doc a criterion is judged on is built from the row
	already read rather than re-fetched a lead at a time — see `_row_columns` for the one shape that still
	re-fetches, and why the context is identical either way.
	"""
	from tatva_connect.automation import context as ctx_build
	from tatva_connect.automation import rules

	predicate = config.get("predicate")
	fields = ctx_build.fields_for(subject)
	base = _grain_filters(config)
	columns = _row_columns(subject, predicate)
	matched, cursor = [], after
	while len(matched) < limit:
		filters = dict(base)
		if cursor:
			filters["name"] = [">", cursor]
		rows = frappe.get_all(  # authz-ok: tier-a — workflow engine; the cohort is the Trigger's declared criteria
			subject, filters=filters, fields=columns or ["name"], order_by="name asc", limit=_PAGE,
		)
		if not rows:
			return matched, cursor
		cursor = rows[-1].name
		for row in rows:
			if predicate:
				doc = frappe.get_doc({"doctype": subject, **row}) if columns else frappe.get_doc(subject, row.name)
				if not rules.predicate_match(predicate, ctx_build.context_for(doc, {}), fields):
					continue
			matched.append(row.name)
			if len(matched) == limit:
				# Stop ON the match, so the frontier never runs past a lead nobody has looked at yet.
				return matched, row.name
	return matched, cursor


def _row_columns(subject, predicate):
	"""The subject's own columns, so the page reads the whole row — or None where a lead must be hydrated.

	ONE SELECTOR, ONE VERDICT, AND THE COST IS THE ONLY THING THAT MOVES. `frappe.get_doc(subject, name)`
	loads every child table to answer a predicate that usually reads a handful of lead columns — 12 queries
	a lead, and the pace is on MATCHES, so a selective predicate walks tens of thousands of rows for one
	chunk. A doc built from a row already read loads none: `init_valid_columns` fills what the row did not
	carry and `get_valid_dict` then returns the SAME bucket `context_for` builds off a hydrated doc, virtual
	fields included, because every column they compute from is present. Proven over a live 550-lead grain:
	every non-section key equal in value AND type.

	CHILD SECTIONS ARE THE ONE THING A ROW CANNOT ANSWER, so a predicate naming a `<table>.<column>` section
	path — or anything else `get_valid_fields` does not declare — keeps hydrating. `contract._predicate_fields`
	is the walk (the same one the publish gate's read check runs), never a second reading of the tree.
	"""
	from tatva_connect.workflow_engine import contract, refs

	if not predicate:
		return None
	meta = frappe.get_meta(subject)
	answerable = set(meta.get_valid_fields())
	for ref in contract._predicate_fields(predicate):
		parsed = refs.parse(ref)
		if not parsed or parsed[0] != refs.slug(subject) or parsed[1] not in answerable:
			return None
	return meta.get_valid_columns()


def _count_matching(subject, config, cap):
	"""Walk the cohort through the ONE selector, stop at the cap.

	Counted, never estimated — and counted by the same function the drain walks, so the preview cannot
	promise a number the drain then disagrees with.
	"""
	count, cursor = 0, None
	while count < cap:
		page, cursor = matching_leads(subject, config, after=cursor, limit=min(_PAGE, cap - count))
		if not page:
			return count, False
		count += len(page)
	return count, True


def _grain_filters(config):
	"""The grain axes as lead columns. A BLANK axis is the wildcard — it means ANY, so it adds no filter.

	The one matcher's semantic (`taxonomy.grain`), expressed as SQL rather than re-decided: an exact-tuple
	comparison here is the defect that once hid 129 fields from 1,894 leads while every test stayed green.
	"""

	# Derived from the two tuples that already exist, never restated — `strict` goes red if they diverge.
	columns = dict(zip(grain.AXES, grain.columns("CRM Lead"), strict=True))
	return {
		columns[axis]: (config.get(axis) or "").strip()
		for axis in grain.AXES
		if (config.get(axis) or "").strip()
	}


# ── THE WALK ──────────────────────────────────────────────────────────────────────────────────────────

# `cohort_state` is the label the workflow screen reads to show "Stop cohort"; the drain lock is what serialises the walk.
IDLE, DRAINING = "", "Draining"


def start_due(limit, until, renew):
	"""Start up to `limit` leads across the cohorts that are due, oldest due first, stopping at `until`. Returns how many it took."""
	taken = 0
	for name in due_workflows():
		if taken >= limit or time.monotonic() >= until:
			break
		try:
			taken += _walk(name, limit - taken, until, renew)
		except Exception:
			# One cohort's failure is not the pass's: its cursor holds, and the next pass retries it.
			frappe.db.rollback()
			frappe.log_error(title="cohort walk: a batch failed", message=f"workflow={name}\n{frappe.get_traceback()}")
	return taken


def due_workflows(limit=None):
	"""Active scheduled workflows whose clock has come, oldest first — the question `DUE_INDEX` serves."""
	from tatva_connect.tatva_connect.doctype.crm_workflow.crm_workflow import ARMED_STATE

	return frappe.get_all(  # authz-ok: tier-a — workflow engine, drain context
		_WORKFLOW_DT,
		filters={"lifecycle_state": ARMED_STATE, "trigger_mode": registry.MODE_SCHEDULE, "trigger_next_run_at": ["<=", now_datetime()]},
		pluck="name",
		order_by="trigger_next_run_at asc",
		limit=limit or thresholds.MAX_DUE_PER_PASS,
	)


def next_due_at():
	"""When the earliest Active scheduled workflow next comes due, or None."""
	from tatva_connect.tatva_connect.doctype.crm_workflow.crm_workflow import ARMED_STATE

	return frappe.db.get_value(  # authz-ok: tier-a — workflow engine, drain context
		_WORKFLOW_DT,
		{"lifecycle_state": ARMED_STATE, "trigger_mode": registry.MODE_SCHEDULE, "trigger_next_run_at": [">", now_datetime()]},
		"trigger_next_run_at",
		order_by="trigger_next_run_at asc",
	)


def abort(workflow_name):
	"""Stop this cohort's walk; journeys already started are left alone (Suspend ends those). An Active workflow finishes at the next pass, any other now."""
	from tatva_connect.tatva_connect.doctype.crm_workflow.crm_workflow import ARMED_STATE

	if frappe.db.get_value(_WORKFLOW_DT, workflow_name, "lifecycle_state") != ARMED_STATE:
		_finish(workflow_name)
		return
	frappe.db.set_value(_WORKFLOW_DT, workflow_name, "cohort_abort", 1, update_modified=False)
	frappe.db.commit()


def _walk(workflow_name, limit, until, renew):
	"""One batch of one cohort: select past the cursor, start each lead, and finish the occurrence once none are left."""
	from tatva_connect.workflow_engine import versions

	version = versions.current_name(workflow_name)
	row = frappe.db.get_value(_WORKFLOW_DT, workflow_name, ["cohort_cursor", "cohort_abort"], as_dict=True) or frappe._dict()
	if not version or row.cohort_abort:
		_finish(workflow_name)
		return 0
	config = _trigger_config(workflow_name)
	leads, scanned_to = matching_leads(
		config.get("subject_doctype") or "CRM Lead", config, after=row.cohort_cursor, limit=limit,
	)
	if not leads:
		_finish(workflow_name)
		return 0
	# The selector's reads end here, so the first cursor write starts a fresh snapshot rather than one an abort may have moved.
	frappe.db.commit()
	for index, lead in enumerate(leads):
		renew()
		if time.monotonic() >= until or not _still_walking(workflow_name):
			# Out of time, suspended or aborted mid-batch: the cursor names the last lead begun, and the next pass decides.
			frappe.db.commit()
			return index
		# Written BEFORE the start, so it commits in the journey's own transaction: killed after that commit, the walk resumes past this lead and never re-sends to it.
		frappe.db.set_value(_WORKFLOW_DT, workflow_name,
		                    {"cohort_state": DRAINING, "cohort_cursor": lead, "cohort_progress_at": now_datetime()},
		                    update_modified=False)
		_start_one(workflow_name, version, lead)
	if len(leads) < limit:
		# The selector ran out of leads before it filled the batch, so the occurrence is over now rather than one pass later.
		_finish(workflow_name)
		return len(leads)
	# The whole batch began, so the cursor may move to how far the selector READ — leads the criteria rejected are never re-scanned.
	frappe.db.set_value(_WORKFLOW_DT, workflow_name, {"cohort_cursor": scanned_to}, update_modified=False)
	frappe.db.commit()
	return len(leads)


def _still_walking(workflow_name):
	"""Active and not aborted, read fresh before each start, so a Suspend or a Stop lands within one lead."""
	from tatva_connect.tatva_connect.doctype.crm_workflow.crm_workflow import ARMED_STATE

	row = frappe.db.get_value(_WORKFLOW_DT, workflow_name, ["lifecycle_state", "cohort_abort"], as_dict=True)
	return bool(row) and row.lifecycle_state == ARMED_STATE and not row.cohort_abort


def _start_one(workflow_name, version, lead):
	"""One ordinary journey for one lead, through the entry a save uses — called INLINE, because N enqueues is the pile-up the drain avoids.

	A lead already running is a no-op (`active_key`) and a completed one is refused (W8.4 run-once), both inside `start_journey`.
	"""
	from tatva_connect.workflow_engine import triggers

	try:
		triggers.start_journey(workflow_name, version, lead)
	except Exception:
		# One lead's failure is not the cohort's: recorded, and the walk goes on.
		frappe.db.rollback()
		frappe.log_error(title="cohort walk: a lead failed to start",
		                 message=f"workflow={workflow_name} lead={lead}\n{frappe.get_traceback()}")


def _finish(workflow_name):
	"""End the occurrence: clear the walk and move the clock, so the next occurrence starts from the first lead."""
	frappe.db.set_value(_WORKFLOW_DT, workflow_name, {
		"cohort_state": IDLE,
		"cohort_cursor": "",
		"cohort_abort": 0,
		"trigger_next_run_at": next_run_at(_trigger_config(workflow_name)),
	}, update_modified=False)
	frappe.db.commit()


def _trigger_config(workflow_name):
	"""This workflow's Trigger config, through the ONE reader."""
	node = frappe.get_all(
		"CRM Workflow Node",
		filters={"workflow": workflow_name, "node_type": registry.TRIGGER},
		fields=["config_json"],
		limit=1,
	)
	return registry.config_of(node[0]) if node else {}
