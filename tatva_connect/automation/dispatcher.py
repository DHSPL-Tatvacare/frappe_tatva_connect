"""The automation engine dispatcher — trigger → grain fan-out → guarded executor → run log.

`fire_rules` is the v1 trigger (CRM Task on_update): on the FIRST Done flip of a lead-linked task it
builds the activity context (the value-source seam — reconstruct_values, one brain), fans out to
every enabled rule whose grain matches the lead (ALL-MATCH, rules.matching_rules), and for each
matched rule whose criteria all pass runs its actions in order through a guarded executor. One action
(or rule) failing never aborts the user's save and never blocks siblings (spec §5.2). Every fire
writes one Run Log row; every failure also hits the common error factory with a structured tag.

Re-entrancy: actions write to the LEAD, which can re-enter no engine here (trigger is the task only).
A belt-and-braces `doc.flags.in_automation` + the dispatch's own guard keep it non-re-entrant even if
the trigger surface widens later. Nothing is reimplemented — grain, context, task creation, and the
allowlist all reuse existing brains.
"""
import time

import frappe

from tatva_connect import automation
from tatva_connect.activity.automation import reconstruct_values
from tatva_connect.automation import rules

KILL_SWITCH = "Task::Automation::rules"
RUN_LOG = "CRM Automation Run Log"
DEFAULT_RETENTION_DAYS = 90  # code fallback (no baked form value) — used until an operator sets one.


def fire_rules(doc, method=None):
	"""CRM Task on_update: run matching automation rules when a lead-linked task FIRST becomes Done.

	Dormant + fail-closed (kill switch off => return), idempotent (only the Done flip fires), and
	non-re-entrant (an action's lead save can't re-enter the engine this request)."""
	if not automation.is_enabled(KILL_SWITCH):
		return
	if getattr(doc.flags, "in_automation", False):
		return  # re-entrancy guard: a lead save we triggered must not re-enter the engine
	if doc.status != "Done":
		return
	before = doc.get_doc_before_save()
	if before and before.status == "Done":
		return  # already Done before this save — don't re-fire (idempotency)
	if doc.reference_doctype != "CRM Lead" or not doc.reference_docname:
		return

	lead = doc.reference_docname
	vertical, group, program = rules.lead_axes(lead)
	matched = rules.matching_rules(vertical, group, program)
	if not matched:
		return

	context = build_context(doc)
	for r in matched:
		_run_rule(r, lead, context, doc, grain=_grain_tag(vertical, group, program))


def build_context(doc):
	"""The value-source seam (spec §3.1): the v1 provider IS reconstruct_values — the submitted
	activity form rebuilt as {fieldname: value}. Future triggers add providers; the matcher/executor
	are unchanged. Imported, never copied."""
	return reconstruct_values(doc)


def _run_rule(r, lead, context, trigger_doc, grain):
	"""Evaluate one rule's criteria; if all match, run its actions in a guarded executor and log."""
	started = time.monotonic()
	rule = frappe.get_doc("CRM Automation Rule", r.name)
	if not rules.criteria_match(rule.criteria, context):
		return  # not a fire — no log (only fires are audited)

	success = failed = 0
	last_error = ""
	for action in rule.actions:
		try:
			_run_action(action, lead, context)
			success += 1
		except Exception as e:
			failed += 1
			last_error = str(e)
			_log_error(rule.name, action.action_type, grain, e)

	duration_ms = int((time.monotonic() - started) * 1000)
	_write_run_log(rule, lead, trigger_doc, grain, success, failed, last_error, duration_ms)


# -- actions -----------------------------------------------------------------


def _run_action(action, lead, context):
	"""Dispatch one action by type. v1: Create Task + Set Field (spec §4). Raises on failure so the
	caller's per-action guard records it — one failure never touches siblings."""
	if action.action_type == "Create Task":
		_action_create_task(action, lead, context)
	elif action.action_type == "Set Field":
		_action_set_field(action, lead, context)
	else:
		raise ValueError(f"unknown action type {action.action_type!r}")


def _action_create_task(action, lead, context):
	"""CREATE_TASK — reuse the idempotent follow-up helper (one open task per lead+type)."""
	from tatva_connect.tasks.tasks import create_followup_task

	create_followup_task(
		lead=lead,
		task_type=action.task_type,
		due_at=context.get(action.due_from) if action.due_from else None,
	)


