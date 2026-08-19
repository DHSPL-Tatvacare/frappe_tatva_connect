"""The automation engine's verb handlers — every EFFECT action body + its resolver helpers +
the `VERBS` declaration.

Extracted from `dispatcher.py` (Task 5 co-located the verb bodies with the two-lane executor for the
initial split; Task 6 finishes the separation so dispatcher.py owns only orchestration —
run_effects/`_run_action` dispatch/Run Log/error factory — and this module owns every verb's
implementation). A move, not a rewrite (A.8/A.12) — behavior, docstrings and security annotations are
unchanged from their originals. Every consumer reads `VERBS`
and `_action_label`; nothing here imports `dispatcher` (the executor depends on the verbs, never the
reverse — no circular import).

NO VERB MAY BLOCK A SAVE (Phase 11). `Require Fields` and `Require Location` used to live here as GUARD
verbs that ran inside `validate` and raised, so an authored workflow decided whether a rep could save.
That is a second brain over a rule the record's own doctype already owns, and it is deleted: a workflow
decides whether IT runs, never whether a rep may save. What each verb demanded is said instead on the
Trigger's `predicate` — `is set` / `is not set` already ship (`automation/rules.py:_PRESENCE_OPS`) — and
the location rule stays declared once on the task type (`visit_mode` plus the location condition), enforced by
`location.api` off `activity.api.compute_activity` with `tasks.enforce_location` as its backstop.
"""
import json

import frappe
from frappe import _
from frappe.integrations.utils import create_request_log
from frappe.utils import get_request_session, validate_url

from tatva_connect.automation import fields, sends, subjects
from tatva_connect.taxonomy import labels
from tatva_connect.workflow_engine import document_render, refs


def _action_label(a):
	"""Short human label of an action for the per-action audit trail in the journey log."""
	if a.action_type == "Create Task":
		# The journey log is read by an operator, so name the type, not its composite PK.
		return "Create Task {}".format(labels.label(a.task_type, labels.TASK_TYPE) or "?")
	if a.action_type == "Update Field":
		# W8.1 — a node sets MANY fields now, so the label names them all; `a.fieldname` would read "?" for every node.
		return "Update Field {}".format(", ".join(r.get("name") for r in (a.updates or []) if r.get("name")) or "?")
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


# A workflow must not hang on an endpoint that never answers; a timeout is the `failed` output. 120s is safe because a journey runs on the `workflow` queue, whose job timeout is 1500s, and never inside a user's save — a reasoning model on a long prompt simply outlives 30s and read as a broken pipeline.
_API_TIMEOUT_SECONDS = 120
_LOG_LIMIT = 10000  # an Integration Request records the shape of an answer, never an unbounded body


# -- the record a verb acts on ------------------------------------------------

TARGET_LEAD = "lead"          # the parent lead the journey is about, whatever fired it
TARGET_AUTHORED = "authored"  # whichever reachable record the author's `Target` parameter names
TARGET_NONE = "none"          # this verb writes no record at all
TARGET_KINDS = (TARGET_LEAD, TARGET_AUTHORED, TARGET_NONE)


def target_of(verb):
	"""Which record this verb acts on, as declared. `None` for a verb that declares nothing — refused,
	never guessed, by `resolve_target`."""
	return (VERBS.get(verb) or {}).get("target")


def params_of(verb):
	"""The parameters an author really gets for `verb`: the declaration, minus any whose declared
	`capability` no adapter on the verb's channel offers.

	THE ONE READER, and that is the point of it. A field is offered because a PROVIDER supports it — the
	calling-hours bypass is a request field Bolna takes and another vendor may not — so the question
	"does this field exist here" has exactly one answer, given once. Read raw, the inspector could hide a
	field while `describe` still advertised it, which is two answers and a support ticket.

	The filter itself is `resolve.offered_fields` — shared with the canvas palette, which asks the same
	question of the same fields in a different shape. Request-time only: it resolves adapter modules, and
	a verb whose params declare no capability short-circuits before touching one.
	"""
	declared = VERBS.get(verb) or {}

	from tatva_connect.channels import resolve

	# `outcomes_channel` is the channel this verb sends on — the same key `outcomes_of` reads, never a second declaration of it.
	return resolve.offered_fields(declared.get("params") or [], declared.get("outcomes_channel"))


def authored_target_field(verb):
	"""The parameter an `authored` verb takes its target doctype from — DERIVED from the verb's own params
	(the one typed `Target`), never a second per-verb map that could name a field the verb does not have."""
	for param in params_of(verb):
		if param.get("type") == "Target":
			return param["name"]
	return None


def reachable_targets(subject_doctype):
	"""The records a verb's target can resolve to in a workflow watching `subject_doctype`.

	Exactly what `resolve_target` will accept: the parent lead the journey is about, and the record that fired
	it (whose doctype IS the subject). Read by the publish gate (`graph._write_target_problems`) and by the
	authoring vocabulary (`describe.builder_schema`) alike — two copies of "what can a write reach" is how
	the picker came to offer lead fields under a `CRM Task` target.
	"""
	return [dt for dt in dict.fromkeys([fields.LEAD_DT, subject_doctype, *subjects.WRITE_TARGETS]) if dt]


def writable_records(subject_doctype):
	"""Every record a write may SET A FIELD on — deliberately wider than `reachable_targets`.

	A Target names the record a verb acts on, and no Target may name a child section. But a child-row node
	names its SECTION and sets that section's columns, so the lead's child sections ARE settable. Two
	questions, two answers: `reachable_targets` is what a Target may resolve to, this is what a field may
	be set on, and `_written_doctype` can return exactly these two shapes and no other.

	Composed from the two existing readers rather than restated: `child_sections` is already the one place
	nobody re-filters `CRM Lead Section` by hand, and it is request-cached. Read by the publish gate
	(`registry._write_target_problems`) and by the authoring vocabulary (`describe._settable_targets`), so
	the gate cannot refuse a write whose fields the picker offered.
	"""
	from tatva_connect.partner_api.doctype.crm_lead_section import crm_lead_section

	sections = [s.target_doctype for s in crm_lead_section.child_sections() if s.target_doctype]
	return list(dict.fromkeys(reachable_targets(subject_doctype) + sections))


def wrote_name(context, doctype):
	"""The record of `doctype` THIS journey has already made, or None when it has made none.

	THE one reader of `refs.WROTE`, so `resolve_target` and every caller asking "did we make one yet"
	cannot answer it two ways. Keyed by `refs.slug` — frappe's own `scrub` — never a slugify of our own."""
	return ((context or {}).get(refs.WROTE) or {}).get(refs.slug(doctype))


def remember_wrote(context, doctype, name):
	"""Record what this journey just made, under the ENGINE's bucket so a later node can see it.

	Written through the door a handler already uses for `_engine.output`: a NAMESPACED ref is honoured as
	written (`refs._WriterView.__setitem__`), where a bare key would land at `<node_id>.<slug>`. Read-copy-
	write because the view exposes `get` and `__setitem__` and nothing else; `_storable` persists
	`state.buckets` whole, so it survives a park."""
	context[refs.WROTE] = {**(context.get(refs.WROTE) or {}), refs.slug(doctype): name}


