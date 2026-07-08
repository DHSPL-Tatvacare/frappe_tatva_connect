"""The automation engine's guarded executor — grain-matched rule → per-action run → Run Log.

TATVA v2 (Task 4): the two v1 triggers (`fire_rules` — CRM Task on_update, Task-Completed; and
`watch.fire_field_change_rules` — CRM Lead/Task on_update, Field-Changed) are RETIRED into the ONE
wildcard event router (`automation/router.py`, keyed on `(on_doctype, event)`). This module now owns
only what's downstream of a trigger decision: the two-lane executor (Task 5) and every action
handler — router.run_guards/run_for_event call this module exactly as both old dispatchers did
(same Run Log, same allowlist recheck; no parallel brain, A.8).

TATVA v2 (Task 5): an after-commit action can DO but cannot DENY, so the executor splits into TWO
lanes, one grammar, ONE registry (`actions._ACTION_LANES`) declaring each verb's lane exactly once
(A.8):
  - GUARD lane (`run_guards`) — runs synchronously in `validate` (router.run_guards); a guard
    handler raising propagates out of validate and BLOCKS the save (S.1/S.3 — never swallowed). No
    savepoint (nothing is written yet), no Run Log (the save may never happen).
  - EFFECT lane (`run_effects`) — runs after commit (router.run_for_event), the ORIGINAL `_run_rule`
    body: per-rule savepoint, deferred thunks, Run Log. Iterates ONLY effect-lane actions — a rule's
    guard actions already ran in validate, never re-run here.
Both lanes reuse the ONE criteria evaluator (`rules.criteria_match`) and the ONE context the router
builds — no second copy of either.

TATVA v2 (Task 6): every verb handler + its resolver helpers + the `_ACTION_LANES` registry moved out
into `automation/actions.py` (a move, not a rewrite — A.8/A.12). This module keeps ONLY orchestration:
the two-lane executor, the per-action dispatch (`_run_action`), the Run Log writer and the error
factory. `actions` is imported for the registry and the per-action label.
"""
import time

import frappe

from tatva_connect import automation
from tatva_connect.automation import actions, rules

SWEEP_SWITCH = "Task::Automation::run-log-sweep"
RUN_LOG = "CRM Automation Run Log"
DEFAULT_RETENTION_DAYS = 90  # code fallback (no baked form value) — v1 has no operator field.


def run_guards(subject, r, context, field_types):
	"""GUARD lane (Task 5) — evaluate one rule's criteria; if they match, run every GUARD-lane action
	synchronously. A handler raising propagates straight out (no try/except here) — that raise IS the
	block, and it must reach `validate` unswallowed (S.1/S.3). No savepoint, no Run Log: nothing has
	been written yet and the save may never happen."""
	rule = frappe.get_doc("CRM Automation Rule", r.name)
	if not rules.criteria_match(rule.criteria, context, field_types):
		return  # not a fire — same non-match semantics as the effect lane
	for action in rule.actions:
		lane, handler = actions._ACTION_LANES.get(action.action_type, (None, None))
		if lane != "guard":
			continue  # an effect-lane action on the same rule runs later, after commit
		handler(action, subject, context)


