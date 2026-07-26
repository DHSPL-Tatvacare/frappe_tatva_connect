"""The automation engine's verb handlers — every GUARD/EFFECT action body + its resolver helpers +
the `VERBS` declaration.

Extracted from `dispatcher.py` (Task 5 co-located the verb bodies with the two-lane executor for the
initial split; Task 6 finishes the separation so dispatcher.py owns only orchestration — run_guards/
run_effects/`_run_action` dispatch/Run Log/error factory — and this module owns every verb's
implementation). A move, not a rewrite (A.8/A.12) — behavior, docstrings and security annotations are
unchanged from their originals. Every consumer reads `VERBS`
and `_action_label`; nothing here imports `dispatcher` (the executor depends on the verbs, never the
reverse — no circular import).
"""
import json

import frappe
from frappe import _
from frappe.integrations.utils import create_request_log
from frappe.utils import flt, get_request_session, validate_url

from tatva_connect.automation import fields, sends
from tatva_connect.taxonomy import labels
from tatva_connect.workflow_engine import refs

# The location guard reads a CRM Task's own fields, so it names them in the CRM Task namespace. Composed
# through `refs.of_record` rather than typed as `"crm_task.custom_task_type"`: the slug is `frappe.scrub`'s
# to decide, and a hand-spelled one is a second rule about the same name.
_TASK_TYPE_REF = refs.of_record("CRM Task", "custom_task_type")
_TASK_LAT_REF = refs.of_record("CRM Task", "custom_location_latitude")
_TASK_LNG_REF = refs.of_record("CRM Task", "custom_location_longitude")


def _action_label(a):
	"""Short human label of an action for the per-action audit trail in the run log."""
	if a.action_type == "Create Task":
		# The run log is read by an operator, so name the type, not its composite PK.
		return "Create Task {}".format(labels.label(a.task_type, labels.TASK_TYPE) or "?")
	if a.action_type == "Update Field":
		return "Update Field {}".format(a.fieldname or "?")
	if a.action_type in ("Append Child Row", "Upsert Child Row"):
		return "{} {}".format(a.action_type, a.child_table or "?")
	if a.action_type == "Call API":
		return "Call API {}".format(a.webhook_endpoint or "?")
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


# A workflow must not hang on an endpoint that never answers; a timeout is the `failed` output.
_API_TIMEOUT_SECONDS = 30
_LOG_LIMIT = 10000  # an Integration Request records the shape of an answer, never an unbounded body


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

	radius = location_api.location_required(context.get(_TASK_TYPE_REF), subject, context)
	if radius is None:
		return  # not required for this task type / submitted values — same non-match as the old hook
	lat, lng = context.get(_TASK_LAT_REF), context.get(_TASK_LNG_REF)
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


# -- the record a verb acts on ------------------------------------------------

TARGET_LEAD = "lead"          # the parent lead the run is about, whatever fired it
TARGET_AUTHORED = "authored"  # whichever reachable record the author's `Target` parameter names
TARGET_NONE = "none"          # this verb writes no record at all
TARGET_KINDS = (TARGET_LEAD, TARGET_AUTHORED, TARGET_NONE)


def target_of(verb):
	"""Which record this verb acts on, as declared. `None` for a verb that declares nothing — refused,
	never guessed, by `resolve_target`."""
	return (VERBS.get(verb) or {}).get("target")


def authored_target_field(verb):
	"""The parameter an `authored` verb takes its target doctype from — DERIVED from the verb's own params
	(the one typed `Target`), never a second per-verb map that could name a field the verb does not have."""
	for param in (VERBS.get(verb) or {}).get("params") or []:
		if param.get("type") == "Target":
			return param["name"]
	return None


def reachable_targets(subject_doctype):
	"""The records a verb's target can resolve to in a workflow watching `subject_doctype`.

	Exactly what `resolve_target` will accept: the parent lead the run is about, and the record that fired
	it (whose doctype IS the subject). Read by the publish gate (`graph._write_target_problems`) and by the
	authoring vocabulary (`describe.builder_schema`) alike — two copies of "what can a write reach" is how
	the picker came to offer lead fields under a `CRM Task` target.
	"""
	return [dt for dt in dict.fromkeys([fields.LEAD_DT, subject_doctype]) if dt]


def resolve_target(action, lead_name, trigger_doc):
	"""`(doctype, name)` of the record this node acts on — THE one answer, off the verb's declaration.

	Four verbs used to answer this four different ways with nothing written down: Update Field honoured
	the author's choice, while Create Note, Assign to User and both child-row verbs always wrote the lead
	even when a Task or a File fired the run. An author who learned one rule guessed wrong on the next.
	The answer now lives in the verb's `target` and the decision lives here; handlers never name a doctype.

	A rule's write scope is {the Lead} ∪ {the triggering doc}: anything else is out of scope and raises
	loudly rather than misfiring on a name that is not its. An undeclared verb raises too — guessing "it
	is probably the lead" is precisely how the four divergent answers grew.
	"""
	kind = target_of(action.action_type)
	if kind not in TARGET_KINDS:
		raise ValueError(
			f"{action.action_type!r} does not declare which record it acts on — declare `target` on it"
		)
	if kind == TARGET_NONE:
		return None, None
	if kind == TARGET_LEAD:
		return fields.LEAD_DT, lead_name
	doctype = action.get(authored_target_field(action.action_type) or "")
	if doctype == fields.LEAD_DT:
		return fields.LEAD_DT, lead_name
	if trigger_doc is not None and doctype == trigger_doc.doctype:
		return trigger_doc.doctype, trigger_doc.name
	raise ValueError(
		f"{action.action_type} target {doctype} is not in this rule's scope "
		f"(the Lead or the triggering {trigger_doc.doctype if trigger_doc else '—'})."
	)


