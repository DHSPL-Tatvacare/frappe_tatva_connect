"""The automation engine's guarded executor — grain-matched rule → per-action run → Run Log.

TATVA v2 (Task 4): the two v1 triggers (`fire_rules` — CRM Task on_update, Task-Completed; and
`watch.fire_field_change_rules` — CRM Lead/Task on_update, Field-Changed) are RETIRED into the ONE
wildcard event router (`automation/router.py`, keyed on `(on_doctype, event)`). This module now owns
only what's downstream of a trigger decision: the two-lane executor (Task 5) and every action
handler — router.run_guards/run_for_event call this module exactly as both old dispatchers did
(same Run Log, same allowlist recheck; no parallel brain, A.8).

TATVA v2 (Task 5): an after-commit action can DO but cannot DENY, so the executor splits into TWO
lanes, one grammar, ONE registry (`_ACTION_LANES`) declaring each verb's lane exactly once (A.8):
  - GUARD lane (`run_guards`) — runs synchronously in `validate` (router.run_guards); a guard
    handler raising propagates out of validate and BLOCKS the save (S.1/S.3 — never swallowed). No
    savepoint (nothing is written yet), no Run Log (the save may never happen).
  - EFFECT lane (`run_effects`) — runs after commit (router.run_for_event), the ORIGINAL `_run_rule`
    body: per-rule savepoint, deferred thunks, Run Log. Iterates ONLY effect-lane actions — a rule's
    guard actions already ran in validate, never re-run here.
Both lanes reuse the ONE criteria evaluator (`rules.criteria_match`) and the ONE context the router
builds — no second copy of either.
"""
import json
import time

import frappe
from frappe import _

from tatva_connect import automation
from tatva_connect.automation import fields, rules

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
		lane, handler = _ACTION_LANES.get(action.action_type, (None, None))
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

	effect_actions = [a for a in rule.actions if _ACTION_LANES.get(a.action_type, (None, None))[0] == "effect"]

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
			thunk = _run_action(action, subject, context, axes, trigger_doc)
			if thunk:
				deferred.append(thunk)
			details.append(f"{i}. {_action_label(action)}: ok")
		frappe.db.release_savepoint(save_point)
		success = len(effect_actions)
	except Exception as e:
		frappe.db.rollback(save_point=save_point)
		deferred = []
		errors.append(f"{action.action_type}: {e}")
		details.append(f"{i}. {_action_label(action)}: FAILED — {e} · rule rolled back (all actions undone)")
		_log_error(rule.name, action.action_type, grain, e)

	for run_deferred in deferred:
		run_deferred()

	duration_ms = int((time.monotonic() - started) * 1000)
	_write_run_log(rule, subject, trigger_doc, grain, success, len(errors), "; ".join(errors), "\n".join(details), duration_ms)


def _action_label(a):
	"""Short human label of an action for the per-action audit trail in the run log."""
	if a.action_type == "Create Task":
		return "Create Task {}".format(a.task_type or "?")
	if a.action_type == "Update Field":
		return "Update Field {}".format(a.fieldname or "?")
	if a.action_type in ("Append Child Row", "Upsert Child Row"):
		return "{} {}".format(a.action_type, a.child_table or "?")
	if a.action_type == "Call Webhook":
		return "Call Webhook {}".format(a.webhook_endpoint or "?")
	if a.action_type == "Create Note":
		return "Create Note"
	return a.action_type or "?"


# -- actions -----------------------------------------------------------------


def _run_action(action, lead, context, axes, trigger_doc):
	"""Dispatch one EFFECT-lane action by type (spec §4). Raises on failure so the caller's per-action
	guard records it — one failure never touches siblings. Never called with a guard-lane action:
	`run_effects` pre-filters its action list by `_ACTION_LANES` before this is reached.

	# TATVA v2 (Task 1): keys renamed to the frozen verb set (Set Field -> Update Field, Add
	# Comment -> Create Note) to match the reshaped crm_automation_action.json action_type Select —
	# a direct consequence of that schema change, not a behavior rewrite. Append/Upsert Child Row are
	# dropped from the v2 verb set (Part A) but their handlers stay wired here (unreachable via the
	# new Select, not pruned) until Task 6 formally retires them into actions.py."""
	lane, handler = _ACTION_LANES.get(action.action_type, (None, None))
	if handler is None or lane != "effect":
		raise ValueError(f"unknown effect action type {action.action_type!r}")
	# A handler may return a deferred side-effect (a thunk) that must fire only if the whole rule
	# commits — the caller runs it after the savepoint is released. DB actions return None.
	return handler(action, lead, context, axes, trigger_doc)


