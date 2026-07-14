"""The automation engine's verb handlers — every GUARD/EFFECT action body + its resolver helpers +
the `_ACTION_LANES` registry (TATVA v2, Task 6).

Extracted from `dispatcher.py` (Task 5 co-located the verb bodies with the two-lane executor for the
initial split; Task 6 finishes the separation so dispatcher.py owns only orchestration — run_guards/
run_effects/`_run_action` dispatch/Run Log/error factory — and this module owns every verb's
implementation). A move, not a rewrite (A.8/A.12) — behavior, docstrings and security annotations are
unchanged from their dispatcher.py originals. `dispatcher.py` imports this module for `_ACTION_LANES`
and `_action_label`; nothing here imports `dispatcher` (the executor depends on the verbs, never the
reverse — no circular import).
"""
import json

import frappe
from frappe import _
from frappe.utils import flt

from tatva_connect.automation import fields
from tatva_connect.taxonomy import labels


def _action_label(a):
	"""Short human label of an action for the per-action audit trail in the run log."""
	if a.action_type == "Create Task":
		# The run log is read by an operator, so name the type, not its composite PK.
		return "Create Task {}".format(labels.label(a.task_type, labels.TASK_TYPE) or "?")
	if a.action_type == "Update Field":
		return "Update Field {}".format(a.fieldname or "?")
	if a.action_type in ("Append Child Row", "Upsert Child Row"):
		return "{} {}".format(a.action_type, a.child_table or "?")
	if a.action_type == "Call Webhook":
		return "Call Webhook {}".format(a.webhook_endpoint or "?")
	if a.action_type == "Create Note":
		return "Create Note"
	if a.action_type == "Send WhatsApp":
		return "Send WhatsApp {}".format(a.whatsapp_template or "?")
	if a.action_type == "Send Email":
		return "Send Email {}".format(a.email_recipient or "?")
	if a.action_type == "Wait":
		return "Wait {}".format(a.wait_expression or "?")
	return a.action_type or "?"


class _ParkSignal(Exception):
	"""Raised by the WAIT verb (effect lane, Task 9) — not a failure, a SEGMENT BOUNDARY. The handler
	never parks anything itself: it only resolves the wake time and raises this; the executor
	(`dispatcher.run_effects`) owns the action list and this action's position in it, so it is the one
	that catches the signal, commits the pre-wait segment, and calls `resume.park()` with the index of
	the action AFTER the Wait.

	Carries `parked_at` — the exact instant `resume_at` was derived FROM — so the queue stores the input
	alongside the answer. Re-deriving a wake time later (a Wait's delay edited under a sleeping lead)
	then lands on the same arithmetic, to the microsecond."""

	def __init__(self, parked_at, resume_at):
		self.parked_at = parked_at
		self.resume_at = resume_at
		super().__init__(f"wait: parked at {parked_at} until {resume_at}")


# -- actions -----------------------------------------------------------------


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


def _action_require_location(action, subject, context):
	"""REQUIRE_LOCATION (guard, Task 8) — the second guard verb, same shape as Require Fields but
	delegating the required-decision to the ONE existing brain (location.api.location_required, A.8) —
	never a second copy of that logic. `subject` is the lead the router's guard lane resolved (the
	rule's subject); `context` is the triggering doc's own field dict (the same sync context Require
	Fields reads), so a rule On CRM Task Updated reads its custom_task_type / custom_location_latitude /
	custom_location_longitude straight off it. Fail-closed, native `frappe.throw` — same message intent
	as the `tasks.enforce_location` hook this verb supersedes once a rule is authored for a task type."""
	from tatva_connect.location import api as location_api

	radius = location_api.location_required(context.get("custom_task_type"), subject, context)
	if radius is None:
		return  # not required for this task type / submitted values — same non-match as the old hook
	lat, lng = context.get("custom_location_latitude"), context.get("custom_location_longitude")
	if not (lat and lng):
		frappe.throw(
			_("Capture your location at the doctor's site to complete this visit — mark it Done from the "
			  "Tasks list or open the task."),
			title=_("Location required"),
		)
	geofence = flt(action.geofence_meters)
	if geofence <= 0:
		return  # no radius configured on this action — the location_required check above is enough
	site_lat, site_lng = frappe.db.get_value(
		"CRM Lead", subject, ["custom_clinic_latitude", "custom_clinic_longitude"]
	) or (None, None)
	if not (site_lat and site_lng):
		return  # no clinic anchor yet to measure against — nothing to enforce a radius on
	distance = location_api.haversine(flt(lat), flt(lng), site_lat, site_lng)
	if distance > geofence:
		frappe.throw(
			_("You are {0} m from the doctor's location — outside the allowed {1} m geofence.").format(
				round(distance), geofence
			),
			title=_("Out of range"),
		)


