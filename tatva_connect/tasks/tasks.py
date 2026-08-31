"""CRM Task automations: the bulk-complete gate and the idempotent follow-up helper."""
import frappe
from frappe import _
from frappe.utils import add_to_date, cint, now_datetime

from tatva_connect import automation
from tatva_connect.taxonomy import labels

DONE_STATUS = "Done"
CLOSED_STATUSES = ("Done", "Canceled")


def on_lead_reassignment_handover(doc, method=None):
	"""ToDo.after_insert — a lead's open tasks move to whoever the lead is now assigned to. Two separate lines, never crossed: whether this becomes a real ToDo follows Task::Assignment::assignee, the SAME line a manual reassignment already follows; notification is a wholly different line (Notify::Task::assigned) this function never touches either way. Guarded whole so a failure here can never fail the real assignment."""
	from tatva_connect.lead.assignment import TASK_ASSIGNEE, silent_assign

	try:
		if doc.reference_type != "CRM Lead" or not doc.allocated_to:
			return
		new_owner = doc.allocated_to
		open_tasks = frappe.get_all(
			"CRM Task",
			filters={
				"reference_doctype": "CRM Lead",
				"reference_docname": doc.reference_name,
				"status": ["not in", CLOSED_STATUSES],
				"assigned_to": ["!=", new_owner],
			},
			pluck="name",
		)
		real = automation.is_enabled(TASK_ASSIGNEE)
		for task in open_tasks:
			try:
				if real:
					silent_assign("CRM Task", task, new_owner)
				else:
					frappe.db.set_value("CRM Task", task, "assigned_to", new_owner, update_modified=False)
			except Exception:
				frappe.log_error(f"lead reassignment task handover failed: {task} -> {new_owner}")
	except Exception:
		frappe.log_error("on_lead_reassignment_handover aborted before touching any task")


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

	refuse_disabled_bulk_complete(doctype, docnames, action, data)
	return _native(doctype, docnames, action, data, task_id)


def refuse_disabled_bulk_complete(doctype, docnames, action, data):
	"""Refuse the whole bulk call when any selected task's type forbids bulk completion.

	Called by every bulk DOOR and by no other kind of caller: this wrapper, which Desk's own list view
	reaches through `override_whitelisted_methods`, and `bulk_actions.run_or_queue`, which the CRM app
	reaches instead. One rule, asked at each entrance, for the reason the wrapper above gives.

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
	"""THE HTTP ENDPOINT — `POST /api/method/tatva_connect.tasks.tasks.create_followup_task`.

	A caller reaching this over the wire is a principal asking to write on someone's lead, so it is
	asked to prove it. Everything after the check is `raise_followup_task`, which is what the SERVER's
	own callers use — see there for why they do not come through here.
	"""
	if not frappe.db.exists("CRM Lead", lead):
		from tatva_connect.api._base import throw_field

		throw_field(_(
			"No lead has the id `{0}`, so no follow-up can be raised against it. Check the value "
			"against a lead_list response."
		).format(lead), ["lead"], frappe.DoesNotExistError)
	frappe.has_permission("CRM Lead", "write", doc=lead, throw=True)
	return raise_followup_task(lead, task_type, due_in_hours, assigned_to, title, due_at,
	                           throttle, node_token, priority, description)


def raise_followup_task(lead, task_type, due_in_hours=4, assigned_to=None, title=None, due_at=None,
                        throttle=True, node_token=None, priority=None, description=None):
	"""The SERVER's own entrypoint — not whitelisted, and deliberately not permission-gated.

	A task the workflow engine raises is asked for by the OPERATOR who published the workflow, not by
	whoever happened to trip it. That distinction is not academic: an intake form submits as `Guest`, so
	gating on the triggering user meant every enrolment that should raise a task raised a PermissionError
	instead — silently, on a real patient. The engine's own gates are unchanged and do the real work:
	only a published workflow runs, its grain must match the lead, and `is_settable` clears every field
	it writes. Assignment, inbound and Document Review are server-initiated in exactly the same way.

	The wire path keeps its check, byte for byte, in `create_followup_task` above. Nothing a rep or a
	partner can reach is widened by this; the two callers were never the same caller.

	Idempotent follow-up task. Throttle (default): ONE open task per lead per type — if one
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
