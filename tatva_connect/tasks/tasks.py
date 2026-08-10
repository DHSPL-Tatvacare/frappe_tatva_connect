"""CRM Task automations: seed + enforce checklists, the bulk-complete gate, idempotent follow-up helper."""
import frappe
from frappe import _
from frappe.utils import add_to_date, cint, now_datetime

from tatva_connect import automation
from tatva_connect.taxonomy import grain, labels

DONE_STATUS = "Done"
CLOSED_STATUSES = ("Done", "Canceled")
# A type_name, never a PK — resolved to this lead's grain-scoped type by the ONE resolver.
CALL_LEAD_TYPE = "Call Lead"


def on_lead_assignment(doc, method=None):
	"""ToDo.after_insert — when a CRM Lead is assigned to an agent, raise ONE open
	'Call Lead' task for that agent (the on-lead-create follow-up). Fires after the
	Assignment Rule sets the owner; gated by the master switch (OFF by default)."""
	from tatva_connect.activity.api import resolve_type_for_lead

	if doc.reference_type != "CRM Lead" or not doc.allocated_to:
		return
	if not automation.is_enabled("Task::Assignment::followup"):
		return
	lead = frappe.db.get_value(
		"CRM Lead", doc.reference_name, ["lead_name", "custom_current_program"], as_dict=True
	) or frappe._dict()
	lead_name = lead.lead_name or doc.reference_name
	# Dormant until a grain seeds a Call Lead type — the same rule the first-activity mapping follows.
	call_type = resolve_type_for_lead(doc.reference_name, CALL_LEAD_TYPE)
	if call_type:
		create_followup_task(
			lead=doc.reference_name,
			task_type=call_type,
			due_in_hours=24,
			assigned_to=doc.allocated_to,
			title=_("Call lead — {0}").format(lead_name),
		)
	# Field-sales: if the lead's program names a first activity type (config), raise ONE open
	# activity-task of it (reuses the same idempotent throttle). Dormant when the mapping is unset.
	first_type = lead.custom_current_program and frappe.db.get_value(
		"CRM Program", lead.custom_current_program, "custom_first_activity_type"
	)
	# Guard: a stale/renamed mapping must never abort the assignment ToDo insert.
	if first_type and frappe.db.exists("CRM Task Type", first_type):
		try:
			create_followup_task(
				lead=doc.reference_name,
				task_type=first_type,
				due_in_hours=48,
				assigned_to=doc.allocated_to,
				title=_("{0} — {1}").format(first_type, lead_name),
			)
		except Exception:
			frappe.log_error("on_lead_assignment: first activity-task creation failed")


def seed_checklist(doc, method=None):
	"""Fill a task's checklist from the most-specific template for the linked lead's
	(Product Line / Group / Program) + the task's type. Runs at creation, or when a
	type is first set on an existing task. No type / no matching template -> no
	checklist (task closes freely). Never overwrites a caller-supplied checklist."""
	if not automation.is_enabled("Task::CRM Task::guards"):
		return
	if doc.custom_checklist or not doc.custom_task_type:
		return
	if not doc.is_new():
		before = doc.get_doc_before_save()
		if before and before.custom_task_type == doc.custom_task_type:
			return  # type unchanged on an existing task — nothing to seed

	tmpl = resolve_template(doc.custom_task_type, *_lead_axes(doc))
	if not tmpl:
		return
	for row in tmpl.items:
		doc.append("custom_checklist", {"item": row.item, "required": row.required, "done": 0})


def enforce_checklist(doc, method=None):
	"""Block marking a task Done while a required checklist item is unticked. Fires
	on both close paths (modal save and the quick status dropdown both run validate)."""
	if not automation.is_enabled("Task::CRM Task::guards"):
		return
	if doc.status != DONE_STATUS:
		return
	pending = [r.item for r in (doc.custom_checklist or []) if r.required and not r.done]
	if pending:
		from tatva_connect.api._base import throw_by_audience

		throw_by_audience(
			_("Cannot mark Done — {0} checklist item(s) still pending: {1}").format(
				len(pending), ", ".join(pending)
			),
			_("This activity's checklist has {0} required item(s) still open: {1}. A checklist is "
			  "ticked by the assigned rep, so leave `status` as it is until they close it.").format(
				len(pending), ", ".join(pending)
			),
			["status"],
			title=_("Checklist incomplete"),
		)