def _resolve_write_target(action, lead_name, trigger_doc):
	"""The record this verb writes to, loaded fresh in the current transaction. ONE decision
	(`resolve_target`), one load — a handler never names its own doctype."""
	doctype, name = resolve_target(action, lead_name, trigger_doc)
	return frappe.get_doc(doctype, name)


def _action_assign_to_user(action, lead, context, axes, trigger_doc):
	"""ASSIGN TO USER — move ownership of the lead as a consequence of what happened in this run.

	The DEFAULT owner is not this node's job. An Assignment Rule declares that per grain, in the Desk,
	because round-robin rotation is state Frappe already keeps and ownership must happen whether or not a
	workflow is armed. This node exists for the part a standing rule cannot express: ownership changing
	BECAUSE something happened — nobody responded, a task completed, a predicate turned true.

	Native only: `assign_to.add` writes a ToDo, and the ToDo is the source of truth. `_assign` on the
	document is a derived cache that Frappe recomputes, so writing it directly is silently reverted.

	Reassign removes the current holders first, and does so through `assign_to.remove` — never by setting
	a ToDo to Closed, because a rule with no `close_condition` reopens Closed ToDos and the person would
	find the work back on their list.

	Leaves by `assigned` or by `nobody`: an escalation with no one to escalate to is a real outcome the
	author must be able to route, not an error that kills the run.
	"""
	from frappe.desk.form import assign_to

	doctype, name = resolve_target(action, lead, trigger_doc)
	user = _assignee(action, context)
	_assert_entitled_to_act(user, axes)
	# `assigned_to` is DECLARED emitted, so it is written on BOTH legs. A key that appears only when
	# someone was found could not honestly be offered downstream at all: the publish gate would certify
	# a node reading it and the read would silently be None on the leg that skipped the write.
	context["assigned_to"] = user or None
	if not user:
		context[refs.OUTPUT] = "nobody"
		return "no assignee resolved"

	if (action.assign_mode or "Assign") == "Reassign":
		for holder in _current_assignees(doctype, name):
			if holder != user:
				assign_to.remove(doctype, name, holder)  # Cancelled, never Closed

	if user not in _current_assignees(doctype, name):
		assign_to.add({
			"doctype": doctype,
			"name": name,
			"assign_to": [user],
			"description": action.assign_note or _("Assigned by a workflow"),
		})
	context[refs.OUTPUT] = "assigned"
	return f"assigned to {user}"


def _assert_entitled_to_act(user, axes):
	"""Refuse an assignment to someone the record's grain does not entitle.

	This node was completely ungrained: `assign_to_user` is a `Link` to `User`, `User` carries no grain
	axis, so nothing scoped the picker and nothing checked the pick. A workflow on Goodflip-Care/Anaya
	could hand a lead to a rep entitled only to Tatvapractice, on both sides, silently.

	Asked of the ONE entitlement brain — the same `access.entitlement` that decides which leads and fields
	that rep may see. No second notion of user-grain entitlement, and no query against the permission
	tables: a reverse query would be a second matcher free to disagree with the forward one.

	`axes` is the record's DATA grain, which is what `grain_entitled` expects. A run carrying no axes at
	all (a non-Lead subject on the durable path) has no grain to enforce, and inventing one here would
	refuse every File-triggered workflow rather than protect anything.
	"""
	from tatva_connect.access import entitlement

	grain = tuple((a or "") for a in (axes or ("", "", "")))
	if not user or not any(grain):
		return
	if not entitlement.grain_entitled(grain, user=user):
		raise PermissionError(
			f"{user} is not entitled to {'/'.join(a or '*' for a in grain)} — a workflow may not assign a "
			"record to someone who may not see it"
		)


def _current_assignees(doctype, name):
	"""Who holds this record right now — asked of Frappe, not queried ourselves.

	`assign_to.get` is the platform's own answer, and it excludes Cancelled AND Closed. A hand-written
	ToDo query here did exclude Cancelled but not Closed, so a closed assignment counted as a live holder
	and a Reassign would have tried to remove someone who no longer held anything.
	"""
	from frappe.desk.form import assign_to

	return [row["owner"] for row in assign_to.get({"doctype": doctype, "name": name})]


def _assignee(action, context):
	"""The user to assign to: a named one, or whatever an upstream value holds."""
	if (action.assignee_mode or "User") == "From Variable":
		return context.get(action.assignee_variable) or None
	return action.assign_to_user or None