def _action_create_task(action, lead, context, axes, trigger_doc):
	"""CREATE_TASK — reuse the idempotent follow-up helper. Grain backstop: a scoped task type may
	only be raised on a lead its scope admits, so a grain-A rule can't plant a grain-B activity type.
	The due date resolves from a context field (From Context) or an expression (Expression).

	A File / WhatsApp Message trigger carries no assignee, so the follow-up would land unassigned (on
	no rep's list, no assignment notification): fall back to the lead's owner. When the trigger is a
	File and the raised type is Document Review, pin the file onto the review task and mark the File
	Pending + linked (the review flow's on-upload step)."""
	from tatva_connect.activity.api import _scope_applies
	from tatva_connect.tasks.tasks import create_followup_task

	scoped = frappe.db.exists("CRM Task Type Scope", {"parent": action.task_type, "parenttype": "CRM Task Type"})
	if scoped and not _scope_applies(action.task_type, axes[0], axes[1], axes[2]):
		raise PermissionError(f"task type {action.task_type} is not in this lead's grain")
	# Carry the completing task's assignee onto the next task (old-engine parity). Only a trigger that
	# genuinely has no assignee field — a File / WhatsApp Message — falls back to the lead owner so its
	# task is never orphaned; a Lead- or Task-triggered rule keeps producing an unassigned task for the
	# native Assignment Rule to route (do NOT force lead_owner on those — it defeats the Assignment Rule).
	assignee = trigger_doc.get("assigned_to") if trigger_doc else None
	if not assignee and trigger_doc is not None and trigger_doc.doctype in ("File", "WhatsApp Message"):
		assignee = frappe.db.get_value("CRM Lead", lead, "lead_owner")
	# Review flow: a File that raises a Document Review task gets its OWN task, one per document — the
	# verdict is per-document, so it must never ride the per-lead-per-type throttle (which would collapse
	# several reviewable files onto one task and mirror one verdict onto all). The File back-reference is
	# the idempotency key: a re-fire on the same File reuses its open review task. Every other Create Task
	# rule keeps the idempotent per-lead-per-type helper unchanged.
	is_review = (
		trigger_doc is not None
		and trigger_doc.doctype == "File"
		and frappe.db.get_value("CRM Task Type", action.task_type, "type_name") == "Document Review"
	)
	if is_review:
		_pin_review_file(_review_task_for_file(trigger_doc.name, lead, action, context, assignee), trigger_doc.name)
		return
	create_followup_task(
		lead=lead,
		task_type=action.task_type,
		due_at=_due_at(action, context),
		assigned_to=assignee,
	)


def _review_task_for_file(file_name, lead, action, context, assignee):
	"""The review task for one File — per document, not per lead+type (review flow, spec §4.2/§13).
	Idempotent on the File's own back-reference: if this File already links to an open review task,
	reuse it; otherwise raise a fresh one with the per-lead-per-type throttle OFF so a second reviewable
	document on the same lead gets its own task instead of collapsing onto the first."""
	from tatva_connect.tasks.tasks import CLOSED_STATUSES, create_followup_task

	existing = frappe.db.get_value("File", file_name, "custom_review_task")
	if existing and frappe.db.get_value("CRM Task", existing, "status") not in CLOSED_STATUSES:
		return existing  # this document already has an open review task — idempotent re-fire
	return create_followup_task(
		lead=lead,
		task_type=action.task_type,
		due_at=_due_at(action, context),
		assigned_to=assignee,
		throttle=False,
	)


