"""The Wait resume queue — `park()` inserts one `CRM Automation Resume` row when
`dispatcher.run_effects` hits a `Wait` action; `sweep_resume()` is the scheduled job that resumes a
parked segment at/after its `resume_at`, calling the SAME `dispatcher.run_effects` executor (A.8 — no
second executor), just with `cursor=<row.cursor>`. A chained rule (`[A, Wait, B, Wait, C]`) can re-park
mid-resume: `run_effects` may hit ANOTHER Wait and park again, producing a new row — the resumed row is
marked Done either way (it handed off cleanly; the new row carries the remainder forward).

An execution binds to an IMMUTABLE `CRM Automation Rule Version`, never to the mutable rule (see
`automation.versions`). So the `cursor` is an index into a list that cannot change underneath it, a
deleted or reordered rule can neither re-route nor orphan a parked lead, and only deleting the rule
itself cancels anything.

DURABILITY: `sweep_resume` commits after EACH execution. Frappe's `ScheduledJobType.execute()` commits
once at the end and rolls back on error, so a batch-wide transaction would revert every already-resumed
row when a deploy kills the worker mid-sweep — while the WhatsApp messages those rows already handed to
the WATI HTTP API stay sent. Per-execution commit is what makes a resume at-least-once at the row level
instead of at the batch level.

HONEST SCOPING: a resume row carries only {version, subject, cursor, parked_at, context} — never the
original triggering doc's identity (that event is long gone by the time a multi-day Wait elapses). On
resume, `run_effects` is passed the SUBJECT (Lead) doc as its own `trigger_doc` stand-in — so the two
effect verbs that read `trigger_doc` (Create Task's assignee carry-over, Update Field targeting "the
triggering doc itself") see the Lead, not the original trigger event, for any action that runs AFTER a
Wait. Same per-SEGMENT-not-per-rule honesty as `dispatcher.run_effects`'s atomicity.
"""
import frappe

from tatva_connect import automation

RESUME_DT = "CRM Automation Resume"
RULE_SWITCH = "Task::Automation::rules"
RESUME_SWITCH = "Task::Automation::resume"  # the sweep's own catalog row — independent of the engine switch
_PAGE_LIMIT = 200  # a sane cap per sweep — a huge backlog drains over several sweeps, not one giant job


def park(rule, rule_version, subject, resume_at, cursor, context, parked_at):
	"""Insert one Pending row — the parked remainder of a rule's effect-lane run. `subject` is always a
	CRM Lead name (the engine's one subject type — `router._subject` always resolves to the Lead)."""
	frappe.get_doc({
		"doctype": RESUME_DT,
		"rule": rule,
		"rule_version": rule_version,
		"subject_doctype": "CRM Lead",
		"subject_name": subject,
		"cursor": cursor,
		"parked_at": parked_at,
		"resume_at": resume_at,
		"context_json": frappe.as_json(context),
		"status": "Pending",
	}).insert(ignore_permissions=True)


def sweep_resume():
	"""Scheduled job (~every 15 min, hooks.scheduler_events): double-gated — the master engine switch
	(A.6, nothing resumes while the engine is off) AND this sweep's own catalog row, so an operator can
	pause just the Wait sweep without killing the whole engine. Picks Pending rows whose `resume_at` has
	arrived and resumes each through the ONE effect executor (A.8), committing after each so a worker
	killed mid-sweep can never replay an execution whose sends already left."""
	if not (automation.is_enabled(RULE_SWITCH) and automation.is_enabled(RESUME_SWITCH)):
		return

	rows = frappe.get_all(
		RESUME_DT,
		filters={"status": "Pending", "resume_at": ["<=", frappe.utils.now_datetime()]},
		fields=["name", "rule_version", "subject_doctype", "subject_name", "cursor", "context_json"],
		order_by="resume_at asc",
		limit=_PAGE_LIMIT,
	)
	for row in rows:
		_resume_one(row)
		frappe.db.commit()


def _resume_one(row):
	"""Resume one parked execution through the SAME `run_effects` a first fire uses (A.8) — the only
	difference is `cursor`. `run_effects` never raises for an action failure; it reports one, and that
	report is what closes this row honestly (a failed segment must never read as Done). The try/except
	here guards the resume PLUMBING only (a deleted lead, a corrupt context, a swept version)."""
	from tatva_connect.automation import dispatcher, router, rules, versions

	frappe.flags.in_automation = True  # writes this segment makes must not re-enter the router
	try:
		# CLAIM the row under a write lock before doing any work, and re-read its status. The scheduler
		# never overlaps itself (ScheduledJobType dedupes on the job id), but a manual `bench execute`
		# can run alongside it — two sweeps that both SELECTed this row would otherwise both resume it,
		# double-sending a message. A second sweep blocks here until the first commits, then sees a
		# terminal status and skips. The lock releases on the per-row commit in `sweep_resume`.
		if not frappe.db.get_value(RESUME_DT, {"name": row.name, "status": "Pending"}, "name", for_update=True):
			return  # another sweep already claimed it
		definition = versions.load(row.rule_version)
		if not frappe.db.get_value("CRM Automation Rule", definition.rule, "enabled"):
			return  # HOLD — a disabled rule PAUSES its in-flight executions. Cancelling is a human act.
		context = frappe.parse_json(row.context_json or "{}")
		subject_doc = frappe.get_doc(row.subject_doctype, row.subject_name)
		axes = rules.lead_axes(row.subject_name)
		grain = dispatcher._grain_tag(*axes)
		result = dispatcher.run_effects(
			row.subject_name, row.rule_version, subject_doc, axes, grain,
			router._field_types_for(definition.on_doctype), context, cursor=row.cursor,
		)
		if result.failed:
			_close(row.name, "Failed", "an action in the resumed segment failed — see the Run Log")
		else:
			_close(row.name, "Done", "re-parked at a later Wait" if result.parked else "")
	except Exception:
		frappe.log_error(
			title="automation: resume sweep failed",
			message=f"resume={row.name} version={row.rule_version} :: {frappe.get_traceback()}",
		)
		_close(row.name, "Failed", "the execution could not be resumed — see the Error Log")
	finally:
		frappe.flags.in_automation = False


def _close(name, status, reason):
	frappe.db.set_value(RESUME_DT, name, {"status": status, "status_reason": reason})