def _action_set_field(action, lead, context):
	"""SET_FIELD via the UNIFIED write path (spec §4.1): load the doc, set the field, save with
	flags.in_automation — NEVER frappe.db.set_value (which skips validate/hook re-mirroring). The
	field is re-checked against the enabled allowlist at runtime too (defense in depth, spec §5.3)."""
	if not (action.target_doctype and action.fieldname):
		raise ValueError("Set Field action missing target doctype or fieldname")
	if not _allowlisted(action.target_doctype, action.fieldname, lead):
		raise PermissionError(
			f"{action.fieldname} on {action.target_doctype} not in the enabled Automatable-Field allowlist"
		)
	value = context.get(action.context_field) if action.value_mode == "From Context" else action.value

	target = lead if action.target_doctype == "CRM Lead" else None
	if target is None:
		# v1 SET_FIELD targets the trigger's lead. A non-lead target would need its own resolution
		# (Phase 2 child-row work) — reject rather than guess.
		raise ValueError(f"Set Field target {action.target_doctype} is not supported in v1 (lead only)")
	tdoc = frappe.get_doc("CRM Lead", target)
	tdoc.set(action.fieldname, value)
	tdoc.flags.in_automation = True
	tdoc.save(ignore_permissions=True)


def _allowlisted(target_doctype, fieldname, lead):
	"""Runtime re-check of the fail-closed write allowlist (mirrors the rule controller, by grain).
	An enabled row whose set axes equal the lead's grain (blank axis = wildcard) authorizes the write."""
	vertical, group, program = rules.lead_axes(lead)
	rows = frappe.get_all(
		"CRM Automatable Field",
		filters={"target_doctype": target_doctype, "fieldname": fieldname, "enabled": 1},
		fields=["vertical", "group", "program"],
	)
	axes = {"vertical": vertical or "", "group": group or "", "program": program or ""}
	for row in rows:
		if all((row.get(a) or "") in ("", axes[a]) for a in axes):
			return True
	return False


# -- logging -----------------------------------------------------------------


def _grain_tag(vertical, group, program):
	return "{0}::{1}::{2}".format(vertical or "", group or "", program or "")


def _write_run_log(rule, lead, trigger_doc, grain, success, failed, error, duration_ms):
	"""ONE Run Log row per fire (spec §8). Its own try/except — a log-write failure never breaks
	the run (audit/observability, not error handling)."""
	outcome = "Success" if not failed else ("Partial" if success else "Failed")
	try:
		frappe.get_doc(
			{
				"doctype": RUN_LOG,
				"fire_time": frappe.utils.now_datetime(),
				"rule": rule.name,
				"trigger_doctype": trigger_doc.doctype,
				"trigger_docname": trigger_doc.name,
				"lead": lead,
				"grain": grain,
				"action_count": success + failed,
				"actions_success": success,
				"actions_failed": failed,
				"outcome": outcome,
				"error": error or "",
				"duration_ms": duration_ms,
			}
		).insert(ignore_permissions=True)
	except Exception:
		frappe.log_error("automation: run-log write failed")


def _log_error(rule_name, action_type, grain, err):
	"""Common error factory with a structured, countable tag (spec §8)."""
	frappe.log_error(
		"automation: rule={r} action={a} grain={g} :: {e}".format(
			r=rule_name, a=action_type, g=grain, e=err
		)
	)


# -- retention ---------------------------------------------------------------


def sweep_run_log():
	"""Scheduled job (spec §8): delete Run Log rows older than the operator-set retention. Idempotent —
	a re-run over an already-swept window deletes nothing. Retention reads from CRM Tatva Automation if
	an operator field exists; otherwise the code fallback (90 days), never a baked form value."""
	if not automation.is_enabled("Task::Automation::run-log-sweep"):
		return  # gated like every scheduled automation (invariant #6) — its registry toggle is real
	days = _retention_days()
	cutoff = frappe.utils.add_to_date(frappe.utils.now_datetime(), days=-days)
	frappe.db.delete(RUN_LOG, {"fire_time": ["<", cutoff]})


def _retention_days():
	"""Operator-set retention in days, else the code fallback. No field exists in v1 (the control
	doctype is a per-key registry, not a settings singleton) — so this resolves to the fallback until
	one is added, honoring 'sensible behaviour in a code fallback, never a baked form value'."""
	return DEFAULT_RETENTION_DAYS
