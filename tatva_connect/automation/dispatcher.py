"""The automation engine dispatcher — trigger → (after commit) grain fan-out → guarded executor → run log.

`fire_rules` is the v1 trigger (CRM Task on_update). It runs in the user's save and does only the
cheap, must-be-synchronous decision: is this the FIRST Done-flip of a lead-linked task (the
idempotency guard needs get_doc_before_save, which only exists during the save)? If so it ENQUEUES
`run_for_task` to run AFTER the task save COMMITS, in its own transaction. So a rule — however
malformed, slow, or deadlock-prone — can never add latency to, block, or roll back the user's task
save (spec §5.2). A DB abort in an action rolls back only the background job, never the rep's "Done".

`run_for_task` rebuilds the activity context (reconstruct_values — one brain), fans out to every
enabled rule whose grain AND trigger task type match the lead (ALL-MATCH), and runs each matched
rule's actions in a guarded executor: one action (or rule) failing never blocks siblings; every fire
writes one Run Log row; every failure also hits the common error factory. Recursion is blocked by a
request-scoped `frappe.flags.in_automation` set for the whole dispatch. Grain, context, task
creation, and the allowlist all reuse existing brains — nothing here is reimplemented.
"""
import json
import time

import frappe

from tatva_connect import automation
from tatva_connect.activity.automation import reconstruct_values
from tatva_connect.automation import rules

KILL_SWITCH = "Task::Automation::rules"
SWEEP_SWITCH = "Task::Automation::run-log-sweep"
RUN_LOG = "CRM Automation Run Log"
DEFAULT_RETENTION_DAYS = 90  # code fallback (no baked form value) — v1 has no operator field.


def fire_rules(doc, method=None):
	"""CRM Task on_update (sync): if this is the first Done-flip of a lead-linked task, enqueue the
	dispatch to run after the save commits. Dormant + fail-closed, idempotent, non-re-entrant."""
	if frappe.flags.get("in_automation"):
		return  # re-entrancy guard (request-scoped): a write the engine made must not re-enter it
	if not automation.is_enabled(KILL_SWITCH):
		return
	if doc.status != "Done":
		return
	before = doc.get_doc_before_save()
	if before and before.status == "Done":
		return  # already Done before this save — don't re-fire (idempotency)
	if doc.reference_doctype != "CRM Lead" or not doc.reference_docname:
		return
	frappe.enqueue(
		"tatva_connect.automation.dispatcher.run_for_task",
		queue="short",
		enqueue_after_commit=True,
		now=bool(frappe.flags.get("in_test")),
		task_name=doc.name,
	)


def run_for_task(task_name):
	"""Background entry (after the task save commits): rebuild context + dispatch, in our own txn.
	A deadlock/abort here can only roll back THIS job — never the user's already-committed save."""
	frappe.flags.in_automation = True
	try:
		doc = frappe.get_doc("CRM Task", task_name)
		if doc.status != "Done" or doc.reference_doctype != "CRM Lead" or not doc.reference_docname:
			return
		_dispatch(doc)
	except Exception:
		frappe.log_error(title="automation: dispatch failed", message=frappe.get_traceback())
	finally:
		frappe.flags.in_automation = False


def _dispatch(doc):
	"""Resolve the lead's grain, fan out to matching rules, run each (per-rule guarded)."""
	lead = doc.reference_docname
	axes = rules.lead_axes(lead)
	matched = rules.matching_rules(axes[0], axes[1], axes[2], doc.custom_task_type)
	if not matched:
		return
	context = build_context(doc)
	grain = _grain_tag(*axes)
	for r in matched:
		try:
			_run_rule(r, lead, context, doc, axes, grain)
		except Exception as e:
			_log_error(r.name, "(rule)", grain, e)


def build_context(doc):
	"""The value-source seam (spec §3.1): the v1 provider IS reconstruct_values. Imported, never copied."""
	return reconstruct_values(doc)


def _run_rule(r, lead, context, trigger_doc, axes, grain):
	"""Evaluate one rule's criteria; if all match, run its actions in a guarded executor and log."""
	started = time.monotonic()
	rule = frappe.get_doc("CRM Automation Rule", r.name)
	if not rules.criteria_match(rule.criteria, context):
		return  # not a fire — no log (only fires are audited)

	success = 0
	errors = []
	details = []
	for i, action in enumerate(rule.actions, 1):
		label = _action_label(action)
		try:
			_run_action(action, lead, context, axes)
			success += 1
			details.append("{0}. {1}: ok".format(i, label))
		except Exception as e:
			errors.append("{0}: {1}".format(action.action_type, e))
			details.append("{0}. {1}: FAILED — {2}".format(i, label, e))
			_log_error(rule.name, action.action_type, grain, e)

	duration_ms = int((time.monotonic() - started) * 1000)
	_write_run_log(rule, lead, trigger_doc, grain, success, len(errors), "; ".join(errors), "\n".join(details), duration_ms)