def _pin_review_file(task_name, file_name):
	"""Pin a File onto its Document Review task and back-link it (review flow, spec §4.2). Both writes
	go through the unified get_doc/save path (never db.set_value — that skips validate/mirroring) and
	are idempotent: the document Attach schema value lands in the task's JSON payload under its
	fieldname (`document`), and the File is stamped Pending + custom_review_task, each written only
	when it actually changes so a re-fire is a no-op."""
	file_doc = frappe.get_doc("File", file_name)
	task = frappe.get_doc("CRM Task", task_name)
	payload = frappe.parse_json(task.custom_activity_payload) if (task.custom_activity_payload or "").strip() else {}
	if payload.get("document") != file_doc.file_url:
		payload["document"] = file_doc.file_url
		task.custom_activity_payload = frappe.as_json(payload)
		task.save(ignore_permissions=True)  # authz-ok: tier-a — automation effect lane (after-commit); rules are operator-built
	if file_doc.custom_review_status != "Pending" or file_doc.custom_review_task != task_name:
		file_doc.custom_review_status = "Pending"
		file_doc.custom_review_task = task_name
		file_doc.save(ignore_permissions=True)  # authz-ok: tier-a — automation effect lane (after-commit); rules are operator-built


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
	tdoc.save(ignore_permissions=True)  # authz-ok: tier-a — automation effect lane (after-commit); rules are operator-built


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
	tdoc.save(ignore_permissions=True)  # authz-ok: tier-a — automation effect lane (after-commit); rules are operator-built


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
	tdoc.save(ignore_permissions=True)  # authz-ok: tier-a — automation effect lane (after-commit); rules are operator-built


def _action_call_webhook(action, lead, context, axes, trigger_doc):
	"""CALL_WEBHOOK — invoke a curated native Webhook's delivery (spec §6). We don't rebuild HTTP:
	enqueue Frappe's enqueue_webhook (HMAC + 3 retries + Webhook Request Log) with the payload doc as
	context. The endpoint is picked, never typed; its URL/secret stay admin-curated.

	`webhook_payload_source` chooses what rides the body: the Lead (default — every existing rule is
	unchanged) or the Trigger Doc (the record that fired the rule, e.g. a Document Review task, so the
	disposition + reason go out). Trigger Doc degrades to the lead only when there is no trigger doc."""
	if not action.webhook_endpoint:
		raise ValueError("Call Webhook action missing an endpoint")
	if not frappe.db.exists("Webhook", action.webhook_endpoint):
		raise ValueError(f"Webhook endpoint {action.webhook_endpoint!r} does not exist")
	if (action.webhook_payload_source or "Lead") == "Trigger Doc" and trigger_doc is not None:
		payload_doc = frappe.get_doc(trigger_doc.doctype, trigger_doc.name)  # fresh load, same txn
	else:
		payload_doc = frappe.get_doc("CRM Lead", lead)
	# Deferred: return the enqueue as a thunk so it fires only if the rule commits (a rolled-back
	# rule must not send its webhook — a savepoint rollback would not clear an after_commit hook).
	return lambda: frappe.enqueue(
		"frappe.integrations.doctype.webhook.webhook.enqueue_webhook",
		doc=payload_doc,
		webhook={"name": action.webhook_endpoint},
		enqueue_after_commit=True,
	)