def run_effects(subject, r, trigger_doc, axes, grain, field_types, context):
	"""EFFECT lane (Task 5) — the original per-rule executor: evaluate criteria once more (the
	after-commit context can differ from the sync one — e.g. a rapid A→B→C edit, spec §5.2), then run
	every EFFECT-lane action in a guarded, savepoint-atomic executor and write one Run Log row. Guard
	actions on this same rule already ran (or blocked the save) in validate — never re-run here."""
	started = time.monotonic()
	rule = frappe.get_doc("CRM Automation Rule", r.name)
	if not rules.criteria_match(rule.criteria, context, field_types):
		return  # not a fire — no log (only fires are audited)

	effect_actions = [a for a in rule.actions if actions._ACTION_LANES.get(a.action_type, (None, None))[0] == "effect"]

	# A rule is all-or-nothing: run every action inside a savepoint; if ANY action fails, roll the
	# whole rule back so no lead is left half-processed. Deferred side-effects (webhooks) fire only
	# after a clean commit. Native savepoint API — no hand-rolled transaction handling.
	success = 0
	errors = []
	details = []
	deferred = []
	i, action = 0, None
	save_point = f"tc_auto_rule_{frappe.generate_hash(length=8)}"
	frappe.db.savepoint(save_point)
	try:
		for i, action in enumerate(effect_actions, 1):
			result = _run_action(action, subject, context, axes, trigger_doc)
			label = actions._action_label(action)
			if callable(result):
				deferred.append(result)  # a deferred thunk (e.g. Call Webhook) - fires only after commit
				details.append(f"{i}. {label}: ok")
			elif result:
				# A handler may return a plain-string marker instead of a thunk (e.g. Send WhatsApp/
				# Send Email's "suppressed: sends dormant" / "sent: ..." - Task 7) - fold it into the
				# same audit line rather than a second Run Log column.
				details.append(f"{i}. {label}: ok ({result})")
			else:
				details.append(f"{i}. {label}: ok")
		frappe.db.release_savepoint(save_point)
		success = len(effect_actions)
	except Exception as e:
		frappe.db.rollback(save_point=save_point)
		deferred = []
		errors.append(f"{action.action_type}: {e}")
		details.append(f"{i}. {actions._action_label(action)}: FAILED — {e} · rule rolled back (all actions undone)")
		_log_error(rule.name, action.action_type, grain, e)

	for run_deferred in deferred:
		run_deferred()

	duration_ms = int((time.monotonic() - started) * 1000)
	_write_run_log(rule, subject, trigger_doc, grain, success, len(errors), "; ".join(errors), "\n".join(details), duration_ms)


def _run_action(action, lead, context, axes, trigger_doc):
	"""Dispatch one EFFECT-lane action by type (spec §4). Raises on failure so the caller's per-action
	guard records it — one failure never touches siblings. Never called with a guard-lane action:
	`run_effects` pre-filters its action list by `actions._ACTION_LANES` before this is reached.

	# TATVA v2 (Task 1): keys renamed to the frozen verb set (Set Field -> Update Field, Add
	# Comment -> Create Note) to match the reshaped crm_automation_action.json action_type Select —
	# a direct consequence of that schema change, not a behavior rewrite. Append/Upsert Child Row are
	# dropped from the v2 verb set (Part A) but their handlers stay wired via `actions._ACTION_LANES`
	# (unreachable via the new Select, not pruned)."""
	lane, handler = actions._ACTION_LANES.get(action.action_type, (None, None))
	if handler is None or lane != "effect":
		raise ValueError(f"unknown effect action type {action.action_type!r}")
	# A handler may return a deferred side-effect (a thunk) that must fire only if the whole rule
	# commits — the caller runs it after the savepoint is released. DB actions return None.
	return handler(action, lead, context, axes, trigger_doc)


# -- logging -----------------------------------------------------------------


def _grain_tag(vertical, group, program):
	return "{}::{}::{}".format(vertical or "", group or "", program or "")


def _write_run_log(rule, lead, trigger_doc, grain, success, failed, error, details, duration_ms):
	"""ONE Run Log row per fire (spec §8). Its own try/except — a log-write failure never breaks
	the run. `error` carries every failed action's message; `details` the per-action ok/FAILED trail
	(so a partial fire shows exactly which write landed)."""
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
				"details": details or "",
				"duration_ms": duration_ms,
			}
		).insert(ignore_permissions=True)
	except Exception:
		frappe.log_error("automation: run-log write failed")


def _log_error(rule_name, action_type, grain, err):
	"""Common error factory: a short stable title + the full structured context in the message body
	(so the tag isn't truncated into the 140-char Error Log title field, spec §8)."""
	frappe.log_error(
		title="automation: rule fire failed",
		message=f"rule={rule_name} action={action_type} grain={grain} :: {err}",
	)


# -- retention ---------------------------------------------------------------


def sweep_run_log():
	"""Scheduled job (spec §8): delete Run Log rows older than the retention window. Gated like every
	scheduled automation (invariant #6). Idempotent — a re-run over a swept window deletes nothing."""
	if not automation.is_enabled(SWEEP_SWITCH):
		return
	cutoff = frappe.utils.add_to_date(frappe.utils.now_datetime(), days=-_retention_days())
	frappe.db.delete(RUN_LOG, {"fire_time": ["<", cutoff]})


def _retention_days():
	"""v1 retention is a fixed code fallback (90 days); there is no operator field yet (the control
	doctype is a per-key registry, not a settings singleton). Honors 'sensible behaviour in a code
	fallback, never a baked form value'."""
	return DEFAULT_RETENTION_DAYS
