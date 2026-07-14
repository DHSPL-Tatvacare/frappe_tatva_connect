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

TATVA v2 (Task 9): `run_effects` is SEGMENT-aware. A `Wait` action (actions._action_wait) is a segment
boundary, not a write: it raises `actions._ParkSignal`, which this loop catches to commit everything the
segment already did, hand the remainder off to `resume.park()`, and RETURN — no Wait means no behaviour
change (one segment, one Run Log row). `cursor` lets the scheduled `resume.sweep_resume()` job resume a
parked segment through the SAME executor (A.8 — never a second one); a resumed run may hit ANOTHER Wait
and re-park (chained delays).

The DEFINITION comes from an immutable `CRM Automation Rule Version` (automation.versions), never from
the mutable rule. That is what makes `cursor` — a plain index — correct: it indexes a list that cannot
change underneath a parked execution. Editing a rule can therefore never re-route, duplicate or orphan a
lead already running it. `run_effects` REPORTS its outcome (`parked`/`failed`/`success`) so the queue can
close a row honestly; a failed segment must never read as Done.

HONEST CONSTRAINT: atomicity is per-SEGMENT (the effects between two Waits, or before the first / after
the last), NEVER across a Wait — you cannot hold one DB transaction open for two weeks. A savepoint
rollback inside one segment never touches an already-committed earlier segment, and a multi-Wait rule
is NOT one atomic unit end to end (same posture as Salesforce/LeadSquared scheduled paths). Do not read
"a rule is all-or-nothing" (below) as spanning a Wait — it describes ONE segment only.
"""
import time

import frappe

from tatva_connect import automation
from tatva_connect.automation import actions, rules, versions
from tatva_connect.automation.resume import RESUME_DT

SWEEP_SWITCH = "Task::Automation::run-log-sweep"
RUN_LOG = "CRM Automation Run Log"
DEFAULT_RETENTION_DAYS = 90  # code fallback (no baked form value) — v1 has no operator field.


def _lane(action):
	"""A verb's lane comes from `actions._ACTION_LANES` — CODE, not data. So a frozen definition carries
	BOTH lanes and each executor filters for its own."""
	return actions._ACTION_LANES.get(action.action_type, (None, None))[0]


def run_guards(subject, rule_version, context, field_types):
	"""GUARD lane (Task 5) — evaluate one rule's criteria; if they match, run every GUARD-lane action
	synchronously. A handler raising propagates straight out (no try/except here) — that raise IS the
	block, and it must reach `validate` unswallowed (S.1/S.3). No savepoint, no Run Log: nothing has
	been written yet and the save may never happen.

	Reads the same frozen definition the effect lane will, so a rule edited between the two lanes of one
	request can never split them across two programs."""
	definition = versions.load(rule_version)
	if not rules.criteria_match(definition.criteria, context, field_types):
		return  # not a fire — same non-match semantics as the effect lane
	for action in definition.actions:
		lane, handler = actions._ACTION_LANES.get(action.action_type, (None, None))
		if lane == "guard":
			handler(action, subject, context)


def run_effects(subject, rule_version, trigger_doc, axes, grain, field_types, context, cursor=0):
	"""EFFECT lane (Task 5) — the per-execution executor: evaluate criteria once more (the after-commit
	context can differ from the sync one — e.g. a rapid A→B→C edit, spec §5.2), then run every
	EFFECT-lane action in a guarded, savepoint-atomic executor and write one Run Log row. Guard actions
	already ran (or blocked the save) in validate — never re-run here.

	The definition comes from an IMMUTABLE `CRM Automation Rule Version`, never from the mutable rule —
	so `cursor`, a plain index into `definition.actions`, indexes a list that cannot change underneath a
	parked execution. It counts EVERY action (both lanes), not just the effect ones: one list, one index,
	one meaning. Guard-lane entries are skipped as we walk.

	`cursor` is 0 for a first fire, or a parked execution's own cursor when `resume.sweep_resume()`
	resumes it. A resume SKIPS the criteria re-check below: they already matched at the ORIGINAL fire
	(that is why this execution parked), and the context a resume replays is the one captured then, not
	a fresh one to re-judge.

	Returns `_dict(parked, failed, success)` — the resume queue closes a row off this report, so a
	failed segment can never read as Done."""
	started = time.monotonic()
	definition = versions.load(rule_version)
	if cursor == 0 and not rules.criteria_match(definition.criteria, context, field_types):
		return frappe._dict(parked=False, failed=False, success=0)  # not a fire — no log (only fires are audited)

	# A SEGMENT is all-or-nothing: run every action inside a savepoint; if ANY action fails, roll the
	# whole segment back so no lead is left half-processed. Deferred side-effects (webhooks) fire only
	# after a clean commit. Native savepoint API — no hand-rolled transaction handling. A Wait action
	# ends the segment here (not a failure — see actions._ParkSignal / the module docstring's HONEST
	# CONSTRAINT): everything up to and including the Wait itself is kept, the rest is parked.
	success = 0
	errors = []
	details = []
	deferred = []
	parked = None
	idx, action, label = cursor, None, ""
	save_point = f"tc_auto_rule_{frappe.generate_hash(length=8)}"
	frappe.db.savepoint(save_point)
	try:
		for idx in range(cursor, len(definition.actions)):
			action = definition.actions[idx]
			if _lane(action) != "effect":
				continue  # a guard-lane action ran synchronously in validate
			# Built BEFORE the action runs: it reads the DB, and the except branch below runs after a
			# rollback, where a second query can raise and lose the real error with it.
			label = actions._action_label(action)
			result = _run_action(action, subject, context, axes, trigger_doc)
			success += 1
			if callable(result):
				deferred.append(result)  # a deferred thunk (e.g. Call Webhook) - fires only after commit
				details.append(f"{idx + 1}. {label}: ok")
			elif result:
				# A handler may return a plain-string marker instead of a thunk (e.g. Send WhatsApp/
				# Send Email's "suppressed: sends dormant" / "sent: ..." - Task 7) - fold it into the
				# same audit line rather than a second Run Log column.
				details.append(f"{idx + 1}. {label}: ok ({result})")
			else:
				details.append(f"{idx + 1}. {label}: ok")
		frappe.db.release_savepoint(save_point)
	except actions._ParkSignal as signal:
		# The Wait handler raised instead of writing — release (never rollback) so every effect that
		# already ran THIS segment stays. The Wait itself counts as run; the execution resumes at the
		# action after it.
		frappe.db.release_savepoint(save_point)
		success += 1
		parked = frappe._dict(
			parked_at=signal.parked_at, resume_at=signal.resume_at, cursor=idx + 1, label=label
		)
	except Exception as e:
		frappe.db.rollback(save_point=save_point)
		deferred = []
		success = 0  # the whole segment rolled back, NOTHING durably ran, so the outcome is Failed, not Partial
		errors.append(f"{action.action_type}: {e}")
		details.append(f"{idx + 1}. {label}: FAILED: {e} · segment rolled back (all its actions undone)")
		_log_error(definition.rule, action.action_type, grain, e)

	if parked:
		# Schedule the remainder BEFORE the Run Log write, and let a failure propagate: the log must
		# never claim "parked" while no queue row exists behind it. If park raises, no log is written
		# here and the caller (router.run_for_event / resume._resume_one) records the failure through the
		# handler it already owns — no second, hand-rolled error path.
		from tatva_connect.automation import resume

		resume.park(
			rule=definition.rule, rule_version=rule_version, subject=subject, resume_at=parked.resume_at,
			cursor=parked.cursor, context=context, parked_at=parked.parked_at,
		)
		details.append(f"{parked.cursor}. {parked.label}: ok (parked, resuming {parked.resume_at})")

	for run_deferred in deferred:
		run_deferred()

	duration_ms = int((time.monotonic() - started) * 1000)
	_write_run_log(
		definition.rule, rule_version, subject, trigger_doc, grain,
		success, len(errors), "; ".join(errors), "\n".join(details), duration_ms,
	)

	return frappe._dict(parked=bool(parked), failed=bool(errors), success=success)


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


def _write_run_log(rule, rule_version, lead, trigger_doc, grain, success, failed, error, details, duration_ms):
	"""ONE Run Log row per SEGMENT (spec §8). Its own try/except — a log-write failure never breaks the
	run. `error` carries every failed action's message; `details` the per-action ok/FAILED trail (so a
	partial fire shows exactly which write landed). `rule_version` answers "which program produced this
	outcome" — a rule edited since the fire no longer explains its own history without it."""
	outcome = "Success" if not failed else ("Partial" if success else "Failed")
	try:
		frappe.get_doc(
			{
				"doctype": RUN_LOG,
				"fire_time": frappe.utils.now_datetime(),
				"rule": rule,
				"rule_version": rule_version,
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
		).insert(ignore_permissions=True)  # authz-ok: tier-a — automation engine, scheduler/queue context
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
	"""Scheduled job (spec §8): prune the engine's finished history past the retention window. Gated like
	every scheduled automation (invariant #6). Idempotent — a re-run over a swept window deletes nothing.

	Three tiers, in dependency order so a version is only ever dropped once nothing references it:
	  1. Run Log rows older than the window.
	  2. TERMINAL resume rows (Done/Failed/Cancelled) parked before the window. Pending rows are the
	     live queue and are never touched, however old — a six-month Wait is not stale data.
	  3. Rule versions no execution and no surviving Run Log still points at. Versions therefore outlive
	     their own audit trail and no longer, and a rule that has ever fired stays deletable."""
	if not automation.is_enabled(SWEEP_SWITCH):
		return
	cutoff = frappe.utils.add_to_date(frappe.utils.now_datetime(), days=-_retention_days())
	frappe.db.delete(RUN_LOG, {"fire_time": ["<", cutoff]})
	frappe.db.delete(RESUME_DT, {"status": ["!=", "Pending"], "parked_at": ["<", cutoff]})
	_purge_unreferenced_versions()


def _purge_unreferenced_versions():
	"""Delete every rule version that no execution and no surviving Run Log points at.

	A LIVE rule's current version is never a candidate, however cold — it is what the next fire binds to,
	and a rule that has never fired would otherwise lose its definition. But `is_current` protects a rule,
	not a ghost: a version whose rule is gone is reclaimable even if it was never retired (`on_trash` does
	that, yet a raw SQL delete bypasses the lifecycle). Set-difference in Python over indexed `pluck` reads
	— no correlated subquery, no raw SQL (A.18/S.2)."""
	referenced = set(frappe.get_all(RESUME_DT, pluck="rule_version", distinct=True))
	referenced |= set(frappe.get_all(RUN_LOG, pluck="rule_version", distinct=True))
	live_rules = set(frappe.get_all("CRM Automation Rule", pluck="name"))
	for row in frappe.get_all(versions.DOCTYPE, fields=["name", "rule", "is_current"]):
		if row.name in referenced or (row.is_current and row.rule in live_rules):
			continue
		frappe.delete_doc(versions.DOCTYPE, row.name, ignore_permissions=True, delete_permanently=True)  # authz-ok: tier-a — automation engine, scheduler/queue context


def _retention_days():
	"""v1 retention is a fixed code fallback (90 days); there is no operator field yet (the control
	doctype is a per-key registry, not a settings singleton). Honors 'sensible behaviour in a code
	fallback, never a baked form value'."""
	return DEFAULT_RETENTION_DAYS