def _action_send_whatsapp(action, lead, context, axes, trigger_doc):
	"""SEND_WHATSAPP (effect, Task 7) — the dormant sends gate. `sends.send_whatsapp` records the
	fire behind `Task::Automation::sends` (OFF by default, A.6) and, once the operator flips it,
	sends through the EXISTING WATI brain (grain-routed account + template, A.11/A.8). This handler
	only resolves the action's config off the rule row; no adapter logic lives here."""
	from tatva_connect.automation import sends

	return sends.send_whatsapp(lead, action.whatsapp_template, context)


def _action_send_email(action, lead, context, axes, trigger_doc):
	"""SEND_EMAIL (effect, Task 7) — same dormant gate as Send WhatsApp; live sends go through
	native `frappe.sendmail` (A.18), never a hand-rolled mail path."""
	from tatva_connect.automation import sends

	return sends.send_email(lead, action.email_recipient, action.email_subject, action.email_body, context)


def wait_resume_at(wait_expression, context, base):
	"""The ONE Wait-delay resolver (A.8). `wait_expression` is a Python expression (safe_eval via the
	ONE resolver, expr.resolve_expression) that must evaluate to a non-empty dict of
	`frappe.utils.add_to_date` kwargs — e.g. `{"days": 14}`, `{"months": 1}`. This is the ONE clear
	contract for the verb (a fixed delay, never a raw datetime literal, so authoring stays declarative
	and testable). Anything else — a non-dict, an empty dict, or a dict `add_to_date` rejects — raises
	loudly; a Wait can never silently resolve to a zero-length (or nonexistent) delay.

	Returns `base` shifted by that delay. Three callers, one arithmetic: the Wait verb (base = now),
	`versions` rescheduling a parked execution after its Wait's delay was edited (base = when it
	parked), and the rule's author-time validator (base = now, empty context) — so a rescheduled wake
	time can never diverge from a freshly computed one."""
	from tatva_connect.automation import expr

	delay = expr.resolve_expression(wait_expression, context)
	if not isinstance(delay, dict) or not delay:
		frappe.throw(
			_("A Wait action's expression must evaluate to a non-empty dict of add_to_date kwargs, "
			  'e.g. {{"days": 14}} — got {0!r}.').format(delay),
			title=_("Bad Wait expression"),
		)
	try:
		return frappe.utils.add_to_date(base, **delay)
	except TypeError as e:
		frappe.throw(
			_("A Wait action's expression dict is not valid add_to_date kwargs: {0}").format(e),
			title=_("Bad Wait expression"),
		)


def _action_wait(action, lead, context, axes, trigger_doc):
	"""WAIT (effect, Task 9) — a SEGMENT BOUNDARY, not a write. Never writes anything and never parks
	anything itself: it raises `_ParkSignal`, which `run_effects` catches to do the actual parking. The
	delay contract lives in `wait_resume_at` (the one resolver)."""
	parked_at = frappe.utils.now_datetime()
	raise _ParkSignal(parked_at, wait_resume_at(action.wait_expression, context, parked_at))


# The ONE action-lane registry (A.8): every verb's lane is declared exactly once here, read by both
# `run_guards` (guard-lane actions) and `run_effects`/`_run_action` (effect-lane actions). Adding a
# verb = one row here, never a second lane table. `Require Fields` is the first guard verb (Task 5);
# `Require Location` (Task 8) is the second. `CRMAutomationRule.validate()` rejects any action_type
# not present here at author time (Task 8) — a verb sitting in the Select with no row here (e.g. Wait,
# before Task 9) can never reach a rule.
_ACTION_LANES = {
	"Require Fields": ("guard", _action_require_fields),
	"Require Location": ("guard", _action_require_location),
	"Create Task": ("effect", _action_create_task),
	"Update Field": ("effect", _action_set_field),
	"Append Child Row": ("effect", _action_append_child),
	"Upsert Child Row": ("effect", _action_upsert_child),
	"Call Webhook": ("effect", _action_call_webhook),
	"Create Note": ("effect", _action_add_comment),
	"Send WhatsApp": ("effect", _action_send_whatsapp),
	"Send Email": ("effect", _action_send_email),
	"Wait": ("effect", _action_wait),
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
