"""The Wait resume queue (TATVA v2, Task 9) — `park()` inserts one `CRM Automation Resume` row
when `dispatcher.run_effects` hits a `Wait` action; `sweep_resume()` is the scheduled job that
resumes a parked segment at/after its `resume_at`, calling the SAME `dispatcher.run_effects`
executor (A.8 — no second executor), just with `start_idx=next_action_idx`. A chained rule
(`[A, Wait, B, Wait, C]`) can re-park mid-resume: `run_effects` may hit ANOTHER Wait and call
`park()` again, producing a new row — the resumed row is simply marked Done either way (it handed
off cleanly; the new park row carries the remainder forward).

HONEST SCOPING: a resume row carries only {rule, subject, next_action_idx, context} — never the
original triggering doc's identity (the doctype has no trigger_doctype/trigger_docname; that event
is long gone by the time a multi-day Wait elapses). On resume, `run_effects` is passed the SUBJECT
(Lead) doc as its own `trigger_doc` stand-in — so the two effect verbs that read `trigger_doc`
(Create Task's assignee carry-over, Update Field targeting "the triggering doc itself") see the
Lead, not the original trigger event, for any action that runs AFTER a Wait. This is the same
per-SEGMENT-not-per-rule honesty as `dispatcher.run_effects`'s atomicity — see its docstring.
"""
import frappe

from tatva_connect import automation

RESUME_DT = "CRM Automation Resume"
_PAGE_LIMIT = 200  # a sane cap per sweep — a huge backlog drains over several sweeps, not one giant job


def park(rule_name, subject, resume_at, next_action_idx, context):
	"""Insert one Pending row — the parked remainder of a rule's effect-lane run. `subject` is
	always a CRM Lead name (the automation engine's one subject type — `router._subject` always
	resolves to the Lead, never the raw trigger doc)."""
	frappe.get_doc({
		"doctype": RESUME_DT,
		"rule": rule_name,
		"subject_doctype": "CRM Lead",
		"subject_name": subject,
		"resume_at": resume_at,
		"next_action_idx": next_action_idx,
		"context_json": frappe.as_json(context),
		"status": "Pending",
	}).insert(ignore_permissions=True)


def sweep_resume():
	"""Scheduled job (~every 15 min, hooks.scheduler_events): gated by the SAME master kill switch
	as the rest of the engine (Task::Automation::rules — A.6, nothing resumes when the engine is
	off). Picks Pending rows whose `resume_at` has arrived and resumes each through the ONE effect
	executor (`dispatcher.run_effects`, A.8). Idempotent: a row is flipped to a terminal status
	(Done/Failed) as soon as it's resumed, so a second sweep over the same window finds nothing
	Pending left to re-run."""
	if not automation.is_enabled("Task::Automation::rules"):
		return

	rows = frappe.get_all(
		RESUME_DT,
		filters={"status": "Pending", "resume_at": ["<=", frappe.utils.now_datetime()]},
		fields=["name", "rule", "subject_doctype", "subject_name", "next_action_idx", "context_json"],
		order_by="resume_at asc",
		limit=_PAGE_LIMIT,
	)
	for row in rows:
		_resume_one(row)


def _resume_one(row):
	"""Resume one parked segment. Reuses the SAME run_effects executor a first fire uses (A.8) —
	the only difference is `start_idx=row.next_action_idx`. `run_effects` never raises for an
	action failure (it writes its own Run Log row and swallows the error, same as a first fire) —
	the try/except here only guards the resume plumbing itself (a deleted rule/lead, corrupt
	context_json), which the resume row's own status records."""
	from tatva_connect.automation import dispatcher, router, rules

	frappe.flags.in_automation = True  # writes this segment makes must not re-enter the router
	try:
		context = frappe.parse_json(row.context_json or "{}")
		subject_doc = frappe.get_doc(row.subject_doctype, row.subject_name)
		axes = rules.lead_axes(row.subject_name)
		grain = "{}::{}::{}".format(axes[0] or "", axes[1] or "", axes[2] or "")
		on_doctype = frappe.db.get_value("CRM Automation Rule", row.rule, "on_doctype")
		field_types = router._field_types_for(on_doctype)
		dispatcher.run_effects(
			row.subject_name, frappe._dict(name=row.rule), subject_doc, axes, grain, field_types, context,
			start_idx=row.next_action_idx,
		)
		frappe.db.set_value(RESUME_DT, row.name, "status", "Done")
	except Exception:
		frappe.log_error(
			title="automation: resume sweep failed",
			message=f"resume={row.name} rule={row.rule} :: {frappe.get_traceback()}",
		)
		frappe.db.set_value(RESUME_DT, row.name, "status", "Failed")
	finally:
		frappe.flags.in_automation = False