def _action_create_task(action, lead, context, axes, trigger_doc):
	"""CREATE_TASK — reuse the idempotent follow-up helper, which grain-gates every task it raises, so
	a grain-A rule cannot plant a grain-B activity type. The gate lives THERE, not here: it must read
	the lead the task lands on, and `axes` is (None, None, None) for a Flow whose subject is not a Lead
	(a File-triggered Document Review is exactly that). The due date resolves from a context field
	(From Context) or an expression (Expression).

	A File / WhatsApp Message trigger carries no assignee, so the follow-up would land unassigned (on
	no rep's list, no assignment notification): fall back to the lead's owner. When the trigger is a
	File and the raised type is Document Review, pin the file onto the review task and mark the File
	Pending + linked (the review flow's on-upload step)."""
	from tatva_connect.tasks.tasks import create_followup_task

	lead = resolve_target(action, lead, trigger_doc)[1]  # declared `lead` — resolved, never assumed
	# The token that ties this task back to the node that raised it. A Wait downstream correlates on the
	# same token, so completing THIS task wakes THIS iteration — never another lead's, and never a
	# different task of the same type on the same lead. Absent on an ephemeral run, which cannot park.
	token = context.get(refs.TOKEN) if hasattr(context, "get") else None

	# Carry the completing task's assignee onto the next task (old-engine parity). Only a trigger that
	# genuinely has no assignee field — a File / WhatsApp Message — falls back to the lead owner so its
	# task is never orphaned; a Lead- or Task-triggered rule keeps producing an unassigned task for the
	# native Assignment Rule to route (do NOT force lead_owner on those — it defeats the Assignment Rule).
	assignee = trigger_doc.get("assigned_to") if trigger_doc else None
	if not assignee:
		# Fall back to the lead's owner whenever nothing else names an assignee. This used to be limited
		# to File / WhatsApp Message triggers, which meant every task raised by a workflow that PARKS
		# landed unassigned: on the durable path the "trigger doc" is the lead itself, a lead has no
		# `assigned_to`, and the old condition could never fire. Unassigned work sits on nobody's list.
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
		review = _review_task_for_file(trigger_doc.name, lead, action, context, assignee)
		_pin_review_file(review, trigger_doc.name)
		_stamp_workflow_token(review, token)
		return
	task = create_followup_task(
		lead=lead,
		task_type=action.task_type,
		due_at=_due_at(action, context),
		assigned_to=assignee,
	)
	_stamp_workflow_token(task, token)


def _stamp_workflow_token(task, token):
	"""Tie a raised task back to the node that raised it, so its completion wakes that exact wait.

	Written straight to the column: the token is engine bookkeeping, not a field a user or a rule may
	set, and a full save here would re-enter the very doc_events that raised it. The throttled helper can
	return an ALREADY-OPEN task from an earlier iteration — that task is already tied to its own node, so
	the token is only ever written where there is none, never moved.
	"""
	if not token or not task:
		return
	# A CRM Task autonames to an INTEGER, so a name is not necessarily a string — take the doc's `name`
	# when given a document, and treat any other scalar as the name itself.
	name = task.get("name") if hasattr(task, "get") else task
	if not name or frappe.db.get_value("CRM Task", name, "custom_workflow_token"):
		return
	frappe.db.set_value("CRM Task", name, "custom_workflow_token", token, update_modified=False)  # authz-ok: tier-a — workflow engine bookkeeping, never user input


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
	are idempotent. The `document` value is written through the activity write brain
	(activity_api.set_schema_field), which validates it is a declared CRM Task Type Field and routes it by
	field_target — never a hardcoded payload key that could silently drift from the field's real home."""
	from tatva_connect.activity import api as activity_api

	file_doc = frappe.get_doc("File", file_name)
	task = frappe.get_doc("CRM Task", task_name)
	if activity_api.set_schema_field(task, task.custom_task_type, "document", file_doc.file_url):
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
	_resolve_write_target(action, lead, trigger_doc).add_comment("Comment", text)


def _action_append_child(action, lead, context, axes, trigger_doc):
	"""APPEND_CHILD_ROW — add a new row to a CRM Lead child table (spec §4.2), via load+save so the
	lead's hooks re-run. Every field must be allowlisted for the child doctype at the lead's grain."""
	child_table, child_dt = _child_target(action)
	values = _resolve_map(action.set_json, context)
	if not values:
		raise ValueError("Append Child Row needs a non-empty Set (JSON)")
	_assert_child_allowlisted(child_dt, child_table, set(values), axes)
	tdoc = _resolve_write_target(action, lead, trigger_doc)
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
	tdoc = _resolve_write_target(action, lead, trigger_doc)
	row = _find_child_row(tdoc.get(child_table), match, child_dt)
	if row:
		for k, v in values.items():
			if k in match:
				continue  # never rewrite the natural key out from under the upsert
			row.set(k, v)
	else:
		tdoc.append(child_table, {**match, **values})
	tdoc.save(ignore_permissions=True)  # authz-ok: tier-a — automation effect lane (after-commit); rules are operator-built