def _action_require_fields(action, subject, context):
	"""REQUIRE_FIELDS (guard, Task 5) — the first guard verb, exercising the guard lane end to end.
	Comma-separated fieldnames read off the rule's subject (the sync context `run_guards` built); a
	blank one blocks the save. Fail-closed, native `frappe.throw` — this raise IS the block and must
	reach `validate` unswallowed (S.1/S.3)."""
	for fieldname in (action.require_fields or "").split(","):
		fieldname = fieldname.strip()
		if not fieldname:
			continue
		if context.get(fieldname) in (None, ""):
			frappe.throw(_("Field {0} is required").format(fieldname))


def _action_create_task(action, lead, context, axes, trigger_doc):
	"""CREATE_TASK — reuse the idempotent follow-up helper. Grain backstop: a scoped task type may
	only be raised on a lead its scope admits, so a grain-A rule can't plant a grain-B activity type.
	The due date resolves from a context field (From Context) or an expression (Expression)."""
	from tatva_connect.activity.api import _scope_applies
	from tatva_connect.tasks.tasks import create_followup_task
	from tatva_connect.automation import expr

	scoped = frappe.db.exists("CRM Task Type Scope", {"parent": action.task_type, "parenttype": "CRM Task Type"})
	if scoped and not _scope_applies(action.task_type, axes[0], axes[1], axes[2]):
		raise PermissionError(f"task type {action.task_type} is not in this lead's grain")
	# Carry the completing task's assignee onto the next task (old-engine parity — otherwise the
	# follow-up lands unassigned, on no rep's list and with no assignment notification).
	create_followup_task(
		lead=lead,
		task_type=action.task_type,
		due_at=_due_at(action, context),
		assigned_to=trigger_doc.get("assigned_to"),
	)


def _action_set_field(action, lead, context, axes, trigger_doc):
	"""SET_FIELD via the UNIFIED write path: load the target doc, set the field, save — NEVER
	frappe.db.set_value (skips validate/hook re-mirroring). The target is the rule's scope — the Lead,
	or the triggering doc itself (a Field-Changed rule on a Task may set a field on that Task). Gated by
	the enabled can_set allowlist at runtime (defense in depth). The write runs inside the rule's
	savepoint, so a later action's failure rolls this back too (group atomicity)."""
	if not (action.target_doctype and action.fieldname):
		raise ValueError("Set Field action missing target doctype or fieldname")
	if not fields.is_settable(action.target_doctype, action.fieldname, axes):
		raise PermissionError(
			f"{action.fieldname} on {action.target_doctype} not in the enabled Automation-Field allowlist"
		)
	tdoc = _resolve_write_target(action, lead, trigger_doc)
	tdoc.set(action.fieldname, _resolve_set_field_value(action, context))
	tdoc.save(ignore_permissions=True)


def _resolve_write_target(action, lead_name, trigger_doc):
	"""The record a Set Field writes to. A rule's write scope is {the Lead} ∪ {the triggering doc}:
	target the Lead, or the trigger doc itself (Field-Changed on a Task → set a field on that Task).
	Any other doctype is out of scope — raise loudly rather than misfire on a name that isn't its."""
	if action.target_doctype == "CRM Lead":
		return frappe.get_doc("CRM Lead", lead_name)
	if trigger_doc is not None and action.target_doctype == trigger_doc.doctype:
		return frappe.get_doc(trigger_doc.doctype, trigger_doc.name)  # fresh load, same txn
	raise ValueError(
		f"Set Field target {action.target_doctype} is not in this rule's scope "
		f"(the Lead or the triggering {trigger_doc.doctype if trigger_doc else '—'})."
	)


def _resolve_set_field_value(action, context):
	"""One seam for the three Set Field value modes. Literal = the field as typed; From Context =
	the named context key; Expression = safe_eval against ctx (raises on a bad/missing ref so the
	rule's savepoint rolls back — no partial write)."""
	from tatva_connect.automation import expr

	if action.value_mode == "Expression":
		return expr.resolve_expression(action.expression, context)
	if action.value_mode == "From Context":
		return context.get(action.context_field)
	return action.value


