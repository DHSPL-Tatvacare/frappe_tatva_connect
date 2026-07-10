"""The unified wildcard event router — the ONE trigger entry seam for the automation engine.

TATVA v2 (Task 4) collapses the old two dispatchers (`dispatcher.fire_rules` — the Task-Completed
path, CRM Task on_update Done-flip; and `watch.fire_field_change_rules` — the Field-Changed path,
CRM Lead/Task on_update watched-field diff) into ONE router keyed on `(on_doctype, event)`:
  • Created = `after_insert` — no before-state, no diff, no `changed…` operators.
  • Updated = `on_update` — carries `{field}__before` for every watched field that changed.
  • Deleted = `on_trash` — no before-state either (same as Created), and unlike the other two the
    trigger DOC ITSELF is gone by the time the after-commit job runs; `on_deleted` captures the
    subject + trigger context synchronously (while the row still exists) and threads them through
    the enqueue payload instead of letting the background job re-fetch anything — see its docstring.
"Task Completed" no longer exists as a path: it is an Updated rule whose criteria include
`status changed to Done` (the shape `patches.reshape_automation_triggers` already produced for
every migrated rule) — the Updated path subsumes it.

Registered on the wildcard `doc_events["*"]` (hooks.py, same precedent as `assignment_rule.apply`
and our own `intake.route_submission`) — the no-code-push design: a doctype is "live" for
automation only because an ENABLED rule names it (`live_doctypes`, cached + self-healing on any
rule save/delete), never because of a per-doctype hook.

Every downstream brain is reused, not reinvented (A.8, one brain): subject resolution
(`subjects.resolve_lead_name`), grain (`rules.lead_axes`), the field-diff and context builder
(moved here from `watch.py`, logic unchanged), the matcher (`rules.matching_rules`), and the
two-lane executor (`dispatcher.run_guards`/`run_effects`, Task 5) — same re-entrancy guard
(`frappe.flags.in_automation`), same kill switch (`Task::Automation::rules`), same
enqueue-after-commit + `job_id`/`deduplicate` pattern the old dispatchers used.

TATVA v2 (Task 5): `run_guards` below is the SYNCHRONOUS entry (wired on `"*"` `validate` in
hooks.py) — a matched rule's guard actions run inline and a raise blocks the save. `on_created`/
`on_updated`/`run_for_event` are the ASYNC entry (after-commit) — unchanged in shape, now calling
`dispatcher.run_effects` (effect-lane actions only; guard actions on the same rule already ran, or
blocked the save, in validate).

Concurrency posture (carried over, not a regression): dispatch is enqueue-after-commit with a
per-(doc, event) `job_id` + `deduplicate`, which coalesces QUEUED duplicates but not one already
running — a rapid double-save of the same doc can fire a no-criteria rule twice (idempotent Create
Task softens this; Update Field / Create Note are at-least-once). The `{field}__before` payload is
captured at enqueue time while current values are re-fetched at run time, so on rapid A→B→C edits
an in-flight job sees before=A / current=C — a clean NON-match for `changed_from_to` (safe), not a
misfire.
"""
import frappe

from tatva_connect import automation
from tatva_connect.automation import dispatcher, rules, subjects, versions
from tatva_connect.automation.dispatcher import _log_error

KILL_SWITCH = "Task::Automation::rules"  # the ONE toggle for the whole automation engine
_LIVE_DOCTYPES_CACHE_KEY = "automation:live_doctypes"


def live_doctypes() -> set:
	"""The distinct `on_doctype` of every ENABLED rule — the cheap guard set the wildcard router
	checks before doing any real work. Memoised in frappe.cache; busted on any CRM Automation Rule
	save/delete (crm_automation_rule.py's on_change/on_trash) so it never serves a stale set. Same
	shape as intake._intake_doctypes (the wildcard-router guard-set precedent)."""
	cached = frappe.cache().get_value(_LIVE_DOCTYPES_CACHE_KEY)
	if cached is None:
		cached = set(frappe.get_all("CRM Automation Rule", filters={"enabled": 1}, pluck="on_doctype"))
		frappe.cache().set_value(_LIVE_DOCTYPES_CACHE_KEY, cached)
	return cached


def clear_live_doctypes_cache(doc=None, method=None):
	"""Drop the memoised live-doctype guard set — the ONE clear point, called from the Rule
	controller's on_change/on_trash so the wildcard router never serves a stale set after a rule is
	added, toggled, or removed."""
	frappe.cache().delete_value(_LIVE_DOCTYPES_CACHE_KEY)