def _action_call_api(action, lead, context, axes, trigger_doc):
	"""CALL API — call a curated endpoint, capture its response, and route on whether it succeeded.

	This replaces a fire-and-forget dispatch that enqueued Frappe's webhook delivery and never read the
	answer: the workflow could tell an external system something but could never act on what it said
	back. A node that cannot see its own result is a dead end in a graph whose whole purpose is to react.

	THE ENDPOINT IS STILL PICKED, NEVER TYPED. The URL, method, headers and secret stay on the curated
	`Webhook` record an administrator owns — an author chooses which endpoint, never where the request
	goes. Letting a workflow author type a URL would turn every workflow into an outbound request the
	network trusts (an SSRF the product would be shipping deliberately).

	The response becomes an ordinary context: `status`, `ok`, and the parsed `body`. `capture` maps paths
	out of it into named run variables, so every downstream node reads them like any other value; and
	`success_when` — the same predicate control the Trigger and Branch use — decides which of the node's
	two outputs the run takes. No `success_when` means the HTTP status decides.

	Runs INLINE rather than deferred: an output the run must route on cannot arrive after the run has
	already moved past this node. A transport failure is not an exception here — it is the `failed`
	output, which is a graph the author can handle.
	"""
	endpoint = action.webhook_endpoint
	if not endpoint:
		raise ValueError("Call API node missing an endpoint")
	if not frappe.db.exists("Webhook", endpoint):
		raise ValueError(f"Endpoint {endpoint!r} does not exist")

	source = action.webhook_payload_source or "Lead"
	if source == "Trigger Doc" and trigger_doc is not None:
		payload_doc = frappe.get_doc(trigger_doc.doctype, trigger_doc.name)  # fresh load, same txn
	else:
		payload_doc = frappe.get_doc("CRM Lead", lead)

	# Custom sends what the author composed; the other two send the record itself, unchanged.
	body = build_request_body(action.request_body, context) if source == "Custom" else None
	response = _call_endpoint(endpoint, payload_doc, body)
	_write_response_state(action.capture, response, context)
	context[refs.OUTPUT] = "succeeded" if _api_succeeded(action.success_when, response, context) else "failed"
	return f"{response['status']} {'ok' if response['ok'] else 'failed'}"


def _call_endpoint(endpoint, payload_doc, body=None):
	"""Issue the request the curated Webhook describes, and shape the answer into one context.

	Frappe's own plumbing, not our own: `get_request_session()` is the platform's HTTP session (pooled,
	with its retry adapter already mounted) and `validate_url` is its URL check. A hand-rolled
	`requests.request` would quietly opt out of both — and out of whatever the platform hardens next.
	`create_request_log` writes the Integration Request row, so an outbound call is inspectable in the
	desk exactly like every other integration this site makes.

	Every failure mode lands in the SAME shape — a transport error is `status: 0, ok: False` with the
	reason in `error` — so a graph handles a refused connection and a 500 identically, and neither takes
	the run down. We do NOT use `make_request`: it raises on any non-2xx, and a failed call here is data
	the author routes on, not an exception.
	"""
	hook = frappe.get_doc("Webhook", endpoint)
	url = hook.request_url
	# Defence in depth: the URL is admin-curated and Webhook validates it on its own save, but an http(s)
	# scheme check costs nothing and keeps a file:// or gopher:// endpoint from ever being reached.
	validate_url(url, throw=True, valid_schemes=("http", "https"))

	headers = {h.key: h.value for h in (hook.get("webhook_headers") or []) if h.get("key")}
	# The authored body when there is one, else the record itself — the request log records what really went.
	payload = payload_doc.as_dict() if body is None else body
	log = create_request_log(
		payload, is_remote_request=1, service_name="Workflow Call API", url=url,
		request_headers=headers or None,
		reference_doctype=payload_doc.doctype, reference_docname=payload_doc.name,
	)
	# `frappe.as_json` like Frappe's own enqueue_webhook: `as_dict()` returns datetimes and `json=` raises.
	headers.setdefault("Content-Type", "application/json")
	try:
		reply = get_request_session().request(
			(hook.request_method or "POST").upper(), url,
			data=frappe.as_json(payload), headers=headers, timeout=_API_TIMEOUT_SECONDS,
		)
	except Exception as transport:
		result = {"status": 0, "ok": False, "body": None, "error": str(transport)}
		log.db_set({"status": "Failed", "error": str(transport)}, commit=False, update_modified=False)
		return result

	try:
		body = reply.json()
	except ValueError:
		body = reply.text
	result = {"status": reply.status_code, "ok": reply.ok, "body": body, "error": None}
	log.db_set(
		{"status": "Completed" if reply.ok else "Failed", "output": frappe.as_json(body)[:_LOG_LIMIT]},
		commit=False, update_modified=False,
	)
	return result


def _write_response_state(capture, response, context):
	"""THE one writer of run state for a Call API — the declared response shape, then the author's rows.

	`emits` promises `status`, `ok` and `error` are always written, and `upstream` offers them to every
	node downstream; nothing ever wrote them, so a Branch on `ok` published green and then raised on the
	first live record. They are written here rather than in the handler so a Call API has exactly ONE
	place that puts anything into state — two writers is how the declaration and the runtime drifted
	apart in the first place.

	The declared keys go in FIRST, so an author who captures into a name of their own overrides the
	shape rather than being overridden by it: their row is the more specific instruction.

	A capture path that resolves to nothing writes `None` rather than being skipped: a downstream
	predicate naming that variable must see it as empty, not raise as though the author had misspelt it.
	"""
	context.update(_response_state(response))
	for row in (capture or []):
		if not isinstance(row, dict) or not row.get("variable"):
			continue
		context[row["variable"]] = _dig(response, row.get("path") or "")