def _action_add_comment(action, lead, context, axes, trigger_doc):
	"""ADD_COMMENT — a native `doc.add_comment()` on the rule's SUBJECT record (always the lead:
	a Lead trigger's subject is itself; a Task trigger's subject is its parent Lead, resolved by
	watch._subject). `add_comment` inserts a Comment row without re-saving the subject, so it cannot
	re-fire the Field-Changed dispatcher on that doc (no new re-entrancy surface)."""
	from tatva_connect.automation import expr

	if action.comment_mode == "Expression":
		text = expr.resolve_expression(action.comment_expression, context)
		if not isinstance(text, str):
			raise ValueError("Add Comment expression did not evaluate to a string")
	else:
		text = action.comment_text or ""
	if not text:
		raise ValueError("Add Comment resolved to an empty string — nothing to log")
	subject_doc = frappe.get_doc("CRM Lead", lead)
	subject_doc.add_comment("Comment", text)


def _action_append_child(action, lead, context, axes, trigger_doc):
	"""APPEND_CHILD_ROW — add a new row to a CRM Lead child table (spec §4.2), via load+save so the
	lead's hooks re-run. Every field must be allowlisted for the child doctype at the lead's grain."""
	child_table, child_dt = _child_target(action)
	values = _resolve_map(action.set_json, context)
	if not values:
		raise ValueError("Append Child Row needs a non-empty Set (JSON)")
	_assert_child_allowlisted(child_dt, child_table, set(values), axes)
	tdoc = frappe.get_doc("CRM Lead", lead)
	tdoc.append(child_table, values)
	tdoc.save(ignore_permissions=True)


def _action_upsert_child(action, lead, context, axes, trigger_doc):
	"""UPSERT_CHILD_ROW — find the row by natural key and update it, else append (spec §4.2). The
	match is type-aware (so 7=='7'==7.0 and a date literal matches a stored date), refuses to match on
	a blank key, never rewrites the key, and fails loud if the key is non-unique."""
	child_table, child_dt = _child_target(action)
	match = _resolve_map(action.match_json, context)
	values = _resolve_map(action.set_json, context)
	if not match:
		raise ValueError("Upsert Child Row needs a non-empty Match (JSON)")
	if any(_blank(v) for v in match.values()):
		raise ValueError("Upsert match key resolved to a blank value — refusing to match on blank")
	_assert_child_allowlisted(child_dt, child_table, set(match) | set(values), axes, keys=set(match))
	tdoc = frappe.get_doc("CRM Lead", lead)
	row = _find_child_row(tdoc.get(child_table), match, child_dt)
	if row:
		for k, v in values.items():
			if k in match:
				continue  # never rewrite the natural key out from under the upsert
			row.set(k, v)
	else:
		tdoc.append(child_table, {**match, **values})
	tdoc.save(ignore_permissions=True)


def _action_call_webhook(action, lead, context, axes, trigger_doc):
	"""CALL_WEBHOOK — invoke a curated native Webhook's delivery (spec §6). We don't rebuild HTTP:
	enqueue Frappe's enqueue_webhook (HMAC + 3 retries + Webhook Request Log) with the lead as payload
	context. The endpoint is picked, never typed; its URL/secret stay admin-curated."""
	if not action.webhook_endpoint:
		raise ValueError("Call Webhook action missing an endpoint")
	if not frappe.db.exists("Webhook", action.webhook_endpoint):
		raise ValueError(f"Webhook endpoint {action.webhook_endpoint!r} does not exist")
	lead_doc = frappe.get_doc("CRM Lead", lead)
	# Deferred: return the enqueue as a thunk so it fires only if the rule commits (a rolled-back
	# rule must not send its webhook — a savepoint rollback would not clear an after_commit hook).
	return lambda: frappe.enqueue(
		"frappe.integrations.doctype.webhook.webhook.enqueue_webhook",
		doc=lead_doc,
		webhook={"name": action.webhook_endpoint},
		enqueue_after_commit=True,
	)