def on_created(doc, method=None):
	"""Wildcard `after_insert` (doc_events["*"]) — Created. No before-state to diff: any live
	doctype's insert enqueues the dispatch outright. Dormant + fail-closed + non-re-entrant (same
	guard ladder as the old dispatchers)."""
	if frappe.flags.get("in_automation"):
		return  # re-entrancy guard (request-scoped): a write the engine made must not re-enter it
	if not automation.is_enabled(KILL_SWITCH):
		return
	if doc.doctype not in live_doctypes():
		return
	_enqueue(doc.doctype, doc.name, "Created", {})


def run_guards(doc, method=None):
	"""Wildcard `validate` (doc_events["*"]) — the SYNCHRONOUS GUARD lane (Task 5). Runs BEFORE the
	save commits: a matched rule's guard actions run inline here, and a raise propagates straight out
	of validate, blocking the save (S.1/S.3 — never swallowed). Reuses the SAME subject/context brains
	as the after-commit effect lane below (`_subject`, `_diff_watched_fields`, `_context_for`) — one
	context builder, no second copy (A.8). Effect-lane actions on the same rule are NOT run here —
	router.on_created/on_updated enqueue those after commit, as before.

	Axes read straight off the in-memory subject doc (`_subject_axes`), NOT `rules.lead_axes` (a DB
	lookup by name) — on a brand-new Lead (Created), validate runs BEFORE `db_insert`, so the row
	doesn't exist yet and a by-name lookup throws "not found"."""
	if frappe.flags.get("in_automation"):
		return  # re-entrancy guard: a write the engine made must not re-enter its own guard lane
	if not automation.is_enabled(KILL_SWITCH):
		return
	if doc.doctype not in live_doctypes():
		return
	subject = _subject(doc)
	if subject is None:
		return  # no resolvable subject -> no rule can fire (fail-closed)
	axes = _subject_axes(subject)
	event = "Created" if doc.is_new() else "Updated"
	matched = rules.matching_rules(doc.doctype, event, axes[0], axes[1], axes[2])
	if not matched:
		return
	changed = {} if event == "Created" else _diff_watched_fields(doc)
	context = _context_for(doc, changed)
	field_types = _field_types_for(doc.doctype)
	for r in matched:
		dispatcher.run_guards(subject.name, versions.current_name(r.name), context, field_types)


def _subject_axes(subject):
	"""(vertical, group, program) read straight off the in-memory subject DOCUMENT — never a DB round
	trip. `_subject()` always returns the real CRM Lead doc (itself, or the loaded parent Lead for a
	Task subject), already grain-stamped by `before_validate` - so its own fields are the answer."""
	return (subject.get("custom_vertical") or "", subject.get("custom_group") or "", subject.get("custom_current_program") or "")


def on_updated(doc, method=None):
	"""Wildcard `on_update` (doc_events["*"]) — Updated. Diffs the doctype's watchable fields that
	actually changed on this save; nothing enqueues unless at least one did."""
	if frappe.flags.get("in_automation"):
		return
	if not automation.is_enabled(KILL_SWITCH):
		return
	if doc.is_new():
		return  # a new doc has no before-state to diff - nothing fires on the first save
	if doc.doctype not in live_doctypes():
		return
	changed = _diff_watched_fields(doc)
	if not changed:
		return
	_enqueue(doc.doctype, doc.name, "Updated", changed)


def on_deleted(doc, method=None):
	"""Wildcard `on_trash` (doc_events["*"]) — Deleted. Frappe runs `on_trash` BEFORE the row is
	actually removed, but this router's dispatch is enqueue-after-commit exactly like Created/Updated
	— so by the time the background job runs, the row is GONE. Unlike Created/Updated it therefore
	CANNOT re-fetch the trigger doc there. So everything the effect lane needs is captured
	SYNCHRONOUSLY here, right now, while `doc` still exists: the subject lead's name (`_subject`) and
	the trigger doc's own field dict (`doc.get_valid_dict()`), both threaded through the enqueue
	payload to `run_for_delete` instead of a bare (doctype, docname) the background job could re-fetch.
	The SUBJECT is the parent LEAD, not the row being deleted, so it still exists after commit and
	effects act on it normally. No before-state exists for a delete (same as Created) — no `changed…`
	criterion can match; `context` never carries a `__before` key, and authoring one on event=Deleted
	is already rejected at save time (CRMAutomationRule._validate_changed_operator)."""
	if frappe.flags.get("in_automation"):
		return  # re-entrancy guard
	if not automation.is_enabled(KILL_SWITCH):
		return
	if doc.doctype not in live_doctypes():
		return
	subject = _subject(doc)
	if subject is None:
		return  # no resolvable subject -> no rule can fire (fail-closed)
	context = doc.get_valid_dict()
	_enqueue_delete(doc.doctype, doc.name, subject.name, context)