def _response_state(response):
	"""The response as run state, in the TYPES the verb declares — `Int`, `Check`, `Data`.

	One shaping, two consumers: what a downstream node reads and what `success_when` is judged against
	are the same values, so an author's predicate on `ok` cannot mean one thing at the node and another
	on the Branch after it. `ok` is a Check (1/0, never True/False) because the evaluator resolves
	operators by declared type, and `error` is Data — empty, not null, when there was nothing to say.
	"""
	return {
		"status": response["status"],
		"ok": 1 if response["ok"] else 0,
		"error": response["error"] or "",
	}


def _dig(value, path):
	"""Walk a dotted path — `status`, `body.data.id`, `body.items.0.name`. Never raises."""
	for part in str(path).split("."):
		if part == "":
			continue
		if isinstance(value, dict):
			value = value.get(part)
		elif isinstance(value, list) and part.isdigit() and int(part) < len(value):
			value = value[int(part)]
		else:
			return None
	return value


def _api_succeeded(success_when, response, context=None):
	"""Did this call succeed? The author's predicate decides; without one, the HTTP status does.

	The predicate is judged in the NODE'S OWN namespace — `api.status`, `api.body.customer_id` — because
	that is where the values it tests really live and where `upstream` offers them. It used to be judged
	against a private flat dict, so `success_when` was the one predicate control in the product that spoke
	a different language from every other one: an author who picked `status` from the picker got
	`crm_lead.status` and it never matched anything here.

	The node id comes from the writer view the interpreter already scoped this handler with, so the verb
	still never has to know it. A caller with no view (a direct unit call) gets the response alone.
	"""
	if not success_when:
		return bool(response["ok"])
	from tatva_connect.automation import rules

	flat = _response_state(response)
	if isinstance(response.get("body"), dict):
		flat.update({f"body.{k}": v for k, v in response["body"].items()})
	writer = getattr(context, "writer_id", None)
	return rules.predicate_match(success_when, refs.Values(buckets={writer or refs.ENGINE: flat}), None)



def _action_send_whatsapp(action, lead, context, axes, trigger_doc):
	"""SEND_WHATSAPP (effect, Task 7) — the dormant sends gate. `sends.send_whatsapp` records the
	fire behind `Task::Automation::sends` (OFF by default, A.6) and, once the operator flips it,
	sends through the EXISTING WATI brain (grain-routed account + template, A.11/A.8). This handler
	only resolves the action's config off the rule row; no adapter logic lives here.

	`_output` names the edge the run leaves by, exactly as Call API does — the ONE routing mechanism,
	validated by `interpreter._verb_output` against what the verb declares. The decision itself belongs
	to `sends`, which is the module that knows why a send did not happen."""
	from tatva_connect.automation import sends

	output, result = sends.send_whatsapp(
		resolve_target(action, lead, trigger_doc)[1],
		action.contact_number,
		action.whatsapp_template, context, action.template_values,
		# The token `_run_verb` minted for THIS node, so a delivery receipt can find this run and no other.
		correlation=context.get(refs.TOKEN),
	)
	context[refs.OUTPUT] = output
	return result


def _action_send_email(action, lead, context, axes, trigger_doc):
	"""SEND_EMAIL (effect, Task 7) — same dormant gate as Send WhatsApp; live sends go through
	native `frappe.sendmail` (A.18), never a hand-rolled mail path."""
	from tatva_connect.automation import sends

	output, result = sends.send_email(
		resolve_target(action, lead, trigger_doc)[1],
		action.email_recipient, action.email_template, context, action.template_values,
	)
	context[refs.OUTPUT] = output
	return result


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



