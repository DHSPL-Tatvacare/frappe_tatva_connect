"""The Field-Changed trigger entry seam - the sibling of dispatcher.fire_rules.

`fire_field_change_rules` is the sync on_update hook (CRM Lead + CRM Task): cheap, synchronous,
must-be-safe. It diffs the watched fields (the Watchable registry, per doctype) and ENQUEUES
`run_for_field_change` to run AFTER the save commits, in its own transaction - so a rule, however
malformed/slow/deadlock-prone, can never add latency to or roll back the user's save (spec §5.2,
same posture as fire_rules).

`run_for_field_change` re-fetches the doc, resolves the SUBJECT (Lead -> itself; Task -> its parent
Lead), resolves grain via the SAME `rules.lead_axes` accessor, matches Field-Changed rules via
`rules.matching_rules_for_field_change`, builds the context (`doc.get_valid_dict()` + the watched
field's `__before` value), and feeds each matched rule to the EXISTING `dispatcher._run_rule` -
the same backbone `fire_rules` uses. No parallel engine: a different entry point feeding the same
guarded executor, savepoint, allowlist recheck, and Run Log.

Re-entrancy is the SAME `frappe.flags.in_automation` guard fire_rules uses - an automation-caused
save (a Set Field action's `tdoc.save()`) re-enters run_for_field_change's entry hook and returns
immediately. The kill switch is the SAME `Task::Automation::rules` toggle - one engine, one switch.

Concurrency posture (audit M2 — shared with the Task-Completed engine, not a regression): dispatch is
enqueue-after-commit with a per-doc `job_id` + `deduplicate`, which coalesces QUEUED duplicates but not
one already running — so a rapid double-save of the same doc can fire a no-criteria rule twice (idempotent
Create Task softens this; Add Comment / Set Field are at-least-once). The `{field}__before` payload is
captured at enqueue time while current values are re-fetched at run time, so on rapid A→B→C edits an
in-flight job sees before=A / current=C — which makes `changed_from_to` a clean NON-match (safe) rather
than a misfire. Bounded to rapid edits; matches the shipped Task-Completed path's semantics.
"""
import frappe

from tatva_connect import automation
from tatva_connect.automation import fields, rules, subjects
from tatva_connect.automation.dispatcher import _log_error, _run_rule

KILL_SWITCH = "Task::Automation::rules"  # reused - one toggle for the whole automation engine


def fire_field_change_rules(doc, method=None):
	"""CRM Lead / CRM Task on_update (sync): if any watched field changed, enqueue the dispatch to
	run after the save commits. Dormant + fail-closed + non-re-entrant (same posture as fire_rules)."""
	if frappe.flags.get("in_automation"):
		return  # re-entrancy guard (request-scoped): a write the engine made must not re-enter it
	if not automation.is_enabled(KILL_SWITCH):
		return
	if doc.is_new():
		return  # a new doc has no before-state to diff - nothing fires on the first save
	changed = _diff_watched_fields(doc)
	if not changed:
		return
	frappe.enqueue(
		"tatva_connect.automation.watch.run_for_field_change",
		queue="short",
		enqueue_after_commit=True,
		now=bool(frappe.flags.get("in_test")),
		job_id=f"automation-field-change::{doc.doctype}::{doc.name}",
		deduplicate=True,  # a job already queued/running for this doc is not re-queued (no double-fire)
		doctype=doc.doctype,
		docname=doc.name,
		changed=changed,
	)


def run_for_field_change(doctype, docname, changed):
	"""Background entry (after the save commits): rebuild context + dispatch, in our own txn.
	A deadlock/abort here can only roll back THIS job - never the user's already-committed save."""
	frappe.flags.in_automation = True
	try:
		doc = frappe.get_doc(doctype, docname)
		subject = _subject(doc)
		if subject is None:
			return  # no resolvable subject -> no rule can fire (fail-closed, same shape as fire_rules)
		axes = rules.lead_axes(subject.name)
		matched = rules.matching_rules_for_field_change(axes[0], axes[1], axes[2], doctype, list(changed))
		if not matched:
			return
		context = _context_for(doc, changed)
		field_types = _field_types_for(doctype)
		grain = "{}::{}::{}".format(axes[0] or "", axes[1] or "", axes[2] or "")
		# Reuse the SAME backbone fire_rules uses - per-rule savepoint, guarded actions, run log.
		for r in matched:
			try:
				_run_rule(r, subject.name, context, doc, axes, grain, field_types)
			except Exception as e:
				_log_error(r.name, "(rule)", grain, e)
	except Exception:
		frappe.log_error(title="automation: field-change dispatch failed", message=frappe.get_traceback())
	finally:
		frappe.flags.in_automation = False


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
	"""The Field-Changed context: the doc's own persistable fields (get_valid_dict) PLUS, for each
	changed watched field, a `{field}__before` key carrying the old value - the pair the
	`changed_from_to` operator and Expression authors read. ONE builder for both Lead and Task -
	no doctype dispatch, no second copy of reconstruct_values (which is Task-Completed's brain)."""
	context = doc.get_valid_dict()
	for fieldname, (old, _new) in changed.items():
		context[f"{fieldname}__before"] = old
	return context


def _subject(doc):
	"""The rule's subject - the Lead the actions act ON. Name resolution is delegated to the ONE brain
	(subjects.resolve_lead_name): Lead → itself, Task → its parent Lead. Returns the Lead DOC so callers
	read `.name` + fields; None for an unresolvable subject (fail-closed, no rule fires). A Lead trigger
	returns the already-fetched doc (no reload)."""
	lead_name = subjects.resolve_lead_name(doc)
	if not lead_name:
		return None
	return doc if doc.doctype == "CRM Lead" else frappe.get_doc("CRM Lead", lead_name)


def _field_types_for(doctype):
	"""{fieldname: schema type} for the watched doctype's meta fields, so criteria evaluate
	type-aware. Reuses `describe.fields_for_doctype` - the same vocabulary the builder + validator
	read (one brain, no parallel schema query)."""
	from tatva_connect.automation.describe import fields_for_doctype

	return {f["key"]: f["type"] for f in fields_for_doctype(doctype)}


def _watchable_fields_for(doctype):
	"""The enabled can_watch fieldnames for a doctype, cached per-request (fire_field_change_rules runs
	on every watched-doctype save; the query brain is fields.watchable_fields — this is just the cache)."""
	cache = frappe.flags.setdefault("_watchable_fields_cache", {})
	if doctype not in cache:
		cache[doctype] = fields.watchable_fields(doctype)
	return cache[doctype]