def enforce_location(doc, method=None):
	"""Fail-closed BACKSTOP for the location guard (VAPT, A.1/S.3): guarantees coordinates on every save
	the rep's own form path (`activity.api.compute_activity` → `location.api.set_or_check_anchor`) does not
	cover — API / import / scripted saves. The gate lives once in `location.api.location_required`, fed by
	the reconstructed submitted values (one brain — same reconstruction the automation engine uses).

	It used to stand down when an authored 'Require Location' workflow covered the same save. That verb is
	gone (Phase 11) — a workflow decides whether IT runs, never whether a rep may save — and with it the
	only reason this backstop ever consulted the workflow engine. Location is declared once on the task
	type (`visit_mode` plus the location condition) and enforced here and in `compute_activity`, nowhere else."""
	from tatva_connect.activity.automation import reconstruct_values
	from tatva_connect.location.api import location_required

	if not automation.is_enabled("Task::CRM Task::guards"):
		return
	# Location is captured when the visit is LOGGED (Done), not while the task is an open to-do.
	# An open/assigned in-person task legitimately has no coordinates yet — only block on completion.
	if doc.status != DONE_STATUS:
		return
	if doc.reference_doctype != "CRM Lead" or not doc.reference_docname:
		return
	values = reconstruct_values(doc)
	if location_required(doc.custom_task_type, doc.reference_docname, values) is None:
		return
	if not (doc.custom_location_latitude and doc.custom_location_longitude):
		from tatva_connect.api._base import throw_by_audience

		# ONE rule, two readers: a rep has a Tasks list and a capture button, an HTTP caller has neither
		# and cannot send coordinates at all (they are not a writable activity field), so it is told the
		# only thing it CAN do. The audience is decided once, in throw_by_audience.
		throw_by_audience(
			_("Capture your location at the doctor's site to complete this visit — mark it Done from the "
			  "Tasks list or open the task."),
			_("A {0} activity records where the visit happened, and coordinates are captured by the "
			  "field app on the device — they cannot be sent over the API. Leave `status` as it is and "
			  "let the assigned rep complete the visit, or ask the operator to clear `Location When` on "
			  "this activity type.").format(labels.label(doc.custom_task_type, labels.TASK_TYPE)),
			["status"],
			title=_("Location required"),
		)
	if not doc.custom_location_captured_at:
		doc.custom_location_captured_at = now_datetime()


def enforce_activity_logged(doc, method=None):
	"""Fail-closed guarantee: a form-activity task cannot be completed (Done) without its details
	logged. Holds on EVERY save path — the quick status dropdown / API / import all run validate,
	not just the Form-view controller. The clean UX (the client opens the activity form on
	completion) sits on top of this; if that UX ever breaks, completion degrades to a clear block,
	never a silent empty activity. One brain: activity.api.activity_is_unlogged owns the rule."""
	from tatva_connect.activity.api import activity_is_unlogged

	if not automation.is_enabled("Task::CRM Task::guards"):
		return
	if activity_is_unlogged(doc):
		from tatva_connect.api._base import throw_by_audience

		throw_by_audience(
			_("Log this activity's details before marking it Done — open the task and fill its form."),
			_("This activity carries no logged details, so it cannot be marked Done. Send its fields "
			  "under `values` in the same call that sets `status` — activity_schema lists the fields "
			  "{0} takes.").format(labels.label(doc.custom_task_type, labels.TASK_TYPE)),
			["values"],
			title=_("Activity not logged"),
		)


@frappe.whitelist()
def submit_cancel_or_update_docs(doctype, docnames, action="submit", data=None, task_id=None):
	"""Frappe's own bulk-update entry, gated by ONE refusal: a `CRM Task Type` carrying
	`disable_bulk_complete` cannot be completed from the list's bulk action, and the refusal names it.
	Everything else — every other doctype, every other field, every other action — reaches the unchanged
	native function, which is why this is a wrapper and not a fork (the `access.native_guards` shape,
	wired through `override_whitelisted_methods`).

	IT HAS TO BE THE ENTRY POINT, and not a `validate` hook, for two independent reasons. Core's
	`_bulk_action` wraps every per-document save in `except Exception: log_error(); failed.append(name)`,
	so a throw inside `validate` is swallowed: the rep gets a success toast, the message never reaches a
	screen, and only an Error Log records it. And in principle a `validate` cannot express this rule at all
	— it cannot tell the bulk lane from the rep's own form, and the flag is about the lane, not the value.

	Frappe resolves an override only for a call arriving through the HTTP handler, which is where the
	list's bulk action arrives from. A Python caller reaching the native function directly is unaffected
	and is meant to be: `disable_bulk_complete` is a rule about the list's bulk action, not about `Done`.
	"""
	from frappe.desk.doctype.bulk_update.bulk_update import submit_cancel_or_update_docs as _native

	_refuse_disabled_bulk_complete(doctype, docnames, action, data)
	return _native(doctype, docnames, action, data, task_id)