# The ONE action-lane registry (A.8): every verb's lane is declared exactly once here, read by both
# `run_guards` (guard-lane actions) and `run_effects`/`_run_action` (effect-lane actions). Adding a
# verb = one row here, never a second lane table. `Require Fields` is the first guard verb (Task 5);
# `Require Location` (Task 8) is the second. `CRMAutomationRule.validate()` rejects any action_type
# not present here at author time (Task 8) — a verb sitting in the Select with no row here (e.g. Wait,
# before Task 9) can never reach a rule.
# THE verb declaration. One entry per verb: which lane it runs in, which handler runs it, how it reads
# to an author, and the parameters it takes. The parameters used to live in `describe._VERB_PARAMS`,
# which meant the thing that DECLARED a verb's inputs and the thing that READ them were in different
# modules and could disagree. They are now next to the handler that consumes them.
#
#   lane "guard"  — runs inside validate and may BLOCK a save by raising.
#   lane "effect" — runs after the save, inside the node's savepoint, and may never block.
#
# `emits` are the run-state VARIABLES a verb writes, so a downstream node can be offered them instead of
# asking the author to type a name from memory. Static keys are listed here; a verb whose keys depend on
# its own config (Call API's `capture` rows) names the config field in `emits_from` and the resolver
# reads the author's rows. This is the same lesson as `outcomes`, applied to state instead of events:
# a name typed blind is a name that can be typed wrong, and nothing notices until a run behaves oddly.
#
# `outcomes` are the events a verb can later EMIT. A verb that emits nothing finishes and the run moves
# on; a verb that emits is something the world answers — a task someone completes, a message someone
# replies to — and a Wait downstream can name one of those outcomes and suspend until it arrives. The
# names are declared here so a Wait offers a CHOICE rather than a free-text box: a typo in an event name
# used to mean a run that parks for ever with nothing able to wake it.
VERBS = {
	"Require Fields": {
		"lane": "guard", "handler": _action_require_fields,
		"label": "Require Fields",
		"description": "Refuses the save unless every named field has a value.",
		"params": [
			{"name": "require_fields", "label": "Fields", "type": "Small Text"},
		],
	},
	"Require Location": {
		"lane": "guard", "handler": _action_require_location,
		"label": "Require Location",
		"description": "Refuses the save unless the record carries a location inside the geofence.",
		"params": [
			{"name": "geofence_meters", "label": "Geofence Meters", "type": "Int"},
		],
	},
	"Assign to User": {
		"lane": "effect", "handler": _action_assign_to_user, "target": TARGET_LEAD,
		"label": "Assign to User",
		"description": "Moves ownership of the lead. The default owner is an Assignment Rule's job; this is for ownership changing because something happened.",
		"outputs": ["assigned", "nobody"],
		"emits": [{"name": "assigned_to", "type": "Link", "about": "who now holds the lead"}],
		"params": [
			{"name": "assign_mode", "label": "Mode", "type": "Select",
			 "options": ["Assign", "Reassign"], "reqd": True},
			{"name": "assignee_mode", "label": "Assign to", "type": "Select",
			 "options": ["User", "From Variable"], "reqd": True},
			# `User` carries no grain axis, so the picker cannot be scoped by columns — it DECLARES the kind.
			{"name": "assign_to_user", "label": "User", "type": "Link", "link": "User",
			 "scope": "entitled_users",
			 "depends_on_value": {"assignee_mode": ["User"]}},
			{"name": "assignee_variable", "label": "Take the user from", "type": "Variable",
			 "depends_on_value": {"assignee_mode": ["From Variable"]}},
			{"name": "assign_note", "label": "Note", "type": "Data"},
		],
	},
	"Create Task": {
		"lane": "effect", "handler": _action_create_task, "target": TARGET_LEAD,
		"label": "Create Task",
		"description": "Raises a task on the lead.",
		"outcomes": ["task.completed", "task.cancelled"],
		"params": [
			{"name": "task_type", "label": "Task Type", "type": "Link", "link": "CRM Task Type", "reqd": True},
			{"name": "due_mode", "label": "Due Mode", "type": "Select", "options": ["From Context", "Expression"]},
			{"name": "due_from", "label": "Due date from", "type": "Variable",
			 "depends_on_value": {"due_mode": ["From Context"]}},
			{"name": "due_expression", "label": "Due Expression", "type": "Small Text", "reads": "expression",
			 "depends_on_value": {"due_mode": ["Expression"]}},
		],
	},
	"Update Field": {
		"lane": "effect", "handler": _action_set_field, "target": TARGET_AUTHORED,
		"label": "Update Field",
		"description": "Writes a value onto a field the operator has allowed automation to set.",
		"params": [
			{"name": "target_doctype", "label": "Write to", "type": "Target", "reqd": True},
			# `doctype_from` names the sibling holding the doctype this field belongs to — read by the publish gate.
			{"name": "fieldname", "label": "Field to set", "type": "Field", "reqd": True,
			 "doctype_from": "target_doctype"},
			{"name": "value_mode", "label": "Value Mode", "type": "Select",
			 "options": ["Literal", "From Context", "Expression"], "reqd": True},
			{"name": "value", "label": "Value", "type": "Data", "depends_on_value": {"value_mode": ["Literal"]}},
			{"name": "context_field", "label": "Take the value from", "type": "Variable",
			 "depends_on_value": {"value_mode": ["From Context"]}},
			{"name": "expression", "label": "Expression", "type": "Small Text", "reads": "expression",
			 "depends_on_value": {"value_mode": ["Expression"]}},
		],
	},
	"Append Child Row": {
		"lane": "effect", "handler": _action_append_child, "target": TARGET_LEAD,
		"label": "Append Child Row",
		"description": "Adds a row to a child table on the lead.",
		"params": [
			{"name": "child_table", "label": "Child Table", "type": "Data", "reqd": True},
			{"name": "set_json", "label": "Set (JSON)", "type": "Code", "options": "JSON", "reqd": True, "reads": "ctx_json"},
		],
	},
	"Upsert Child Row": {
		"lane": "effect", "handler": _action_upsert_child, "target": TARGET_LEAD,
		"label": "Upsert Child Row",
		"description": "Updates a matching child row, or adds one if none matches.",
		"params": [
			{"name": "child_table", "label": "Child Table", "type": "Data", "reqd": True},
			{"name": "match_json", "label": "Match (JSON)", "type": "Code", "options": "JSON", "reqd": True, "reads": "ctx_json"},
			{"name": "set_json", "label": "Set (JSON)", "type": "Code", "options": "JSON", "reqd": True, "reads": "ctx_json"},
		],
	},
	"Call API": {
		"lane": "effect", "handler": _action_call_api, "target": TARGET_NONE,
		"label": "Call API",
		"description": "Calls a curated endpoint and captures its response into named variables.",
		"outputs": ["succeeded", "failed"],
		# The shape of the answer, always written; plus one variable per `capture` row the author adds.
		"emits": [
			{"name": "status", "type": "Int", "about": "HTTP status code"},
			{"name": "ok", "type": "Check", "about": "1 when the call succeeded"},
			{"name": "error", "type": "Data", "about": "why the call could not be made"},
		],
		"emits_from": "capture",
		"params": [
			{"name": "webhook_endpoint", "label": "Endpoint", "type": "Link", "link": "Webhook", "reqd": True},
			{"name": "webhook_payload_source", "label": "Send", "type": "Select",
			 "options": ["Lead", "Trigger Doc", "Custom"]},
			# The author says WHAT is sent; the curated Webhook still says WHERE. `reads=ctx_json` is what
			# makes its references visible to the publish gate, at any depth.
			{"name": "request_body", "label": "Request Body", "type": "Code", "options": "JSON",
			 "reads": "ctx_json", "depends_on_value": {"webhook_payload_source": ["Custom"]},
			 "placeholder": '{"model": "gpt-4o", "messages": [{"role": "user", "content": "$ctx.crm_lead.first_name"}]}'},
			# `preview` declares that this control can fetch a REAL answer: the method, and its sibling args.
			{"name": "capture", "label": "Capture", "type": "Mapping",
			 "preview": {
				 "method": "tatva_connect.workflow_engine.context.test_call",
				 "args": {"endpoint": "webhook_endpoint", "request_body": "request_body"},
			 }},
			{"name": "success_when", "label": "Succeeded when", "type": "Predicate"},
		],
	},
	"Create Note": {
		"lane": "effect", "handler": _action_add_comment, "target": TARGET_LEAD,
		"label": "Create Note",
		"description": "Adds a note to the lead's timeline.",
		"params": [
			{"name": "comment_mode", "label": "Mode", "type": "Select", "options": ["Literal", "Expression"]},
			{"name": "comment_text", "label": "Text", "type": "Data",
			 "depends_on_value": {"comment_mode": ["Literal"]}},
			{"name": "comment_expression", "label": "Expression", "type": "Small Text", "reads": "expression",
			 "depends_on_value": {"comment_mode": ["Expression"]}},
		],
	},
	"Send WhatsApp": {
		"lane": "effect", "handler": _action_send_whatsapp, "target": TARGET_LEAD,
		"label": "Send WhatsApp",
		"description": "Sends a template message on the resolved WhatsApp account, and routes on whether it reached the patient.",
		# A send that did not reach the patient is DATA the author routes, not an exception that kills the
		# run. The split between the two edges lives in `sends`; the names come from there too.
		"outputs": [sends.SENT, sends.FAILED],
		# The LATER events this send can emit come from the adapter's own declaration, never from a list typed here — see `outcomes_of`. `sent`/`failed` are the synchronous answer above and are excluded there.
		"outcomes_channel": "whatsapp",
		"params": [
			# The recipient is DECLARED, in every trigger mode - the same field with the same picker, never conditional on anything else in the graph. Picked, never typed: a typed number is how a message reached the wrong country.
			{"name": "contact_number", "label": "Contact number", "type": "Variable", "reqd": True},
			{"name": "whatsapp_template", "label": "Template", "type": "Link",
			 "link": "WhatsApp Templates", "reqd": True},
			# Which buttons this send OFFERS. The author declares them; a downstream Wait draws one branch per row. Never synced from the provider and never inferred from whatever arrives.
			{"name": "buttons", "label": "Buttons offered", "type": "Button List"},
			# The template's placeholders, DECLARED. `reads=value_rows` is what makes them visible to
			# `contract.reads_of` and therefore refusable by the publish gate; `slots_from` names the
			# sibling holding the template whose real placeholder names the control offers.
			{"name": "template_values", "label": "Template Values", "type": "Value Map",
			 "slots_from": "whatsapp_template",
			 "slots_method": "tatva_connect.automation.sends.template_slots"},
		],
	},
	"Send Email": {
		"lane": "effect", "handler": _action_send_email, "target": TARGET_LEAD,
		"label": "Send Email",
		"description": "Sends an email after the segment commits, and routes on whether it could be sent.",
		"outputs": [sends.SENT, sends.FAILED],
		"params": [
			# Picked from the grouped picker, never typed. The literal path this used to carry mailed a phone-shaped string as an address.
			{"name": "email_recipient", "label": "Recipient", "type": "Variable", "reqd": True},
			# Frappe's own template store. There is no compose box: every message goes through the org's template chain.
			{"name": "email_template", "label": "Template", "type": "Link", "link": "Email Template", "reqd": True},
			# The same Value Map WhatsApp uses; only the slot SOURCE differs, because an Email Template names its variables.
			{"name": "template_values", "label": "Template Values", "type": "Value Map",
			 "slots_from": "email_template",
			 "slots_method": "tatva_connect.automation.sends.email_template_slots"},
		],
	},
}