# The ONE action-lane registry (A.8): every verb's lane is declared exactly once here, read by both
# `run_guards` (guard-lane actions) and `run_effects`/`_run_action` (effect-lane actions). Adding a
# verb = one row here, never a second lane table. `Require Fields` is the first guard verb (Task 5);
# `Require Location` (Task 8) will be the second.
_ACTION_LANES = {
	"Require Fields": ("guard", _action_require_fields),
	"Create Task": ("effect", _action_create_task),
	"Update Field": ("effect", _action_set_field),
	"Append Child Row": ("effect", _action_append_child),
	"Upsert Child Row": ("effect", _action_upsert_child),
	"Call Webhook": ("effect", _action_call_webhook),
	"Create Note": ("effect", _action_add_comment),
}


# -- value + child helpers ---------------------------------------------------


def _blank(v):
	return v is None or (isinstance(v, str) and not v.strip())


def _due_at(action, context):
	"""Resolve a Create Task due date, coercing defensively: a non-datetime value degrades to None
	(create_followup_task then applies its default lead time) rather than dropping the task. Two
	modes — From Context (read a context key) and Expression (safe_eval against ctx)."""
	from tatva_connect.automation import expr

	if action.due_mode == "Expression":
		raw = expr.resolve_expression(action.due_expression, context)
	else:  # From Context (the v1 default; also the pre-Expression behavior)
		if not action.due_from:
			return None
		raw = context.get(action.due_from)
	if raw is None:
		return None
	try:
		return frappe.utils.get_datetime(raw)
	except Exception:
		return None


def _resolve(spec, context):
	"""A value is literal unless it starts with `$ctx.` — then it's pulled from the activity context."""
	if isinstance(spec, str) and spec.startswith("$ctx."):
		return context.get(spec[5:])
	return spec


def _resolve_map(raw, context):
	"""Parse a {fieldname: value} JSON map and resolve each value (literal | $ctx.<field>)."""
	if not (raw or "").strip():
		return {}
	try:
		data = json.loads(raw)  # ALLOWLIST 2026-06-29: keep raw — the except gives a precise "invalid JSON" error; parse_json won't raise.
	except (ValueError, TypeError):
		raise ValueError("invalid JSON in a child-row action")
	if not isinstance(data, dict):
		raise ValueError("child-row action JSON must be an object")
	return {k: _resolve(v, context) for k, v in data.items()}


def _child_target(action):
	"""(child_table fieldname, child doctype). The child table must be a Table field on CRM Lead."""
	if not action.child_table:
		raise ValueError("child-row action missing Child Table")
	field = frappe.get_meta("CRM Lead").get_field(action.child_table)
	if not field or field.fieldtype != "Table":
		raise ValueError(f"{action.child_table!r} is not a child table on CRM Lead")
	return action.child_table, field.options


def _find_child_row(rows, match, child_dt):
	"""Type-aware row match (spec §4.2). Casts both sides by the child field's fieldtype so a Date
	literal matches a stored Date/Datetime and 7/'7'/7.0 collapse — preventing dup-append. Raises if
	the key matches more than one row (a key marked is_row_key that isn't actually unique)."""
	meta = frappe.get_meta(child_dt)
	hits = [row for row in (rows or []) if all(_eq(row.get(k), v, meta.get_field(k)) for k, v in match.items())]
	if len(hits) > 1:
		raise ValueError(f"upsert key matched {len(hits)} rows in {child_dt} — not a unique row key")
	return hits[0] if hits else None


def _eq(a, b, df):
	"""Type-aware equality for a match key - delegates to the ONE shared comparator
	(rules._eq_typed) so before/after diff semantics (watch._diff_watched_fields) and
	changed_from_to (rules._one_match) and upsert row-matching all share one brain. Casts both
	sides by the field's fieldtype (Date->getdate, Datetime->get_datetime, Float/Currency->flt,
	Int->cint, ...). Falls back to None-safe equality on an uncastable value."""
	from tatva_connect.automation.rules import _eq_typed
	return _eq_typed(a, b, df.fieldtype if df is not None else None)


def _assert_child_allowlisted(child_dt, child_table, fieldnames, axes, keys=None):
	"""Every set/match field must be an enabled can_set row for the child table at the lead's grain;
	match keys must additionally be is_row_key. Fail-closed. One allowlist brain (fields.is_settable)."""
	keys = keys or set()
	for f in fieldnames:
		if not fields.is_settable(child_dt, f, axes, child_table_field=child_table, require_row_key=(f in keys)):
			raise PermissionError(
				f"{f} on {child_dt} ({child_table}) is not in the enabled Automation-Field allowlist"
			)


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