def _enqueue_delete(doctype, docname, subject_name, context):
	"""Enqueue the Deleted-event dispatch to run after the delete commits — same enqueue-after-commit +
	job_id/deduplicate posture as `_enqueue`, but carrying the SUBJECT NAME and CONTEXT captured
	synchronously in `on_deleted` (the trigger row is gone by the time this job runs — there is nothing
	left for it to re-fetch)."""
	frappe.enqueue(
		"tatva_connect.automation.router.run_for_delete",
		queue="short",
		enqueue_after_commit=True,
		now=bool(frappe.flags.get("in_test")),
		job_id=f"automation-event::{doctype}::{docname}::Deleted",
		deduplicate=True,
		doctype=doctype,
		docname=docname,
		subject_name=subject_name,
		context=context,
	)


def run_for_delete(doctype, docname, subject_name, context):
	"""Background entry for the Deleted event (after the delete commits). The trigger row
	(doctype, docname) is GONE by now — unlike `run_for_event`, this does NOT re-fetch it: it builds an
	in-memory trigger doc straight from the CONTEXT captured synchronously in `on_deleted`
	(`frappe.get_doc({"doctype": ..., **context})` constructs an unsaved Document from a dict, no DB
	round trip — see get_doc's own docstring), so the SAME downstream verb handlers that read
	`trigger_doc.doctype`/`.name`/`.get(field)` (e.g. Create Task's assignee carry-over) keep working
	off the captured snapshot instead of a re-fetch that would 404. `subject_name` is the parent lead's
	name, resolved synchronously too — the subject is NOT the deleted row, so it still exists and its
	axes resolve from the DB exactly as for Created/Updated (`rules.lead_axes`). Reuses the SAME matcher
	+ effect executor as every other event (A.8) — no parallel engine. `context` never carries a
	`__before` key (no diff on a delete, exactly like Created), so a `changed…` criterion is a clean
	non-match, never a raise."""
	frappe.flags.in_automation = True
	try:
		axes = rules.lead_axes(subject_name)
		matched = rules.matching_rules(doctype, "Deleted", axes[0], axes[1], axes[2])
		if not matched:
			return
		field_types = _field_types_for(doctype)
		grain = dispatcher._grain_tag(*axes)
		trigger_doc = frappe.get_doc({"doctype": doctype, **context})
		for r in matched:
			try:
				dispatcher.run_effects(subject_name, versions.current_name(r.name), trigger_doc, axes, grain, field_types, context)
			except Exception as e:
				_log_error(r.name, "(rule)", grain, e)
	except Exception:
		frappe.log_error(title="automation: delete dispatch failed", message=frappe.get_traceback())
	finally:
		frappe.flags.in_automation = False


def _enqueue(doctype, docname, event_name, changed):
	"""Enqueue the background dispatch to run AFTER the save commits, in its own transaction — so a
	rule, however malformed/slow/deadlock-prone, can never add latency to or roll back the user's
	save (spec §5.2, carried over from both v1 dispatchers). NB: the kwarg is `event_name`, not
	`event` — `frappe.enqueue` itself reserves `event` for its own RQ job-clearing parameter."""
	frappe.enqueue(
		"tatva_connect.automation.router.run_for_event",
		queue="short",
		enqueue_after_commit=True,
		now=bool(frappe.flags.get("in_test")),
		job_id=f"automation-event::{doctype}::{docname}::{event_name}",
		deduplicate=True,  # a job already queued/running for this (doc, event) is not re-queued
		doctype=doctype,
		docname=docname,
		event_name=event_name,
		changed=changed,
	)


def run_for_event(doctype, docname, event_name, changed):
	"""Background entry (after the save commits): rebuild context + dispatch, in our own txn.
	A deadlock/abort here can only roll back THIS job — never the user's already-committed save."""
	frappe.flags.in_automation = True
	try:
		doc = frappe.get_doc(doctype, docname)
		subject = _subject(doc)
		if subject is None:
			return  # no resolvable subject -> no rule can fire (fail-closed)
		axes = rules.lead_axes(subject.name)
		matched = rules.matching_rules(doctype, event_name, axes[0], axes[1], axes[2])
		if not matched:
			return
		context = _context_for(doc, changed)
		field_types = _field_types_for(doctype)
		grain = dispatcher._grain_tag(*axes)
		# Reuse the SAME backbone both v1 dispatchers used - per-rule savepoint, guarded actions, run log.
		# EFFECT lane only (Task 5) - this rule's guard actions already ran (or blocked) in validate.
		# Each fire binds to the rule's CURRENT version and never reads the rule again (automation.versions).
		for r in matched:
			try:
				dispatcher.run_effects(subject.name, versions.current_name(r.name), doc, axes, grain, field_types, context)
			except Exception as e:
				_log_error(r.name, "(rule)", grain, e)
	except Exception:
		frappe.log_error(title="automation: dispatch failed", message=frappe.get_traceback())
	finally:
		frappe.flags.in_automation = False