def emits_of(verb, config=None):
	"""The run-state variables this verb writes, given how it is configured.

	Static keys come from the declaration; config-derived ones are read from the field named by
	`emits_from` — for Call API that is the author's own `capture` rows, so the variables offered
	downstream are exactly the ones this node will really set.
	"""
	declared = VERBS.get(verb) or {}
	found = [dict(e) for e in declared.get("emits") or []]

	source = declared.get("emits_from")
	if source and config:
		for row in config.get(source) or []:
			name = (row or {}).get("variable")
			if name:
				found.append({"name": name, "type": "Data", "about": _("captured from the response")})
	return found


def outcomes_of(verb):
	"""The events this verb can emit — the choices a downstream Wait may name. Empty for a verb the world
	never answers.

	A verb that acts on a CHANNEL declares the channel rather than a list, and the answer is derived from
	what that channel's adapters declare they can truthfully report. So WATI declaring `delivered` is what
	gives the node a waitable `delivered`, with nothing typed here and no code change.

	Its own SYNCHRONOUS outputs are excluded, and that closes a race rather than tidying a taxonomy. The
	send path already returned `sent`/`failed` to the run before any provider was called; a `sent` status
	can arrive from WATI in under a second, while the row the bridge correlates through is only committed
	when the background job ends. A Wait on `sent` could therefore never be woken reliably — so nothing may
	declare one, and the verb's own `outputs` are the one place that fact is written.
	"""
	declared = VERBS.get(verb) or {}
	channel = declared.get("outcomes_channel")
	if not channel:
		return list(declared.get("outcomes") or [])

	from tatva_connect.channels import resolve

	synchronous = set(declared.get("outputs") or [])
	return [name for name in resolve.outcomes_for_channel(channel) if name.split(".", 1)[1] not in synchronous]