def _refuse_disabled_bulk_complete(doctype, docnames, action, data):
	"""Refuse the whole bulk call when any selected task's type forbids bulk completion.

	The whole call, not the offending rows: a partial bulk that silently skipped some of the selection is
	how a rep comes to believe an activity was logged when no form was ever filled. `frappe.get_all` (not
	`get_list`) reads the types, because a task the caller cannot see must still be counted — a guard that
	under-refuses is not a guard. The types are named by their clean labels, never the composite `::` PK.
	"""
	if doctype != "CRM Task" or action != "update":
		return
	values = frappe.parse_json(data) if isinstance(data, str) else data
	if not isinstance(values, dict) or values.get("status") != DONE_STATUS:
		return
	names = frappe.parse_json(docnames) if isinstance(docnames, str) else docnames
	if not names:
		return
	types = {
		t for t in frappe.get_all("CRM Task", filters={"name": ("in", list(names))}, pluck="custom_task_type") if t
	}
	if not types:
		return
	blocked = frappe.get_all(
		"CRM Task Type",
		filters={"name": ("in", sorted(types)), "disable_bulk_complete": 1},
		pluck="name",
	)
	if not blocked:
		return
	frappe.throw(
		_("{0} is completed one activity at a time — open each task and log its form. Take it out of the "
		  "selection to bulk-update the rest.").format(
			", ".join(sorted(labels.label(t, labels.TASK_TYPE) or t for t in blocked))),
		title=_("Bulk complete not allowed"),
	)