def _subject(doc):
	"""The rule's subject - the Lead the actions act ON. Name resolution is delegated to the ONE brain
	(subjects.resolve_lead_name): Lead → itself, Task → its parent Lead. Returns the Lead DOC so callers
	read `.name` + fields; None for an unresolvable subject (fail-closed, no rule fires). A Lead trigger
	returns the already-fetched doc (no reload)."""
	lead_name = subjects.resolve_lead_name(doc)
	if not lead_name:
		return None
	return doc if doc.doctype == "CRM Lead" else frappe.get_doc("CRM Lead", lead_name)


def _diff_watched_fields(doc):
	"""Return {fieldname: (old, new)} for every watched field whose value changed on this save.
	Mirrors frappe/email/doctype/notification/notification.py's Value Change event: skip new docs,
	cast both sides through the field's own fieldtype, compare. NOT Document.has_value_changed() -
	that returns True unconditionally for new docs (wrong here; the caller already skips is_new but
	this stays self-contained). The Watchable registry is read per-doctype and cached for the request."""
	watched = _watchable_fields_for(doc.doctype)
	if not watched:
		return {}
	before = doc.get_doc_before_save()
	if not before:
		return {}  # no before-state (e.g. a migration re-save) - nothing to diff
	out = {}
	for fieldname in watched:
		df = doc.meta.get_field(fieldname)
		if df is None:
			continue  # a stale registry row pointing at a removed field - skip, don't crash
		old, new = before.get(fieldname), doc.get(fieldname)
		try:
			if frappe.utils.cast(df.fieldtype, old) != frappe.utils.cast(df.fieldtype, new):
				out[fieldname] = (old, new)
		except Exception:  # nosec B110 - an uncastable value falls back to raw equality
			if old != new:
				out[fieldname] = (old, new)
	return out


def _context_for(doc, changed):
	"""The trigger context: the doc's own persistable fields (get_valid_dict) PLUS, for each changed
	watched field, a `{field}__before` key carrying the old value - the pair the `changed_from_to`
	operator and Expression authors read - PLUS (Task 13) CRM Task's activity-schema values, keyed by
	their own schema fieldname, `setdefault`-merged so a genuine doc column always wins a name clash.
	ONE builder for every subject/event - no doctype dispatch, no second copy."""
	context = doc.get_valid_dict()
	for fieldname, (old, _new) in changed.items():
		context[f"{fieldname}__before"] = old
	for fieldname, value in _activity_values(doc).items():
		context.setdefault(fieldname, value)
	return context


def _activity_values(doc):
	"""CRM Task's activity-schema submitted values, keyed by their SCHEMA fieldname - NOT the promoted
	column / payload key `activity.api.compute_activity` actually routed them to. A rule criterion is
	authored against the schema fieldname (outcome/training_status/call_completed_next_steps/...,
	`describe.fields_for_doctype`'s vocabulary), so the context must expose the SAME name at fire time.
	Reuses the ONE existing brain (`activity.api._task_values` + `_type_config` - the exact merge the
	Lead task board/timeline already render from) instead of a second payload parse (A.8). Empty for a
	non-CRM-Task subject or a plain task with no activity type."""
	if doc.doctype != "CRM Task" or not doc.get("custom_task_type"):
		return {}
	from tatva_connect.activity.api import _task_values, _type_config

	cfg = _type_config(doc.custom_task_type)
	if not cfg:
		return {}
	return _task_values(doc, cfg)


def _field_types_for(doctype):
	"""{fieldname: schema type} for the trigger doctype's meta fields, so criteria evaluate
	type-aware. Reuses `describe.fields_for_doctype` - the same vocabulary the builder + validator
	read (one brain, no parallel schema query)."""
	from tatva_connect.automation.describe import fields_for_doctype

	return {f["key"]: f["type"] for f in fields_for_doctype(doctype)}


def _watchable_fields_for(doctype):
	"""The enabled can_watch fieldnames for a doctype, cached per-request (the router runs on every
	live-doctype save; the query brain is fields.watchable_fields - this is just the cache)."""
	from tatva_connect.automation import fields

	cache = frappe.flags.setdefault("_watchable_fields_cache", {})
	if doctype not in cache:
		cache[doctype] = fields.watchable_fields(doctype)
	return cache[doctype]
