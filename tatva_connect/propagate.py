# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A side-effect must never destroy a rep's save. The ONE helper that guarantees it.

THE DISASTER SHAPE. Frappe runs `doc_events` INSIDE the save's transaction (`document.py`
`run_post_save_methods`), so any exception a hook raises rolls the whole save back. A notification the
push service refused, a search index whose sqlite file is locked, a timeline pointer that hit a
duplicate — each of those is enough to stop a rep saving a patient record and lose their typing. The
side-effect is not the work; the record is.

THREE CATEGORIES OF HOOK, and only the third one is wrapped:

  GATE       enforces a business rule (`dedup_guard`, `enforce_location`, `normalize_lead_phones`).
             It MUST still block — a refused save is the whole point of it.
  DERIVE     fills fields on the document being saved (`stamp_entitled_grain`, `pin_inbound_reference`,
             `sync_headline_metrics`). A half-filled document is worse than a refused save.
  PROPAGATE  writes somewhere ELSE — a notification, the search index, the timeline rail, a metrics
             roll-up, a file bond, a workflow start. Nothing about the record being saved depends on it.

Every `validate` / `before_validate` hook in `hooks.py` is a gate or a derive. This decorator goes on
propagate hooks and nowhere else.

WHY A SAVEPOINT AND NOT A BARE `try/except`. If the failure came from the database — a deadlock, a
duplicate key, a truncated column — the transaction is ALREADY poisoned: every statement after it errors
with "current transaction is aborted", so swallowing the exception only moves the crash to the next
write, which is the rep's save. A savepoint is the only construct that undoes the hook's half-written
work and leaves the outer save intact. `automation/sends.py` proved this exact shape for exactly this
reason (a message on the wire that could not be recorded), and this is that shape, written once.

FRAPPE'S OWN PRIMITIVES, AND WHY NOT ITS OWN WRAPPER. The three calls below are the framework's
(`frappe.db.savepoint` / `rollback(save_point=)` / `release_savepoint`), in the same order frappe's own
`frappe.database.database.savepoint` contextmanager uses them — mark, roll back to the mark on failure,
release on success. That contextmanager is not reused directly because it swallows the exception where
it stands: no Error Log row, and no way to let a business refusal through. Both are non-negotiable here.

THE PRICE, AND WHY IT IS BOUNDED. Swallowing means the side-effect silently did not happen. That is only
acceptable where something can put it right later — the timeline has `rebuild()`, the metrics have
`backfill()`, the search index has `build_index()`, the file bond is re-attempted on the next save. A
propagate hook whose work is NOT rebuildable is not wrapped, however tempting: see
`docs/pending/2026-07-28-hooks-unrebuildable-propagate-side-effects.md` for the two that are excluded
and what each needs before it can be made safe.

SILENCE IS THE RISK. Every swallow writes an Error Log row naming the hook, the doctype and the docname.
And a `frappe.ValidationError` or `frappe.PermissionError` is NEVER swallowed: a business refusal
surfacing from a propagate hook means the hook is mis-classified, and hiding it would turn a
mis-classification into a silent data problem.

WHY THE RE-ENTRANCY FLAG. `frappe.log_error` INSERTS an Error Log document, and a document insert fires
`doc_events["*"]` — the same wildcard several of these hooks ride. So a wildcard hook that fails would
log, and the log's own insert would fail the same way, and log again, without end. `in_propagate_log` is
that door, and it is frappe's own convention for exactly this (`in_workflow`, `in_automation`,
`in_import`): while the Error Log row is being written, a propagate hook does not run. The side effects
of writing an error row are not news; the error row is.
"""
import functools
import itertools

import frappe

# A savepoint name is unique per invocation: a wrapped hook can re-enter itself (a save inside a hook
# fires the hook again), and MySQL silently REPLACES a savepoint of the same name — the outer frame
# would then roll back to, or release, a savepoint the inner frame already consumed.
_SEQ = itertools.count()

# Frappe-native re-entrancy flag, read by the wrapper and set only while the Error Log row is written.
IN_PROPAGATE_LOG = "in_propagate_log"


def fail_safe(fn):
	"""Decorate a PROPAGATE doc_event handler so its failure cannot abort the save.

	Marks a savepoint, runs the hook, and on failure rolls back to that savepoint ONLY — the outer save
	keeps every write it made — then writes an Error Log row naming the hook, the doctype and the docname.
	`frappe.ValidationError` and `frappe.PermissionError` are re-raised untouched.
	"""

	@functools.wraps(fn)
	def wrapper(doc, method=None, *args, **kwargs):
		if frappe.flags.get(IN_PROPAGATE_LOG):
			return None  # writing the Error Log for a failed hook — its own side effects are not news
		save_point = f"tc_prop_{fn.__name__}_{next(_SEQ)}"
		try:
			frappe.db.savepoint(save_point)
			result = fn(doc, method, *args, **kwargs)
		except Exception as exception:
			if _must_surface(exception):
				raise
			_undo_and_log(save_point, fn, doc)
			return None
		_release(save_point)
		return result

	return wrapper


def _must_surface(exception) -> bool:
	"""A business refusal is never swallowed — it means this hook is a gate wearing a propagate's clothes.

	`frappe.ValidationError` is matched on the EXACT type, because `frappe.throw()` raises exactly that
	and almost everything the framework raises subclasses it: `DoesNotExistError` when a linked row was
	deleted underneath us, `TimestampMismatchError` on a concurrent edit, `UniqueValidationError` on an
	index the hook re-ran into. Those are the accidents this decorator exists to absorb, and re-raising
	them would leave the disaster shape fully intact for its most common causes.
	"""
	return isinstance(exception, frappe.PermissionError) or type(exception) is frappe.ValidationError


def _undo_and_log(save_point, fn, doc):
	"""Undo the hook's writes, keep the save's, and say what was lost. Never raises."""
	try:
		frappe.db.rollback(save_point=save_point)
		frappe.flags[IN_PROPAGATE_LOG] = True
		frappe.log_error(
			title=f"propagate hook failed: {fn.__module__}.{fn.__name__}",
			message=f"doctype={doc.doctype} docname={doc.name}\n\n{frappe.get_traceback(with_context=True)}",
			reference_doctype=doc.doctype,
			reference_name=doc.name,
		)
	except Exception:  # nosec B110 — an escape here re-opens the exact hole this decorator closes
		# log_error is itself a DB insert and can fail the same way the hook did; the save still wins.
		frappe.logger("tatva_connect").error(f"propagate hook failed and could not be logged: {fn.__name__}")
	finally:
		frappe.flags[IN_PROPAGATE_LOG] = False


def _release(save_point):
	"""Drop the savepoint on the happy path — a bulk import holds ONE transaction over thousands of saves,
	and an unreleased savepoint per hook per row is undo state the server keeps until the commit."""
	try:
		frappe.db.release_savepoint(save_point)
	except Exception:  # nosec B110 — releasing is housekeeping; failing to release must not fail the save
		pass