def lane_of(verb):
	return (VERBS.get(verb) or {}).get("lane")


def handler_of(verb):
	return (VERBS.get(verb) or {}).get("handler")


def verbs_in_lane(lane):
	"""Every verb in a lane, in declaration order. The ONE way to ask 'what can guard' / 'what can act'."""
	return [verb for verb, declared in VERBS.items() if declared["lane"] == lane]


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
	if isinstance(spec, str) and spec.startswith(refs.CTX_PREFIX):
		return context.get(spec[5:])
	return spec


def _walk(value, leaf):
	"""Rebuild a JSON structure with `leaf` applied to every scalar, at any depth.

	ONE traversal, used by both halves of the request body: the publish gate collects references with it
	and the runtime resolves them with it. A real API body nests — `messages` is a list of objects — and
	two separate walks over that shape is precisely how a gate comes to check less than a run performs.
	"""
	if isinstance(value, dict):
		return {k: _walk(v, leaf) for k, v in value.items()}
	if isinstance(value, list):
		return [_walk(v, leaf) for v in value]
	return leaf(value)


def _parsed_body(raw):
	"""The authored body as data, or {} when there is none. Raises the author's own error on bad JSON."""
	if not (raw or "").strip():
		return {}
	try:
		return json.loads(raw)  # ALLOWLIST 2026-07-22: raw parse so the except gives a precise message.
	except (ValueError, TypeError):
		raise ValueError("invalid JSON in the Call API request body")


def build_request_body(raw, context):
	"""The body an author wrote, with every `$ctx.` reference resolved at any depth.

	The author says WHAT is sent; the curated `Webhook` still says WHERE it goes and carries the secret.
	That split is the whole security model — an author cannot point a request anywhere, only fill one in.
	"""
	return _walk(_parsed_body(raw), lambda v: _resolve(v, context))


def body_references(raw):
	"""Every run-state reference the authored body names — the same walk `build_request_body` performs.

	Exported so the publish gate asks THIS module what the body reads, rather than re-deriving it from a
	structure it would have to learn the shape of independently.
	"""
	found = []
	_walk(_parsed_body(raw), lambda v: found.append(v[len(refs.CTX_PREFIX):]) if isinstance(v, str) and v.startswith(refs.CTX_PREFIX) else v)
	return [name for name in found if name]


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
	match keys must additionally be the section's row key. Fail-closed. One allowlist brain
	(fields.is_settable).

	The doctype asked about is the LEAD, never the child doctype: the lead catalog (`CRM Lead API Field`)
	is where a child field is declared, and WHICH child table it lands in is derived from its
	`CRM Lead Section` — which is exactly what `child_table_field` disambiguates. Asking about the child
	doctype fell through `is_settable`'s `doctype != LEAD_DT` floor and refused every field, so no
	Append/Upsert Child Row node could ever run. `child_dt` is kept for the message, which is what an
	author reads.
	"""
	keys = keys or set()
	for f in fieldnames:
		if not fields.is_settable(fields.LEAD_DT, f, axes, child_table_field=child_table, require_row_key=(f in keys)):
			raise PermissionError(
				f"{f} on {child_dt} ({child_table}) is not in the enabled Automation-Field allowlist"
			)
