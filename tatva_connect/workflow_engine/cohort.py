# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""W7.2 PART A — what a scheduled Trigger DECLARES, and what an author is shown before arming it.

A COHORT IS A journey FACTORY, NOT A SECOND ENGINE. When the drain lands (Part B) a due workflow selects its
leads and each one gets its OWN ordinary journey down the identical graph. Nothing about node contracts,
park/resume or the interpreter changes, and nothing here starts a journey: this module answers two questions
and no more — *when is this workflow next due* and *how many leads would it take*.

NOTHING WALKS `trigger_next_run_at` YET. That is deliberate and it is the stopping point: a materialised
column that no sweep reads cannot start a journey, so this half ships inert rather than half-wired.

WHAT IT REUSES, AND WHY THERE IS NO SECOND BRAIN:
  * the grain matcher  — `taxonomy.grain`, the same one `triggers._trigger_context` narrows with
  * the criteria       — `rules.predicate_match`, the same evaluator the record-event lane runs
  * the context        — `automation.context`, so a lead is judged by the values a real journey would read
The preview therefore cannot disagree with the drain, because it is asking the same code the same way.
Translating the predicate into SQL would have been faster and would have been a second criteria brain
that silently diverges the first time an operator is added.
"""
import frappe
from frappe import _
from frappe.utils import get_datetime, now_datetime

from tatva_connect.taxonomy import grain
from tatva_connect.workflow_engine import registry

# The index behind "which workflows are due?" — equality on mode, then range on the clock, in that order.
DUE_INDEX = "ix_workflow_due"
DUE_FIELDS = ["trigger_mode", "trigger_next_run_at"]

# A preview that walks a million rows to be exact is the outage it exists to prevent; past this it bounds.
PREVIEW_CAP = 5000
_PAGE = 500


def next_run_at(config, after=None):
	"""When a Trigger carrying this config is next due, or None if it is not a scheduled one.

	FRAPPE NATIVE, and the API rejected is named: `Scheduled Job Type` itself is the obvious home for
	"run this on a cron", and it is wrong here — it is a SITE-level singleton keyed by method path, so
	every workflow would have to become a Scheduled Job Type row and the "which are due" question would
	move into frappe's own scheduler loop, where our switch, our grain and our cancel flag cannot reach.
	What we DO take from it is its vocabulary and its arithmetic: the frequency names are frappe's, the
	cron strings are the ones `scheduled_job_type.py:113-127` maps them to, and `croniter` — frappe's own
	dependency, already parsing exactly these — computes the next occurrence. No date maths of our own.
	"""
	if (config or {}).get("mode") != registry.MODE_SCHEDULE:
		return None
	cron = registry.SCHEDULES.get(config.get("schedule"))
	if not cron:
		return None  # an unreadable schedule is the publish gate's business, not a guess made here
	return _next_occurrence(_at_time_of_day(cron, config.get("schedule_time")), after)


def _at_time_of_day(cron, schedule_time):
	"""Move a daily/weekly/monthly cron to the author's hour. `0 0 * * *` at 09:30 becomes `30 9 * * *`.

	A blank or unreadable time keeps midnight, which is what the frequency already means — a half-typed
	time must not silently move a cohort to an hour nobody chose.
	"""
	minute, hour = "0", "0"
	parts = str(schedule_time or "").split(":")
	if len(parts) >= 2 and parts[0].strip().isdigit() and parts[1].strip().isdigit():
		h, m = int(parts[0]), int(parts[1])
		if 0 <= h <= 23 and 0 <= m <= 59:
			minute, hour = str(m), str(h)
	rest = cron.split(" ")[2:]
	return " ".join([minute, hour, *rest])


def _next_occurrence(cron, after=None):
	from croniter import croniter

	return croniter(cron, get_datetime(after) if after else now_datetime()).get_next(type(now_datetime()))


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
	config = frappe.parse_json(config) if isinstance(config, str) else (config or {})
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
	"""
	from tatva_connect.automation import context as ctx_build
	from tatva_connect.automation import rules

	predicate = config.get("predicate")
	field_types = ctx_build.field_types_for(subject)
	base = _grain_filters(config)
	matched, cursor = [], after
	while len(matched) < limit:
		filters = dict(base)
		if cursor:
			filters["name"] = [">", cursor]
		rows = frappe.get_all(  # authz-ok: tier-a — workflow engine; the cohort is the Trigger's declared criteria
			subject, filters=filters, fields=["name"], order_by="name asc", limit=_PAGE,
		)
		if not rows:
			return matched, cursor
		cursor = rows[-1].name
		for row in rows:
			if predicate:
				doc = frappe.get_doc(subject, row.name)
				if not rules.predicate_match(predicate, ctx_build.context_for(doc, {}), field_types):
					continue
			matched.append(row.name)
			if len(matched) == limit:
				# Stop ON the match, so the frontier never runs past a lead nobody has looked at yet.
				return matched, row.name
	return matched, cursor


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
	from tatva_connect.taxonomy.picklist import _LEAD_AXES

	# Derived from the two tuples that already exist, never restated — `strict` goes red if they diverge.
	columns = dict(zip(grain.AXES, _LEAD_AXES, strict=True))
	return {
		columns[axis]: (config.get(axis) or "").strip()
		for axis in grain.AXES
		if (config.get(axis) or "").strip()
	}