@frappe.whitelist()
def create_followup_task(lead, task_type, due_in_hours=4, assigned_to=None, title=None, due_at=None,
                         throttle=True, node_token=None, priority=None, description=None):
	"""Idempotent follow-up task. Throttle (default): ONE open task per lead per type — if one
	is already open, return it untouched. Otherwise create it (assigned + due at
	`due_at` if given, else `due_in_hours` from now). Also the method the WhatsApp
	inbound event calls.

	`throttle=False` skips the one-open-per-lead-per-type check and always inserts a fresh task. The
	Document Review flow uses it because its idempotency is PER DOCUMENT (keyed on the File's
	custom_review_task back-reference by its caller), not per lead+type — several reviewable files on
	one lead must each get their own review task, never collapse onto the first.

	`node_token` NARROWS the throttle key, it does not remove it: the open-task lookup then also matches
	`custom_workflow_token`, so the key is lead + type + THAT node instead of lead + type. This is what
	lets a journey put two Create Task nodes of the same type either side of a Wait — the second node no
	longer collides with the first node's still-open task — while the SAME node re-firing (retry, replay,
	a loop iteration) still finds its own open task and stays idempotent. A wider key would silently drop
	the second task; no key at all would pile duplicates onto a patient, so it is narrower, never absent.
	The token is engine bookkeeping written once by `actions._stamp_workflow_token` (the one writer, and
	it never moves an existing token) — this helper only READS it, and does not stamp.

	`priority` is set on the inserted task when given; when None the doctype's own default stands.
	`description` is the same shape: the task's own note, written only when the caller supplies one.
	Both new arguments default to None, which is byte-for-byte today's behaviour for every caller.

	THE ONE EXCEPTION to "every writer goes through compute_activity", and it is a real one: this
	creates a schema-less SHELL — an open to-do carries no submitted form, so there is nothing to
	resolve. The grain is not part of that exemption. A task type is available to a lead or it is not,
	and the answer comes from the same `_scope_applies` brain the picker and the activity gate use,
	read off the lead the task lands on (never a caller-passed axes tuple, which the durable Flow path
	leaves blank for a non-Lead subject).

	Race-free throttle: a row lock on the lead serializes concurrent creates, so simultaneous
	fires (automation engine / assignment / inbound) for the same lead can't slip two tasks past
	the check-then-insert."""
	from tatva_connect.activity.api import scope_applies_to_lead
	from tatva_connect.api._base import throw_field

	if not frappe.db.exists("CRM Lead", lead):
		throw_field(_(
			"No lead has the id `{0}`, so no follow-up can be raised against it. Check the value "
			"against a lead_list response."
		).format(lead), ["lead"], frappe.DoesNotExistError)

	# Gate the user-facing entrypoint: raising a follow-up task writes to the lead's record set, so the
	# caller must hold lead write. Internal automations (assignment / engine / inbound) run in the
	# triggering user's session and already hold it; the insert itself stays ignore_permissions so the
	# follow-up lands assigned even where the child docperm is narrower than lead access (one gate).
	frappe.has_permission("CRM Lead", "write", doc=lead, throw=True)

	if not scope_applies_to_lead(task_type, lead):
		throw_field(
			_("`{0}` is not an activity type this lead's grain runs, so no task of it can be raised. "
			  "Call activity_schema for this lead and pick a type it lists.").format(
				labels.label(task_type, labels.TASK_TYPE)),
			["task_type"],
		)

	# An enabled, non-Guest assignee only (a task's assignee can see its lead reference).
	if assigned_to and (assigned_to == "Guest" or not frappe.db.get_value("User", {"name": assigned_to, "enabled": 1})):
		throw_field(_(
			"`{0}` is not an enabled user, and a task is assigned only to one that can open the lead it "
			"references. Send the id of an enabled user, or omit `assigned_to` to leave it unassigned."
		).format(assigned_to), ["assigned_to"])

	# Lock the lead row so the check-then-insert below is serialized per lead (no duplicate task).
	frappe.db.get_value("CRM Lead", lead, "name", for_update=True)

	if throttle:
		filters = {
			"reference_doctype": "CRM Lead",
			"reference_docname": lead,
			"custom_task_type": task_type,
			"status": ["not in", CLOSED_STATUSES],
		}
		# A node's token narrows the key to that node — same node reuses its own open task, a different
		# node of the same type gets its own instead of silently colliding with the first one's.
		if node_token:
			filters["custom_workflow_token"] = node_token
		existing = frappe.db.get_value("CRM Task", filters, "name")
		if existing:
			return existing

	due_date = due_at or add_to_date(now_datetime(), hours=cint(due_in_hours))
	# Title defaults to the CLEAN activity name (type_name), never the grain-composite `::` PK — a
	# task_type is keyed vertical::group::program::type_name (A.7), and that key must never leak into
	# a user-facing title. Falls back to the raw value only if the type row is somehow missing.
	task = frappe.get_doc(
		{
			"doctype": "CRM Task",
			"title": title or labels.label(task_type, labels.TASK_TYPE),
			"custom_task_type": task_type,
			"status": "Todo",
			"due_date": due_date,
			"assigned_to": assigned_to,
			"reference_doctype": "CRM Lead",
			"reference_docname": lead,
			# Every caller of this helper is an automation (lead-assignment follow-up, activity
			# transition, WhatsApp inbound) -> stamp it so the timeline can badge it as automated.
			"custom_automated": 1,
			# Written HERE because the throttle above FILTERS on it: a key this function narrows by and a
			# caller writes afterwards is two brains, and the gap between them is a duplicate task.
			"custom_workflow_token": node_token,
		}
	)
	# Only set when the author asked for one — an unset priority leaves the doctype's own default standing,
	# and the allowed values are the column's, validated by the doctype, never a typed list in here.
	if priority:
		task.priority = priority
	# Same rule as `priority` directly above: written only when asked for, so an unset note leaves the
	# doctype's own default standing rather than blanking a description a caller never spoke about.
	if description:
		task.description = description
	task.insert(ignore_permissions=True)  # authz-ok: tier-b — gated by frappe.has_permission on the task before the write
	return task.name


# -- helpers -----------------------------------------------------------------


def _lead_axes(doc):
	"""(vertical, group, program) of the linked lead, or blanks if not lead-linked."""
	if doc.reference_doctype == "CRM Lead" and doc.reference_docname:
		return grain.of("CRM Lead", doc.reference_docname)
	return "", "", ""


def resolve_template(task_type, vertical, group, program):
	"""Most-specific-wins checklist template for the lead's grain — a thin caller of
	the shared grain brain (taxonomy.grain.resolve_scoped). No global default; a set
	axis must match, a blank axis is a wildcard; an exact tie -> raise; no match -> None."""
	from tatva_connect.taxonomy.grain import resolve_scoped

	candidates = [
		{"name": c.name, "vertical": c.vertical, "group": c.psp_group, "program": c.program}
		for c in frappe.get_all(
			"CRM Task Checklist Template",
			filters={"task_type": task_type, "enabled": 1},
			fields=["name", "vertical", "psp_group", "program"],
		)
	]
	winner = resolve_scoped(candidates, vertical, group, program)
	return frappe.get_doc("CRM Task Checklist Template", winner["name"]) if winner else None