def _action_label(a):
	"""Short human label of an action for the per-action audit trail in the run log."""
	if a.action_type == "Create Task":
		return "Create Task {0}".format(a.task_type or "?")
	if a.action_type == "Set Field":
		return "Set Field {0}".format(a.fieldname or "?")
	if a.action_type in ("Append Child Row", "Upsert Child Row"):
		return "{0} {1}".format(a.action_type, a.child_table or "?")
	if a.action_type == "Call Webhook":
		return "Call Webhook {0}".format(a.webhook_endpoint or "?")
	return a.action_type or "?"


# -- actions -----------------------------------------------------------------


def _run_action(action, lead, context, axes):
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
	handler(action, lead, context, axes)


def _action_create_task(action, lead, context, axes):
	"""CREATE_TASK — reuse the idempotent follow-up helper. Grain backstop: a scoped task type may
	only be raised on a lead its scope admits, so a grain-A rule can't plant a grain-B activity type."""
	from tatva_connect.tasks.tasks import create_followup_task
	from tatva_connect.activity.api import _scope_applies

	scoped = frappe.db.exists("CRM Task Type Scope", {"parent": action.task_type, "parenttype": "CRM Task Type"})
	if scoped and not _scope_applies(action.task_type, axes[0], axes[1], axes[2]):
		raise PermissionError(f"task type {action.task_type} is not in this lead's grain")
	create_followup_task(lead=lead, task_type=action.task_type, due_at=_due_at(action, context))


def _action_set_field(action, lead, context, axes):
	"""SET_FIELD via the UNIFIED write path (spec §4.1): load the doc, set the field, save — NEVER
	frappe.db.set_value (which skips validate/hook re-mirroring). Re-checked against the enabled
	allowlist at runtime (defense in depth, spec §5.3)."""
	if not (action.target_doctype and action.fieldname):
		raise ValueError("Set Field action missing target doctype or fieldname")
	if not _allowlisted(action.target_doctype, action.fieldname, axes):
		raise PermissionError(
			f"{action.fieldname} on {action.target_doctype} not in the enabled Automatable-Field allowlist"
		)
	value = context.get(action.context_field) if action.value_mode == "From Context" else action.value
	if action.target_doctype != "CRM Lead":
		raise ValueError(f"Set Field target {action.target_doctype} is not supported (lead only)")
	tdoc = frappe.get_doc("CRM Lead", lead)
	tdoc.set(action.fieldname, value)
	tdoc.save(ignore_permissions=True)


def _action_append_child(action, lead, context, axes):
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


def _action_upsert_child(action, lead, context, axes):
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


def _action_call_webhook(action, lead, context, axes):
	"""CALL_WEBHOOK — invoke a curated native Webhook's delivery (spec §6). We don't rebuild HTTP:
	enqueue Frappe's enqueue_webhook (HMAC + 3 retries + Webhook Request Log) with the lead as payload
	context. The endpoint is picked, never typed; its URL/secret stay admin-curated."""
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


# -- value + child helpers ---------------------------------------------------


def _blank(v):
	return v is None or (isinstance(v, str) and not v.strip())


def _due_at(action, context):
	"""Resolve a Create Task due date from a context field, coercing defensively: a non-datetime
	value degrades to None (create_followup_task then applies its default lead time) rather than
	dropping the task."""
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
	"""Type-aware equality for a match key — cast both sides by the field's fieldtype (Date->getdate,
	Datetime->get_datetime, Float/Currency->flt, Int->cint, ...). Falls back to None-safe equality."""
	if df is not None:
		try:
			return frappe.utils.cast(df.fieldtype, a) == frappe.utils.cast(df.fieldtype, b)
		except Exception:
			pass
	return (a if a is not None else "") == (b if b is not None else "")


def _assert_child_allowlisted(child_dt, child_table, fields, axes, keys=None):
	"""Every set/match field must be in the enabled allowlist for the child table at the lead's grain;
	match keys must additionally be marked is_row_key. Fail-closed (spec §5.3)."""
	keys = keys or set()
	for f in fields:
		if not _allowlisted(child_dt, f, axes, child_table=child_table, require_row_key=(f in keys)):
			raise PermissionError(
				f"{f} on {child_dt} ({child_table}) is not in the enabled Automatable-Field allowlist"
			)


def _allowlisted(target_doctype, fieldname, axes, child_table=None, require_row_key=False):
	"""Runtime re-check of the fail-closed write allowlist (mirrors the rule controller), by grain.
	An enabled row whose set axes equal the lead's grain (blank axis = wildcard) authorizes the write.
	For child-row writes the row must also match the child table (and be a row-key when required)."""
	filters = {"target_doctype": target_doctype, "fieldname": fieldname, "enabled": 1}
	if child_table is not None:
		filters["child_table_field"] = child_table
	if require_row_key:
		filters["is_row_key"] = 1
	rows = frappe.get_all("CRM Automatable Field", filters=filters, fields=["vertical", "group", "program"])
	av = {"vertical": axes[0] or "", "group": axes[1] or "", "program": axes[2] or ""}
	for row in rows:
		if all((row.get(a) or "") in ("", av[a]) for a in av):
			return True
	return False


# -- logging -----------------------------------------------------------------


def _grain_tag(vertical, group, program):
	return "{0}::{1}::{2}".format(vertical or "", group or "", program or "")


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
		message="rule={r} action={a} grain={g} :: {e}".format(r=rule_name, a=action_type, g=grain, e=err),
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
