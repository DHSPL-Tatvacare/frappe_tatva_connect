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
import json
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

	try:
		_dispatch(doc)
	except Exception:
		# Engine-level failure (grain/criteria/etc.) must never abort the user's task save (spec §5.2).
		frappe.log_error("automation: dispatch failed")


def _dispatch(doc):
	"""Resolve the lead's grain, fan out to matching rules, run each (per-rule guarded)."""
	lead = doc.reference_docname
	vertical, group, program = rules.lead_axes(lead)
	matched = rules.matching_rules(vertical, group, program, doc.custom_task_type)
	if not matched:
		return
	context = build_context(doc)
	grain = _grain_tag(vertical, group, program)
	for r in matched:
		try:
			_run_rule(r, lead, context, doc, grain=grain)
		except Exception as e:
			_log_error(r.name, "(rule)", grain, e)


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
	"""Dispatch one action by type (spec §4). Raises on failure so the caller's per-action guard
	records it — one failure never touches siblings."""
	handler = {
		"Create Task": _action_create_task,
		"Set Field": _action_set_field,
		"Append Child Row": _action_append_child,
		"Upsert Child Row": _action_upsert_child,
		"Call Webhook": _action_call_webhook,
	}.get(action.action_type)
	if handler is None:
		raise ValueError(f"unknown action type {action.action_type!r}")
	handler(action, lead, context)


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

	if action.target_doctype != "CRM Lead":
		# v1 SET_FIELD targets the trigger's lead. A non-lead target would need its own resolution
		# (Phase 2 child-row work covers child tables) — reject rather than guess.
		raise ValueError(f"Set Field target {action.target_doctype} is not supported (lead only)")
	tdoc = frappe.get_doc("CRM Lead", lead)
	tdoc.set(action.fieldname, value)
	tdoc.flags.in_automation = True
	tdoc.save(ignore_permissions=True)


def _action_append_child(action, lead, context):
	"""APPEND_CHILD_ROW — add a new row to a CRM Lead child table (spec §4.2), via the unified
	load+save path so the lead's hooks (headline sync etc.) re-run."""
	child_table, child_dt = _child_target(action)
	values = _resolve_map(action.set_json, context)
	if not values:
		raise ValueError("Append Child Row needs a non-empty Set (JSON)")
	_assert_child_allowlisted(child_dt, child_table, set(values), lead)
	tdoc = frappe.get_doc("CRM Lead", lead)
	tdoc.append(child_table, values)
	tdoc.flags.in_automation = True
	tdoc.save(ignore_permissions=True)


def _action_upsert_child(action, lead, context):
	"""UPSERT_CHILD_ROW — find the row by natural key (match) and update it, else append (spec §4.2).
	Drug Program keyed on cycle_date is the canonical case."""
	child_table, child_dt = _child_target(action)
	match = _resolve_map(action.match_json, context)
	values = _resolve_map(action.set_json, context)
	if not match:
		raise ValueError("Upsert Child Row needs a non-empty Match (JSON)")
	_assert_child_allowlisted(child_dt, child_table, set(match) | set(values), lead, keys=set(match))
	tdoc = frappe.get_doc("CRM Lead", lead)
	row = _find_child_row(tdoc.get(child_table), match)
	if row:
		for k, v in values.items():
			row.set(k, v)
	else:
		tdoc.append(child_table, {**match, **values})
	tdoc.flags.in_automation = True
	tdoc.save(ignore_permissions=True)


def _action_call_webhook(action, lead, context):
	"""CALL_WEBHOOK — invoke a curated native Webhook's delivery (spec §6). We don't rebuild HTTP:
	enqueue Frappe's own enqueue_webhook (HMAC sign + 3 retries + Webhook Request Log) with the lead
	as payload context. The endpoint is picked, never typed; its URL/secret stay admin-curated."""
	if not action.webhook_endpoint:
		raise ValueError("Call Webhook action missing an endpoint")
	if not frappe.db.exists("Webhook", action.webhook_endpoint):
		raise ValueError(f"Webhook endpoint {action.webhook_endpoint!r} does not exist")
	lead_doc = frappe.get_doc("CRM Lead", lead)
	frappe.enqueue(
		"frappe.integrations.doctype.webhook.webhook.enqueue_webhook",
		doc=lead_doc,
		webhook={"name": action.webhook_endpoint},
		enqueue_after_commit=True,
	)


# -- child-row + value helpers -----------------------------------------------


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
		data = json.loads(raw)
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


def _find_child_row(rows, match):
	for row in rows or []:
		if all(str(row.get(k) or "") == str(v or "") for k, v in match.items()):
			return row
	return None


def _assert_child_allowlisted(child_dt, child_table, fields, lead, keys=None):
	"""Every set/match field must be in the enabled allowlist for the child table at the lead's grain;
	match keys must additionally be marked is_row_key. Fail-closed (spec §5.3)."""
	keys = keys or set()
	for f in fields:
		if not _allowlisted(child_dt, f, lead, child_table=child_table, require_row_key=(f in keys)):
			raise PermissionError(
				f"{f} on {child_dt} ({child_table}) is not in the enabled Automatable-Field allowlist"
			)


def _allowlisted(target_doctype, fieldname, lead, child_table=None, require_row_key=False):
	"""Runtime re-check of the fail-closed write allowlist (mirrors the rule controller, by grain).
	An enabled row whose set axes equal the lead's grain (blank axis = wildcard) authorizes the write.
	For child-row writes the row must also match the child table (and be a row-key when required)."""
	vertical, group, program = rules.lead_axes(lead)
	filters = {"target_doctype": target_doctype, "fieldname": fieldname, "enabled": 1}
	if child_table is not None:
		filters["child_table_field"] = child_table
	if require_row_key:
		filters["is_row_key"] = 1
	rows = frappe.get_all(
		"CRM Automatable Field",
		filters=filters,
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