def resolve_target(action, lead_name, trigger_doc, context=None):
	"""`(doctype, name)` of the record this node acts on — THE one answer, off the verb's declaration.

	Four verbs used to answer this four different ways with nothing written down: Update Field honoured
	the author's choice, while Create Note, Assign to User and both child-row verbs always wrote the lead
	even when a Task or a File fired the journey. An author who learned one rule guessed wrong on the next.
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
	# A DECLARED write target names whatever THIS journey has already made, and nothing when it has made
	# none yet — a missing name is the insert leg, not a fault. The name lives in the ENGINE's bucket
	# because a handler's `context` is a `_WriterView` scoped to its own node (refs.py:365): a bare key
	# would land at `<node_id>.<slug>` and the next node could never see it.
	if doctype in subjects.WRITE_TARGETS:
		return doctype, wrote_name(context, doctype)
	raise ValueError(
		f"{action.action_type} target {doctype} is not in this rule's scope "
		f"(the Lead or the triggering {trigger_doc.doctype if trigger_doc else '—'})."
	)


def _save_target(tdoc, touched=None):
	"""Save a verb's target, skipping reconciliation of the child tables this run did not write.

	Frappe reconciles EVERY child table on every save and `CRM Lead` has eleven, so a stage write issued ten
	`DELETE ... WHERE name NOT IN (...)` statements against tables it never looked at. `ignore_children_type`
	is Frappe's own flag for this and it suppresses ONLY the delete (`document.update_child_table`): rows held
	in memory are still upserted, so a hook that APPENDS a row is unaffected. Safe by construction here — no
	verb removes a child row, so those deletes were no-ops to begin with.
	"""
	tdoc.flags.ignore_children_type = [
		df.options for df in tdoc.meta.get_table_fields() if df.fieldname != touched
	]
	tdoc.save(ignore_permissions=True)  # authz-ok: tier-a — automation effect lane (after-commit); rules are operator-built


def _resolve_write_target(action, lead_name, trigger_doc, context=None):
	"""The record this verb writes to, loaded fresh in the current transaction. ONE decision
	(`resolve_target`), one load — a handler never names its own doctype.

	A declared write target with no name yet is a NEW doc: `save()` branches to `insert()` itself
	(document.py:558), so insert-or-update is one code path and nothing here asks which of the two
	happened. The name is recorded by the handler after the save, which is what makes a second write
	on the same target an update."""
	doctype, name = resolve_target(action, lead_name, trigger_doc, context)
	return frappe.get_doc(doctype, name) if name else frappe.new_doc(doctype)


def _action_assign_to_user(action, lead, context, axes, trigger_doc):
	"""ASSIGN TO USER — move ownership of the lead as a consequence of what happened in this journey.

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
	author must be able to route, not an error that kills the journey.
	"""
	from frappe.desk.form import assign_to

	doctype, name = resolve_target(action, lead, trigger_doc)
	# Pool assigns THROUGH frappe, so the record already has its holder by the time the tail runs: the
	# `add` below is skipped because the user is already in `_current_assignees`, and the Reassign loop
	# cannot run because `assign_mode` is hidden in this mode. The grain gate is the same one, not a second.
	user = (
		_pool_assignee(action, doctype, name)
		if (action.assignee_mode or "User") == POOL
		else _assignee(action, context)
	)
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

	`axes` is the record's DATA grain, which is what `grain_entitled` expects. A journey carrying no axes at
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


# The third way to name an assignee: frappe's own Assignment Rule picks, and keeps the rotation state.
POOL = "Pool"


def _pool_assignee(action, doctype, name):
	"""Hand the record to frappe's Assignment Rule and report who it chose, or None.

	`do_assignment` is the whole of it — it picks by the rule's own strategy (Round Robin, Load Balancing,
	Based on Field, Weighted), writes the ToDo stamped with the rule, notifies, and advances `last_user`.
	Calling `get_user()` and assigning ourselves would split frappe's pick from frappe's bookkeeping, so
	round robin would never rotate and weighted would burn a slot per call.

	It returns True/False rather than the user, so the holder is read back through `_current_assignees` —
	frappe's own `assign_to.get`, the same reader the named-user leg uses. A rule off duty today, or one
	that found nobody, is `None`: the caller leaves by `nobody`, which is a real outcome an author routes.
	"""
	if not action.assignment_rule:
		return None
	rule = frappe.get_cached_doc("Assignment Rule", action.assignment_rule)
	if rule.is_rule_not_applicable_today():
		return None
	# `as_dict()`, because that is what frappe hands its own rules (assignment_rule.apply:296) and
	# `do_assignment` renders the rule's description against it — a Document is not iterable and Jinja
	# refuses it.
	if not rule.do_assignment(frappe.get_doc(doctype, name).as_dict()):
		return None
	# `do_assignment` clears first and then adds exactly one, so this is that one.
	return next(iter(_current_assignees(doctype, name)), None)


def _assignee(action, context):
	"""The user to assign to: a named one, or whatever an upstream value holds."""
	if (action.assignee_mode or "User") == "From Variable":
		return context.get(action.assignee_variable) or None
	return action.assign_to_user or None


# The third due mode's word — the delay is written with the Wait's own `Duration`, so one node's "in 14 days" and another's mean the same thing and land on the same arithmetic.
DUE_AFTER_DELAY = "After a delay"


def _priority_options():
	"""The priorities a task may really carry — READ OFF `CRM Task`'s own field, never typed here.

	A typed triple would be a second declaration of an operator's vocabulary: add a priority to the doctype
	and this node would keep offering yesterday's. Split by `describe._value_options`, the ONE reader of a
	Select's option lines, so what an author picks and what the record accepts cannot disagree.
	"""
	from tatva_connect.automation import describe

	field = frappe.get_meta("CRM Task").get_field("priority")
	return describe._value_options("Select", field.options if field else "")


def _action_create_task(action, lead, context, axes, trigger_doc):
	"""CREATE_TASK — reuse the idempotent follow-up helper, which grain-gates every task it raises, so
	a grain-A rule cannot plant a grain-B activity type. The gate lives THERE, not here: it must read
	the lead the task lands on, and `axes` is (None, None, None) for a Flow whose subject is not a Lead
	(a File-triggered Document Review is exactly that). The due date resolves from a context field
	(From Context), an expression (Expression) or a delay from now (After a delay).

	The assignee is resolved with the same controls as Assign to User — `assignee_mode` plus
	`assign_to_user` / `assignee_variable`. When neither is chosen (the default, in every existing
	workflow) the old auto rule fires: carry the trigger's assignee forward, falling back to the
	lead's owner. A chosen user is grain-entitled through the same `_assert_entitled_to_act` gate
	Assign to User uses.

	When the trigger is a File and the raised type is Document Review, pin the file onto the review
	task and mark the File Pending + linked (the review flow's on-upload step)."""
	from tatva_connect.tasks.tasks import raise_followup_task

	lead = resolve_target(action, lead, trigger_doc)[1]  # declared `lead` — resolved, never assumed
	# The token that ties this task back to the node that raised it. A Wait downstream correlates on the
	# same token, so completing THIS task wakes THIS iteration — never another lead's, and never a
	# different task of the same type on the same lead. Absent on an ephemeral journey, which cannot park.
	token = context.get(refs.TOKEN) if hasattr(context, "get") else None

	# Assignee: the same two modes as Assign to User's `_assignee` — User picks a person, From Variable
	# reads one out of the run. When neither is chosen (the default, all existing workflows) the old
	# auto rule fires: carry the trigger's assignee forward, falling back to the lead's owner.
	mode = action.get("assignee_mode")
	if mode == "From Variable":
		assignee = context.get(action.get("assignee_variable")) or None
	elif mode == "User":
		assignee = action.get("assign_to_user") or None
	else:
		assignee = trigger_doc.get("assigned_to") if trigger_doc else None
		if not assignee:
			assignee = frappe.db.get_value("CRM Lead", lead, "lead_owner")
	if assignee:
		_assert_entitled_to_act(assignee, axes)
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
	task = raise_followup_task(
		lead=lead,
		task_type=action.task_type,
		title=_subject(action, context),
		due_at=_due_at(action, context),
		assigned_to=assignee,
		priority=action.get("priority") or None,
		description=action.get("description") or None,
		# The author's control, inverted at the seam: the param asks "allow duplicates", the helper asks
		# "throttle". One negation here keeps the author's word plain and the helper's contract unchanged.
		throttle=not action.get("allow_duplicate_tasks"),
		# The SAME token stamped below, handed in so the open-task check matches on it too: this node still reuses its own task on a re-fire, while a SECOND node of the same type gets its own instead of silently creating nothing. Narrowed by the node, never lifted.
		node_token=token,
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
	from tatva_connect.tasks.tasks import CLOSED_STATUSES, raise_followup_task

	existing = frappe.db.get_value("File", file_name, "custom_review_task")
	if existing and frappe.db.get_value("CRM Task", existing, "status") not in CLOSED_STATUSES:
		return existing  # this document already has an open review task — idempotent re-fire
	return raise_followup_task(
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
	"""UPDATE FIELD via the UNIFIED write path: load the target doc, set every declared row, save ONCE —
	NEVER frappe.db.set_value (skips validate/hook re-mirroring). The target is the rule's scope — the
	Lead, or the triggering doc itself (a Field-Changed rule on a Task may set a field on that Task).
	Every row is gated by the lead's grain contract at runtime (defense in depth), and gated BEFORE
	anything is written so a forbidden third row cannot leave the first two applied. The write runs inside
	the rule's savepoint, so a later action's failure rolls this back too (group atomicity).

	W8.1 — one node, many fields, each row carrying its own mode. It was one field per node, which is why
	one LeadSquared step needed three of ours. Row values resolve through `contract.resolve_row`, THE one
	reader, so this node and a template slot cannot disagree about what `From Context` means.

	W8.2 — a row in `Increment by` mode reads the field it is about to write, so the read and the write
	must be one atomic step or two journeys on the same lead both read 2 and both write 3. The row lock is
	taken FIRST and only when a row needs it: `frappe.db.get_value(..., for_update=True)`, the same door
	`tasks.create_followup_task` uses to serialize its check-then-insert, held to the segment's commit.
	"""
	from tatva_connect.workflow_engine import contract  # lazy: registry imports actions, which imports this

	rows = _update_rows(action)
	if not (action.target_doctype and rows):
		raise ValueError("Update Field action missing target doctype or rows")
	for row in rows:
		if not fields.is_settable(action.target_doctype, row["name"], axes):
			raise PermissionError(
				f"{row['name']} on {action.target_doctype} is not entitled to this workflow's grain"
			)
	if any(row.get("mode") == refs.INCREMENT for row in rows):
		doctype, name = resolve_target(action, lead, trigger_doc, context)
		# Only a row that EXISTS can be locked. With no name this reads `WHERE name IS NULL`, which locks
		# nothing while looking as though it did; on the insert leg there is no contender to lock against.
		if name:
			frappe.db.get_value(doctype, name, "name", for_update=True)
	tdoc = _resolve_write_target(action, lead, trigger_doc, context)
	for row in rows:
		if tdoc.meta.get_field(row["name"]) is None:
			raise ValueError(
				f"{row['name']!r} is not a field on {action.target_doctype} — it may belong to a child "
				"table; use Upsert Child Row to write fields on a child table"
			)
	for row in rows:
		tdoc.set(row["name"], contract.resolve_row(
			row.get("mode"), row.get("value"), context, current=tdoc.get(row["name"]),
		))
	_save_target(tdoc)
	# AFTER the save, because an insert has no name before it — which is what makes the next write an update.
	if action.target_doctype in subjects.WRITE_TARGETS:
		remember_wrote(context, action.target_doctype, tdoc.name)


def _update_rows(action):
	"""The rows this node writes. Refuses the pre-W8.1 single-field shape LOUDLY.

	Authored configs are folded to rows by `patches/fold_update_field_into_rows.py`, so the only way the
	old shape still reaches here is out of a `CRM Workflow Version` — which is content-addressed and
	immutable by design, so it cannot be migrated and a journey parked on one executes what was frozen.
	Reading `updates` and finding nothing would write nothing and say nothing; the raise makes it a
	journey that visibly fails, which is the only honest answer for a rule whose author is long gone.
	"""
	rows = [r for r in (action.updates or []) if isinstance(r, dict) and r.get("name")]
	if rows or not action.fieldname:
		return rows
	raise ValueError(
		f"this Update Field node was frozen before W8.1 and still sets one field ({action.fieldname}); "
		f"republish the workflow so its nodes carry rows"
	)


def _action_add_comment(action, lead, context, axes, trigger_doc):
	"""ADD_COMMENT — a native `doc.add_comment()` on the rule's SUBJECT record (always the lead:
	a Lead trigger's subject is itself; a Task trigger's subject is its parent Lead, resolved by
	watch._subject). `add_comment` inserts a Comment row without re-saving the subject, so it cannot
	re-fire the Field-Changed dispatcher on that doc (no new re-entrancy surface)."""
	from tatva_connect.automation import expr

	if action.comment_mode == refs.EXPRESSION:
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
	from tatva_connect.workflow_engine import contract  # lazy: registry imports actions, which imports this

	section = _child_target(action)
	rows = _child_rows(action)
	_assert_child_in_grain(section.target_doctype, section.child_table_field, {r["name"] for r in rows}, axes)
	tdoc = _resolve_write_target(action, lead, trigger_doc)
	tdoc.append(section.child_table_field, {
		r["name"]: contract.resolve_row(r.get("mode"), r.get("value"), context) for r in rows
	})
	_save_target(tdoc, section.child_table_field)


def _action_upsert_child(action, lead, context, axes, trigger_doc):
	"""UPSERT_CHILD_ROW — write the lead's row in a section, adding it when the lead has none.

	W8.3 — HOW the row is found is the section's own declaration (`lead.multirow.row_for_section`, the
	rule the Data tab and every Smart View already read), never a match map this node restates. The node
	that restated it could be authored two ways the runtime and the publish gate disagreed about.

	W8.1 — rows carry {mode, value} and resolve through `contract.resolve_row`, THE one reader, so a
	counter on a child row is INCREMENTED exactly as a field on the lead is.

	W8.2 — `Increment by` reads the row it is about to write, so the parent is locked first. The lock is
	unconditional here: with no row yet, two concurrent journeys would otherwise both append one."""
	from tatva_connect.lead import multirow
	from tatva_connect.workflow_engine import contract  # lazy: registry imports actions, which imports this

	section = _child_target(action)
	rows = _child_rows(action)
	_assert_child_in_grain(section.target_doctype, section.child_table_field, {r["name"] for r in rows}, axes)
	frappe.db.get_value(*resolve_target(action, lead, trigger_doc), "name", for_update=True)

	tdoc = _resolve_write_target(action, lead, trigger_doc)
	held = tdoc.get(section.child_table_field) or []
	if not section.is_multi_row and len(held) > 1:
		raise ValueError(
			f"{section.title} declares one row and this lead has {len(held)} rows — "
			"refusing to write into a section whose row is ambiguous"
		)
	row = multirow.row_for_section(tdoc, section)
	values = {r["name"]: contract.resolve_row(
		r.get("mode"), r.get("value"), context, current=(row.get(r["name"]) if row else None),
	) for r in rows}
	if row:
		for name, value in values.items():
			row.set(name, value)
	else:
		tdoc.append(section.child_table_field, values)
	_save_target(tdoc, section.child_table_field)


def _child_rows(action):
	"""The rows this child-row node writes — `_update_rows`' twin, and refusing the pre-W8.3 JSON shape
	for the same reason it refuses the pre-W8.1 one: a config frozen into a `CRM Workflow Version` cannot
	be migrated, so reading `set_fields` and finding nothing would write nothing and say nothing."""
	rows = [r for r in (action.set_fields or []) if isinstance(r, dict) and r.get("name")]
	if rows:
		return rows
	if action.get("set_json") or action.get("match_json"):
		raise ValueError(
			"this child-row node was authored as JSON (set_json/match_json), which this engine no longer "
			"reads — re-author it on the canvas as Section + Fields to set"
		)
	raise ValueError("a child-row node needs at least one field to set")





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
	out of it into named journey variables, so every downstream node reads them like any other value; and
	`success_when` — the same predicate control the Trigger and Route use — decides which of the node's
	two outputs the journey takes. No `success_when` means the HTTP status decides.

	Runs INLINE rather than deferred: an output the journey must route on cannot arrive after the journey has
	already moved past this node. A transport failure is not an exception here — it is the `failed`
	output, which is a graph the author can handle.
	"""
	endpoint = action.webhook_endpoint
	if not endpoint:
		raise ValueError("Call API node missing an endpoint")
	# The "endpoint exists" check MOVED to the publish gate (`graph._endpoint_problems`): a deleted Webhook
	# is author error the author now learns at publish, not from the first journey. A published graph reaches
	# here only with a real endpoint, so `_call_endpoint`'s own `get_doc` is the whole resolution.

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
	the journey down. We do NOT use `make_request`: it raises on any non-2xx, and a failed call here is data
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
	"""THE one writer of journey state for a Call API — the declared response shape, then the author's rows.

	`emits` promises `status`, `ok` and `error` are always written, and `upstream` offers them to every
	node downstream; nothing ever wrote them, so a Route on `ok` published green and then raised on the
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
	"""The response as journey state, in the TYPES the verb declares — `Int`, `Check`, `Data`.

	One shaping, two consumers: what a downstream node reads and what `success_when` is judged against
	are the same values, so an author's predicate on `ok` cannot mean one thing at the node and another
	on the Route after it. `ok` is a Check (1/0, never True/False) because the evaluator resolves
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
	fire behind `Workflow::Engine::sends` (OFF by default, A.6) and, once the operator flips it,
	sends through the EXISTING WATI brain (grain-routed account + template, A.11/A.8). This handler
	only resolves the action's config off the rule row; no adapter logic lives here.

	`_output` names the edge the journey leaves by, exactly as Call API does — the ONE routing mechanism,
	validated by `interpreter._verb_output` against what the verb declares. The decision itself belongs
	to `sends`, which is the module that knows why a send did not happen."""
	from tatva_connect.automation import sends

	output, result = sends.send_whatsapp(
		resolve_target(action, lead, trigger_doc)[1],
		action.contact_number,
		action.whatsapp_template, context, action.template_values,
		# The token `_run_verb` minted for THIS node, so a delivery receipt can find this run and no other.
		correlation=context.get(refs.TOKEN),
		# The template's media-header placeholder and the file to resolve into it — an ordinary named
		# parameter to WATI, so both stay declarations here and the send path owns the resolution.
		document_variable=action.get("document_variable"),
		document_file=action.get("document_file"),
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


def _action_place_voice_call(action, lead, context, axes, trigger_doc):
	"""AI VOICE CALL (effect, W7.4) — the SAME dormant sends gate as Send WhatsApp. The node always places
	a SINGLE call (our engine is one-journey-per-lead); the cohort/batch path is W7.2. `sends.send_voice`
	records the fire behind `Workflow::Engine::sends` (OFF by default) and, in pass 2, will resolve against
	the Bolna adapter. This handler only reads the action's config; no adapter logic lives here."""
	from tatva_connect.automation import sends

	output, result = sends.send_voice(
		resolve_target(action, lead, trigger_doc)[1],
		action.contact_number, action.connection, action.agent_id, context,
		from_override=action.get("from_override"),
		# The author's declared row per agent placeholder — resolved in `sends`, never read from state here.
		values=action.get("agent_values"),
		# The token `_run_verb` minted for THIS node, carried to the provider so a terminal webhook wakes this run.
		correlation=context.get(refs.TOKEN),
		# The author's own tick, carried through untouched — it is a provider request field, not a gate.
		bypass_guardrails=bool(action.get("bypass_guardrails")),
	)
	context[refs.OUTPUT] = output
	return result


def _action_generate_document(action, lead, context, axes, trigger_doc):
	"""GENERATE DOCUMENT (effect, W13) — render a pre-authored template to a PDF, then park.

	DISPATCH, THEN PARK, and that is the whole difference from Call API. The synchronous answer is only
	"was the render accepted" (`queued`/`failed`); the thing a journey really waits for — the document
	existing — is an OUTCOME the render job delivers, so a Wait placed after this node can name it. Call
	API declares outputs and no outcomes, which is exactly why nothing can ever wait on one.

	THE PDF IS OWNED BY A `CRM Campaign Document`, NEVER BY THE LEAD. `file_events.may_be_public()`
	classifies a file by its OWNING doctype's place on the operator allowlist, so filing a marketing
	document on the lead would mean publishing every lead attachment, clinical files included. Creating
	that row here is all this handler does about files: no privacy flag, no Azure call, no second decider.

	THE RENDER RIDES `workflow` LIKE EVERY OTHER ENGINE JOB. It was written for `long` on the belief that a
	render is seconds of subprocess work; it is not — measured on this app's own template, 0.5s, the same
	order as the WhatsApp and voice provider calls that already ride this lane. `test_workflow_lane_isolation`
	holds the whole engine on one lane so a burst can never starve `wakeups.sweep`, and buying a carve-out in
	that lock for a cost the measurement does not show would be trading a real invariant for a guess. The
	unbounded case — pdfkit fetching a remote image with no timeout of its own — is answered where it lives,
	by the job's own death penalty (`timeout=`), not by moving lanes. Deferred past commit, so a segment that
	rolls back renders nothing.

	Gated on `Document::Generation::render` HERE rather than inside the job (D9): a dormant bench must
	dispatch no work at all, so the node takes its `failed` edge exactly as the voice channel does.
	"""
	subject = resolve_target(action, lead, trigger_doc)[1]
	# Both keys are DECLARED emitted, so both are written on EVERY leg — a key that appears only on the
	# happy path could not honestly be offered downstream at all (the `assigned_to` lesson). `document_file`
	# is not knowable yet: the render job reports the real File name on the outcome a Wait accepts.
	context["campaign_document"] = None
	context["document_file"] = None
	if not action.document_template:
		raise ValueError("Generate Document node missing a template")

	if not document_render.render_enabled():
		context[refs.OUTPUT] = "failed"
		return "failed: document generation is switched off"

	# THE one filler (`sends._filled_rows`): a slot with no row is author error and raises, a row that
	# resolves blank routes to `failed` — a gap in a document a patient reads is the same defect as a gap
	# in a message they read, and a fourth copy of that loop is what that function exists to prevent.
	values, blank = sends._filled_rows(
		document_template_slots(action.document_template), action.get("document_values"), context,
		f"Generate Document: template {action.document_template}",
	)
	if blank:
		context[refs.OUTPUT] = "failed"
		return f"failed: {', '.join(blank)} resolved blank for lead {subject}, so the document would carry a gap"

	row = frappe.get_doc({
		"doctype": document_render.CAMPAIGN_DOCUMENT_DT,
		"lead": subject,
		"template": action.document_template,
		"status": document_render.QUEUED,
	}).insert(ignore_permissions=True)  # authz-ok: tier-a — automation effect lane (after-commit); rules are operator-built
	context["campaign_document"] = row.name
	context[refs.OUTPUT] = "queued"
	frappe.enqueue(
		"tatva_connect.workflow_engine.document_render.render_document",
		queue="workflow",
		enqueue_after_commit=True,
		timeout=document_render.RENDER_TIMEOUT_SECONDS,
		campaign_document=row.name,
		subject_doctype=fields.LEAD_DT,
		subject_name=subject,
		# The token `_run_verb` minted for THIS node, so the ready signal wakes this run and no other.
		correlation=context.get(refs.TOKEN),
		values=values,
		file_name=action.get("file_name"),
	)
	return f"queued: {row.name}"


@frappe.whitelist()
def document_template_slots(template):
	"""The inputs this `Web Template` really declares — the rows the node's Document Values grid offers.

	A `Web Template` already IS "markup plus a child table declaring its inputs", which is the reason it is
	the document template rather than a doctype of ours: the author is offered the names the render will
	really look up, so nothing is typed from memory. Read off the template's own `fields`, never a regex
	over its markup — a regex would drift from the renderer the first time a template used a block.

	One function, three callers, exactly as `email_template_slots` is: the authoring grid, the publish gate
	(which refuses an unmapped input), and the handler that fills them. A second reader is how a gate comes
	to check a shape nobody sends.
	"""
	if not frappe.has_permission("CRM Workflow", "read"):
		frappe.throw(_("Not permitted"), frappe.PermissionError)
	if not template or not frappe.db.exists("Web Template", template):
		return []
	return [row.fieldname for row in (frappe.get_doc("Web Template", template).get("fields") or []) if row.fieldname]


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



# The ONE action-lane registry (A.8): every verb's lane is declared exactly once here, read by
# `run_effects`/`_run_action`. Adding a verb = one row here, never a second lane table.
# `CRMAutomationRule.validate()` rejects any action_type not present here at author time (Task 8) — a
# verb sitting in the Select with no row here (e.g. Wait, before Task 9) can never reach a rule.
# THE verb declaration. One entry per verb: which lane it runs in, which handler runs it, how it reads
# to an author, and the parameters it takes. The parameters used to live in `describe._VERB_PARAMS`,
# which meant the thing that DECLARED a verb's inputs and the thing that READ them were in different
# modules and could disagree. They are now next to the handler that consumes them.
#
#   lane "effect" — runs after the save, inside the node's savepoint, and may never block.
#
# There is NO guard lane any more (Phase 11): `Require Fields` and `Require Location` were the only two
# verbs in it, they ran inside `validate` and raised, and a workflow that can refuse a rep's save is a
# second brain over the record's own rules. What they demanded is a Trigger `predicate` about the
# workflow itself; the location rule is the task type's, enforced once by `location.api`.
#
# `emits` are the journey-state VARIABLES a verb writes, so a downstream node can be offered them instead of
# asking the author to type a name from memory. Static keys are listed here; a verb whose keys depend on
# its own config (Call API's `capture` rows) names the config field in `emits_from` and the resolver
# reads the author's rows. This is the same lesson as `outcomes`, applied to state instead of events:
# a name typed blind is a name that can be typed wrong, and nothing notices until a journey behaves oddly.
#
# `outcomes` are the events a verb can later EMIT. A verb that emits nothing finishes and the journey moves
# on; a verb that emits is something the world answers — a task someone completes, a message someone
# replies to — and a Wait downstream can name one of those outcomes and suspend until it arrives. The
# names are declared here so a Wait offers a CHOICE rather than a free-text box: a typo in an event name
# used to mean a journey that parks for ever with nothing able to wake it.
VERBS = {
	"Assign to User": {
		"lane": "effect", "handler": _action_assign_to_user, "target": TARGET_LEAD,
		"label": "Assign to User",
		"description": "Moves ownership of the lead. Use it when ownership changes because something happened.",
		"outputs": ["assigned", "nobody"],
		"emits": [{"name": "assigned_to", "type": "Link", "about": "who now holds the lead"}],
		"params": [
			{"name": "assign_mode", "label": "Mode", "help": "Assign adds this person alongside anyone already on the record. Reassign clears the others first.", "type": "Select",
			 "options": ["Assign", "Reassign"], "reqd": True,
			 # A pool always reassigns — `do_assignment` clears the record first — so the choice is not offered rather than offered and ignored.
			 "depends_on_value": {"assignee_mode": ["User", "From Variable"]}},
			{"name": "assignee_mode", "label": "Assign to", "help": "Name one person here, take whoever an earlier node worked out, or hand it to a pool and let its rota decide.", "type": "Select",
			 "options": ["User", "From Variable", POOL], "reqd": True},
			# `User` carries no grain axis, so the picker cannot be scoped by columns — it DECLARES the kind.
			{"name": "assign_to_user", "label": "User", "help": "Only people entitled to this workflow's grain are offered — widen the Trigger's grain to see more.", "type": "Link", "link": "User",
			 "scope": "entitled_users",
			 "depends_on_value": {"assignee_mode": ["User"]}},
			{"name": "assignee_variable", "label": "Take the user from", "help": "The value must hold a user's login id. Values come from the nodes above this one.", "type": "Variable",
			 "depends_on_value": {"assignee_mode": ["From Variable"]}},
			{"name": "assignment_rule", "label": "Pool", "help": "Who is in the pool and whose turn it is are the rule's own settings, under Assignment Rule. This node only says when to draw from it.", "type": "Link", "link": "Assignment Rule",
			 "depends_on_value": {"assignee_mode": [POOL]}},
			# A pool writes the rule's OWN description on the ToDo (`do_assignment`), so a note here would be silently dropped.
			{"name": "assign_note", "label": "Note", "help": "Optional line shown with the assignment, so the person knows why it reached them. A pool uses the rule's own description instead.", "type": "Data",
			 "depends_on_value": {"assignee_mode": ["User", "From Variable"]}},
		],
	},
	"Create Task": {
		"lane": "effect", "handler": _action_create_task, "target": TARGET_LEAD,
		"label": "Create Task",
		"description": "Raises a task on the lead.",
		"outcomes": ["task.completed", "task.cancelled"],
		"params": [
			{"name": "task_type", "label": "Task Type", "help": "Decides the task's own fields and who may complete it. Task types are set up under Taxonomy.", "type": "Link", "link": "CRM Task Type", "reqd": True},
			# The same assignee controls as Assign to User — the two verbs answer the same question so they share
			# one vocabulary. When neither is chosen (the default, in every existing workflow) the old auto rule
			# fires: carry the trigger's assignee forward, falling back to the lead's owner.
			{"name": "assignee_mode", "label": "Assign to", "help": "Name one person here, or take whoever an earlier node worked out. Leave empty to carry the trigger's assignee forward.", "type": "Select",
			 "options": ["User", "From Variable"]},
			{"name": "assign_to_user", "label": "User", "help": "Only people entitled to this workflow's grain are offered — widen the Trigger's grain to see more.", "type": "Link", "link": "User",
			 "scope": "entitled_users",
			 "depends_on_value": {"assignee_mode": ["User"]}},
			{"name": "assignee_variable", "label": "Take the user from", "help": "The value must hold a user's login id. Values come from the nodes above this one.", "type": "Variable",
			 "depends_on_value": {"assignee_mode": ["From Variable"]}},
			# The subject trio MIRRORS Create Note's — text an author writes, built from context the one way it is built anywhere; a second shape for "write some text" is a second thing to learn.
			{"name": "subject_mode", "label": "Subject Mode", "help": "Type the subject, or build it from values the run is carrying. Leave it unset and the task is named after its type.", "type": "Select",
			 "options": [refs.LITERAL, refs.EXPRESSION]},
			{"name": "subject_text", "label": "Subject", "help": "Exactly what the rep reads on their task list.", "type": "Data",
			 "depends_on_value": {"subject_mode": [refs.LITERAL]}},
			{"name": "subject_expression", "label": "Subject Expression", "help": "Must produce text, e.g. \"Call \" + ctx[\"crm_lead.first_name\"].", "type": "Small Text", "reads": "expression",
			 "depends_on_value": {"subject_mode": [refs.EXPRESSION]}},
			{"name": "priority", "label": "Priority", "help": "How urgent this is on the rep's list. Leave it unset and the task keeps the priority the record itself defaults to.", "type": "Select",
			 "options": _priority_options()},
			{"name": "description", "label": "Note", "help": "A line of instruction shown under the subject, e.g. \"Patient has not uploaded the documents\". Leave it blank and the task carries no note.", "type": "Small Text"},
			# Duplicate suppression, exposed. `create_followup_task` throttles by default — one OPEN task
			# per lead per type, narrowed by this node's token — and that default is preserved by leaving
			# this unticked. Tick it and every fire raises its own task, which is what LeadSquared does.
			{"name": "allow_duplicate_tasks", "label": "Allow duplicate tasks", "help": "Off (the default): if this node already has an open task of this type for the patient, it is reused instead of raising another. On: every fire raises a new task, even when one is still open.", "type": "Check"},
			{"name": "due_mode", "label": "Due Mode", "help": "Leave it unset for the task type's own default due date.", "type": "Select",
			 "options": [refs.FROM_CONTEXT, refs.EXPRESSION, DUE_AFTER_DELAY]},
			{"name": "due_from", "label": "Due date from", "help": "A date carried by the run — the patient's appointment, or a date an earlier node worked out.", "type": "Variable",
			 "depends_on_value": {"due_mode": [refs.FROM_CONTEXT]}},
			{"name": "due_expression", "label": "Due Expression", "help": "Date arithmetic, e.g. add_days(ctx[\"crm_lead.creation\"], 7).", "type": "Small Text", "reads": "expression",
			 "depends_on_value": {"due_mode": [refs.EXPRESSION]}},
			# The Wait's own delay control, so "in 14 days" is authored the same way wherever it is written.
			{"name": "due_delay", "label": "Due after", "help": "How long after this node runs the task falls due.", "type": "Duration",
			 "depends_on_value": {"due_mode": [DUE_AFTER_DELAY]}},
		],
	},
	"Update Field": {
		"lane": "effect", "handler": _action_set_field, "target": TARGET_AUTHORED,
		"label": "Update Field",
		"description": "Writes values onto fields the operator has allowed automation to set.",
		# W8.1 — one row per field with the mode ON THE ROW, replacing five params and the three gates they needed.
		"params": [
			{"name": "target_doctype", "label": "Write to", "help": "Which record is written — the patient's lead, or the record that started the journey.", "type": "Target", "reqd": True},
			# `doctype_from` names the sibling holding these fields' doctype; `modes` adds the two no template slot can take.
			{"name": "updates", "label": "Fields to set", "help": "One row per field. Only fields an operator has allowed automation to write are offered; the rest are set up under Automation Fields.", "type": "Field Map", "reqd": True,
			 "doctype_from": "target_doctype",
			 "modes": [refs.LITERAL, refs.FROM_CONTEXT, refs.EXPRESSION, refs.INCREMENT]},
		],
	},
	# W8.3 — authored exactly like Update Field: `child_table` names a CRM Lead Section, its columns are rows.
	"Append Child Row": {
		"lane": "effect", "handler": _action_append_child, "target": TARGET_LEAD,
		"label": "Append Child Row",
		"description": "Adds a row to a section on the lead.",
		"params": [
			{"name": "child_table", "label": "Section", "help": "Which of the lead's sections gains a row.", "type": "Link", "link": "CRM Lead Section", "reqd": True},
			{"name": "set_fields", "label": "Fields to set", "help": "One row per field on the new row.", "type": "Field Map", "reqd": True,
			 "doctype_from": "child_table",
			 "modes": [refs.LITERAL, refs.FROM_CONTEXT, refs.EXPRESSION]},
		],
	},
	"Upsert Child Row": {
		"lane": "effect", "handler": _action_upsert_child, "target": TARGET_LEAD,
		"label": "Upsert Child Row",
		"description": "Updates the lead's row in a section, or adds one if it has none.",
		"params": [
			{"name": "child_table", "label": "Section", "help": "Which of the lead's sections is written. How its row is found is the section's own declaration, not this node's.", "type": "Link", "link": "CRM Lead Section", "reqd": True},
			{"name": "set_fields", "label": "Fields to set", "help": "One row per field. Increment by adds to what the row already holds.", "type": "Field Map", "reqd": True,
			 "doctype_from": "child_table",
			 "modes": [refs.LITERAL, refs.FROM_CONTEXT, refs.EXPRESSION, refs.INCREMENT]},
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
			{"name": "webhook_endpoint", "label": "Endpoint", "help": "Where the call goes, and the credential it goes with. Endpoints are curated under Webhook — this node cannot name a URL of its own.", "type": "Link", "link": "Webhook", "reqd": True},
			{"name": "webhook_payload_source", "label": "Send", "help": "Send the whole record, or write the body yourself.", "type": "Select",
			 "options": ["Lead", "Trigger Doc", "Custom"]},
			# The author says WHAT is sent; the curated Webhook still says WHERE. `reads=ctx_json` is what
			# makes its references visible to the publish gate, at any depth.
			{"name": "request_body", "label": "Request Body", "help": "Write $ctx.<name> anywhere, at any depth, to drop in a value the run is carrying.", "type": "Code",
			 "reads": "ctx_json", "depends_on_value": {"webhook_payload_source": ["Custom"]},
			 "placeholder": '{"model": "gpt-4o", "messages": [{"role": "user", "content": "$ctx.crm_lead.first_name"}]}'},
			# WHICH RECORD the Test call is built from. Declared because a preview's args are read off this
			# node's own config, and because a response is SHAPED by the record behind it: a capture tree
			# built from whatever lead was modified last teaches the author paths the next patient will not
			# have. The picker is narrowed to the workflow's grain by the link target's own axes.
			{"name": "preview_lead", "label": "Test with", "help": "Whose record the Test call below is built from. Only patients inside this workflow's grain are offered. Leave it blank and the most recently updated one is used.", "type": "Link", "link": "CRM Lead"},
			# `preview` declares that this control can fetch a REAL answer: the method, and its sibling args.
			{"name": "capture", "label": "Capture", "help": "Names for the parts of the response later nodes should be able to read. Press Test call to see a real response and pick from it.", "type": "Mapping",
			 "preview": {
				 "method": "tatva_connect.workflow_engine.context.test_call",
				 "args": {"endpoint": "webhook_endpoint", "request_body": "request_body", "lead": "preview_lead"},
			 }},
			{"name": "success_when", "label": "Succeeded when", "help": "Which responses count as success and take the Succeeded branch. Leave it blank and any 2xx does.", "type": "Predicate"},
		],
	},
	"Create Note": {
		"lane": "effect", "handler": _action_add_comment, "target": TARGET_LEAD,
		"label": "Create Note",
		"description": "Adds a note to the lead's timeline.",
		"params": [
			{"name": "comment_mode", "label": "Mode", "help": "Type the note, or build it from values the run is carrying.", "type": "Select",
			 "options": [refs.LITERAL, refs.EXPRESSION]},
			{"name": "comment_text", "label": "Text", "help": "Exactly what appears on the timeline.", "type": "Data",
			 "depends_on_value": {"comment_mode": [refs.LITERAL]}},
			{"name": "comment_expression", "label": "Expression", "help": "Must produce text, e.g. \"Called \" + ctx[\"crm_lead.first_name\"].", "type": "Small Text", "reads": "expression",
			 "depends_on_value": {"comment_mode": [refs.EXPRESSION]}},
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
			{"name": "contact_number", "label": "Mobile Number", "help": "Picked, never typed — a typed number is how a message reaches the wrong person.", "type": "Variable", "reqd": True},
			{"name": "whatsapp_template", "label": "Template", "help": "Only approved templates can be sent. They are set up under WhatsApp Templates, and the template decides which values are asked for below.", "type": "Link",
			 "link": "WhatsApp Templates", "reqd": True},
			# Which buttons this send OFFERS. The author declares them; a downstream Wait draws one branch per row. Never synced from the provider and never inferred from whatever arrives.
			{"name": "buttons", "label": "Buttons offered", "help": "Buttons this message offers. Each one draws its own branch on a Wait placed after this node, so a tap can be routed by wiring rather than by a condition.", "type": "Button List"},
			# The template's placeholders, DECLARED. `reads=value_rows` is what makes them visible to
			# `contract.reads_of` and therefore refusable by the publish gate; `slots_from` names the
			# sibling holding the template whose real placeholder names the control offers.
			# `preview` is Call API's own declaration, reused verbatim: the method that fetches a REAL answer, and its sibling args.
			{"name": "template_values", "label": "Template Values", "help": "One row per blank in the template. Every blank needs a row — a missing one sends the patient an empty space.", "type": "Value Map",
			 "slots_from": "whatsapp_template",
			 "slots_method": "tatva_connect.automation.sends.template_slots",
			 "preview": {
				 "method": "tatva_connect.automation.sends.whatsapp_template_preview",
				 "args": {"template": "whatsapp_template", "values": "template_values"},
			 }},
			# The document header (D6), and it is TWO ORDINARY CONTROLS because a media header is an ordinary
			# named parameter to the provider — the header placeholder is matched by NAME like every other
			# blank, so nothing about the send path is special-cased for it. DECLARED ALWAYS, never gated on
			# a mode: the authoring experience is singular and nothing here morphs. Blank means this message
			# carries no document, which is what every send does today.
			{"name": "document_variable", "label": "Document placeholder", "capability": "media", "type": "Data",
			 "help": "The name the template's own header gives its document, e.g. pdfLink. Copy it exactly — the provider matches by name, not by position. Leave it blank when the message carries no document.",
			 "placeholder": "pdfLink"},
			# `capability: media` on BOTH, so a provider that does not declare media renders neither — one filter (`params_of` -> `offered_fields`), never a second answer to "does this control exist here".
			{"name": "document_file", "label": "Document to attach", "capability": "media", "type": "Variable",
			 "help": "Which file goes into that header — pick the document an earlier Generate Document node produced. A file that is still private, or gone, sends nothing and takes the Failed branch."},
		],
	},
	"Send Email": {
		"lane": "effect", "handler": _action_send_email, "target": TARGET_LEAD,
		"label": "Send Email",
		"description": "Sends an email after the segment commits, and routes on whether it could be sent.",
		"outputs": [sends.SENT, sends.FAILED],
		"params": [
			# Picked from the grouped picker, never typed. The literal path this used to carry mailed a phone-shaped string as an address.
			{"name": "email_recipient", "label": "Recipient", "help": "Picked, never typed. It must hold an email address.", "type": "Variable", "reqd": True},
			# Frappe's own template store. There is no compose box: every message goes through the org's template chain.
			{"name": "email_template", "label": "Template", "help": "There is no compose box — every message goes through the org's templates, set up under Email Template.", "type": "Link", "link": "Email Template", "reqd": True},
			# The same Value Map WhatsApp uses; only the slot SOURCE differs, because an Email Template names its variables.
			# The email twin: two readers, because rendering a WhatsApp body is the provider's job and rendering an email's is ours.
			{"name": "template_values", "label": "Template Values", "help": "One row per variable the template names. Every one needs a row.", "type": "Value Map",
			 "slots_from": "email_template",
			 "slots_method": "tatva_connect.automation.sends.email_template_slots",
			 "preview": {
				 "method": "tatva_connect.automation.sends.email_template_preview",
				 "args": {"template": "email_template", "values": "template_values"},
			 }},
		],
	},
	"AI Voice Call": {
		"lane": "effect", "handler": _action_place_voice_call, "target": TARGET_LEAD,
		"label": "AI Voice Call",
		"description": "Places an outbound AI voice call and routes on whether it was handed to the provider.",
		# Synchronous: did we place it. `placed` = accepted for dialling, never "answered". The LATER outcomes
		# (answered · completed · no_answer) come from the channel declaration via `outcomes_channel`, EXCLUDING
		# these synchronous outputs — the same race-closing exclusion Send WhatsApp uses. Nothing typed twice.
		"outputs": [sends.PLACED, sends.FAILED],
		"outcomes_channel": "voice",
		# THE ORDER IS THE AUTHORING SEQUENCE, and it is declared, not incidental. The account gates the
		# agent and the agent gates its values, so the chain is answered top to bottom: account, agent,
		# what the agent says, who it calls, which number it calls from. Declaring the recipient first —
		# which is what WhatsApp does, because nothing there is gated — meant an author met two controls
		# reading "choose an account first" BEFORE meeting the account, and three controls looked broken.
		"params": [
			# 1. The account. Everything below is scoped by it, so it is asked first.
			{"name": "connection", "label": "Voice account", "help": "The provider account this call is placed on. Everything below belongs to it, so choose it first. Accounts are set up under AI Voice Account.", "type": "Link",
			 "link": "CRM AI Voice Account", "reqd": True, "placeholder": "Select an account"},
			# 2. The agents on THAT account, fetched server-side (the api key never reaches the browser) and
			# cached. `options_from` names the sibling holding the account, so the picker empties and
			# refetches when the author changes it rather than offering another account's agents.
			{"name": "agent_id", "label": "Agent", "help": "Which agent speaks. Agents are created on the provider, not here — press Refresh after adding one.", "type": "Remote Select", "reqd": True,
			 "options_from": "connection",
			 "options_method": "tatva_connect.voice.api.list_agents",
			 "detail_method": "tatva_connect.voice.api.get_agent",
			 "detail_label": "what this agent says",
			 "placeholder": "Select an agent",
			 "gate_text": "Pick a voice account first — the agents belong to it.",
			 "empty_text": "This account has no agents yet. Create one on the provider, then Refresh."},
			# 3. The agent's OWN placeholders, declared row by row — the voice twin of `template_values`, and
			# for the identical reason: an undeclared slot is not a blank on a screen, it is "Hi
			# customer_name" spoken to a patient. `slots_args` hands the control the sibling account,
			# because an agent id means nothing without the account it lives on.
			{"name": "agent_values", "label": "What the agent says", "help": "One row per blank the agent's script names. An unfilled blank is spoken aloud to the patient as it is written.", "type": "Value Map",
			 "slots_from": "agent_id",
			 "slots_args": {"account": "connection"},
			 "slots_method": "tatva_connect.voice.api.agent_slots"},
			# 4. Who it calls. Picked, never typed: a ref to a phone, conformed by the declared E164_PLUS.
			# "Mobile Number" is the ONE author-facing word for a person's number (ruled 2026-08-05); the
			# stored column (`CRM Workflow Step Log.contact`) and the wire key (`to_number`) do not move.
			{"name": "contact_number", "label": "Mobile Number", "help": "Picked, never typed — a typed number is how a call reaches the wrong person.", "type": "Variable", "reqd": True},
			# 5. Which number it calls FROM. Picked from the numbers the account really owns — a typed
			# from-number the provider does not own is rejected at dial time, which is a failed journey
			# found on a live lead instead of at author time. Blank is a real answer, and the placeholder
			# says what blank DOES rather than restating the label.
			{"name": "from_override", "label": "From number", "help": "Only numbers this account really owns are offered. Leave it blank to use the account's own.", "type": "Remote Select",
			 "options_from": "connection",
			 "options_method": "tatva_connect.voice.api.list_phone_numbers",
			 "placeholder": "The account's own number",
			 "gate_text": "Pick a voice account first — the numbers belong to it.",
			 "empty_text": "This account owns no numbers. The agent's own default is used."},
			# 6. Last, and OFF: it qualifies the whole node rather than any field above it, as the Trigger's "Only once per patient" does. Offered only because a voice adapter DECLARES `bypass_guardrails` — it is a provider request field, not a guardrail of ours, and `params_of` drops it for a provider that has none.
			{"name": "bypass_guardrails", "type": "Check", "capability": "bypass_guardrails",
			 "label": "Skip the agent's calling hours",
			 "help": "Dials as soon as the journey reaches this node instead of waiting for the agent's configured calling hours. Useful for testing a journey end to end."},
		],
	},
	"Generate Document": {
		"lane": "effect", "handler": _action_generate_document, "target": TARGET_LEAD,
		"label": "Generate Document",
		"description": "Renders a pre-authored template to a PDF for this patient, and reports when the document is ready to send.",
		# The SYNCHRONOUS answer, and only that: was the render accepted. Whether it succeeded arrives later.
		"outputs": ["queued", "failed"],
		# STATIC, following Create Task, and the single most important line here: it is what makes this node
		# WAITABLE. A render is answered by our own job rather than by a channel's adapters, so there is no
		# channel to derive the list from; the two names live beside the job that delivers them.
		"outcomes": [document_render.DOCUMENT_READY, document_render.DOCUMENT_FAILED],
		# POINTERS, never a URL: a journey parks for days and a URL captured into state was true once. The
		# file's real address is derived at send time, from the File row, by the node that needs it (I5).
		"emits": [
			{"name": "campaign_document", "type": "Link", "about": "the record the document is filed on"},
			{"name": "document_file", "type": "Link", "about": "the rendered file, once the render reports"},
		],
		# THE ORDER IS THE AUTHORING SEQUENCE, as AI Voice Call's is: the template gates its own inputs, so
		# it is asked first; what the document is CALLED is last, because it qualifies the finished artefact.
		"params": [
			{"name": "document_template", "label": "Template", "help": "The document's layout and wording. Templates are authored under Web Template, and the one you pick decides which values are asked for below.", "type": "Link",
			 "link": "Web Template", "reqd": True, "placeholder": "Select a template"},
			# The template's OWN declared inputs, row by row — the same control and the same reasoning as
			# `template_values`: an undeclared input is not a blank on a screen, it is a gap in a document a
			# patient reads. `slots_from` names the sibling holding the template whose inputs these are.
			{"name": "document_values", "label": "Document Values", "help": "One row per input the template declares. Every one needs a row — a missing one leaves a hole in the document.", "type": "Value Map",
			 "slots_from": "document_template",
			 "slots_method": "tatva_connect.automation.actions.document_template_slots"},
			{"name": "file_name", "label": "File name", "help": "What the document is called when the patient receives it. Leave it blank and it is named after its own record.", "type": "Data",
			 "placeholder": "The record's own name"},
		],
	},
}


def emits_of(verb, config=None):
	"""The journey-state variables this verb writes, given how it is configured.

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
	"""Every verb in a lane, in declaration order. The ONE way to ask 'what can act'. `guard` is empty by
	construction now (Phase 11) — no verb may block a save — and asking for it must answer nothing."""
	return [verb for verb, declared in VERBS.items() if declared["lane"] == lane]


# -- value + child helpers ---------------------------------------------------


def _subject(action, context):
	"""The line a rep reads on their list, or None for the task type's own name.

	The resolution is Create Note's, because the question is Create Note's: text the author typed, or text
	built from what the run is carrying. Blank is a real answer and it is today's behaviour — the helper
	then labels the task after its type, which is also what keeps the composite task_type key off a screen.
	"""
	from tatva_connect.automation import expr

	if action.get("subject_mode") == refs.EXPRESSION:
		text = expr.resolve_expression(action.get("subject_expression"), context)
		# A non-string is author error and says so; nothing at all degrades to the type's name, as a blank subject already does.
		if text is not None and not isinstance(text, str):
			raise ValueError("Create Task subject expression did not evaluate to a string")
	else:
		text = action.get("subject_text")
	return (text or "").strip() or None


def _due_at(action, context):
	"""Resolve a Create Task due date, coercing defensively: a non-datetime value degrades to None
	(create_followup_task then applies its default lead time) rather than dropping the task. Three
	modes — From Context (read a context key), Expression (safe_eval against ctx), and After a delay
	(a length of time from the moment this node runs)."""
	from tatva_connect.automation import expr

	if action.due_mode == DUE_AFTER_DELAY:
		# ONE delay arithmetic — the Wait's own resolver, based on now: the same delay written on either node must land on the same instant, and a delay that is not one is refused there in the author's words.
		raw = wait_resume_at(action.get("due_delay"), context, frappe.utils.now_datetime()) if action.get("due_delay") else None
	elif action.due_mode == refs.EXPRESSION:
		raw = expr.resolve_expression(action.due_expression, context)
	else:  # From Context is the `else`, so a renamed Expression falls silently to the default due date.
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
	"""Every journey-state reference the authored body names — the same walk `build_request_body` performs.

	Exported so the publish gate asks THIS module what the body reads, rather than re-deriving it from a
	structure it would have to learn the shape of independently.
	"""
	found = []
	_walk(_parsed_body(raw), lambda v: found.append(v[len(refs.CTX_PREFIX):]) if isinstance(v, str) and v.startswith(refs.CTX_PREFIX) else v)
	return [name for name in found if name]



def _child_target(action):
	"""The `CRM Lead Section` this node writes — resolved by the section brain, whichever spelling the
	config holds. The section already validated that its `child_table_field` is a real Table field on
	CRM Lead holding `target_doctype` rows (`CRMLeadSection.validate`), so nothing is re-checked here."""
	from tatva_connect.partner_api.doctype.crm_lead_section import crm_lead_section

	section = crm_lead_section.section_for_child(action.child_table)
	if not section:
		raise ValueError(f"{action.child_table!r} is not a section on CRM Lead that lives in a child table")
	return section




def _assert_child_in_grain(child_dt, child_table, fieldnames, axes):
	"""Every field written must be a catalog row for the child table entitled at the lead's grain.
	Fail-closed. One brain (fields.is_settable).

	The doctype asked about is the LEAD, never the child doctype: the lead catalog (`CRM Lead API Field`)
	is where a child field is declared, and WHICH child table it lands in is derived from its
	`CRM Lead Section` — which is exactly what `child_table_field` disambiguates. Asking about the child
	doctype fell through `is_settable`'s `doctype != LEAD_DT` floor and refused every field, so no
	Append/Upsert Child Row node could ever run. `child_dt` is kept for the message, which is what an
	author reads.
	"""
	for f in fieldnames:
		if not fields.is_settable(fields.LEAD_DT, f, axes, child_table_field=child_table):
			raise PermissionError(
				f"{f} on {child_dt} ({child_table}) is not entitled to this workflow's grain"
			)
