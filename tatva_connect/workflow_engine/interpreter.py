"""The workflow interpreter - run to suspension, commit at the boundary.

`advance(instance)` walks the frozen graph from `instance.current_node` until it hits a suspension
(a Wait park, or a Terminal), then commits ONCE (per-SEGMENT, not per-node - F3). A crash mid-segment
auto-rolls-back to the last durable Parked/Done state; nothing is ever left `Running` with no owner.

Reuse, not reinvention: a Step runs an Action Group's effect actions through the EXISTING
`automation.actions._ACTION_LANES` handlers inside a savepoint; Branch/Assign evaluate through the ONE
expression resolver `automation.expr`; a For-Duration Wait's wake time comes from the ONE arithmetic
`automation.actions.wait_resume_at` (`frappe.utils.add_to_date`). No second executor, no second evaluator.

INVARIANT (F6): every `advance()` is entered ONLY after the caller has claimed the Instance row
`for_update`. This module does not re-claim; it trusts the claim and re-reads status via the passed doc.

A Wait[event mode] tries the durable signal inbox FIRST (`_consume_signal`): an early, duplicate, or
crash-interleaved delivery is correct by construction because it simply waits in the inbox. A consumed
signal's declared payload paths merge into state via `_map_payload` (a dotted-path resolver, NOT eval)
and the Wait leaves by `on_event`; otherwise, if the clock is due, it leaves by `on_timeout`
(Event-or-Timeout) or `next_node` (pure timer); else it parks. The scheduler sweep + `deliver_signal` +
`resume_for_signal` (the wake callers) live in `wakeups.py` / `signals.py` and hold the `for_update` claim.
"""
import time

import frappe

from tatva_connect.automation import actions, expr

INSTANCE_DT = "CRM Workflow Instance"
STEP_LOG_DT = "CRM Workflow Step Log"
SIGNAL_DT = "CRM Workflow Signal"

MAX_HOPS = 100
MAX_RETRIES = 5
_EVENT_MODES = frozenset({"Until Event", "Event-or-Timeout"})
_TIME_MODES = frozenset({"For Duration", "Until Time", "Event-or-Timeout"})


class _Permanent(Exception):
	"""A permanent (non-retryable) failure - bad config, a bad expression, a broken graph. Terminal:
	the Instance goes Failed, so a re-drive cannot storm."""


def wait_deadline(wait_mode, wait_expression, state, base=None):
	"""The wake time of a time-mode Wait, off the ONE arithmetic. For Duration / an Event-or-Timeout
	timeout, `wait_expression` is a delay dict (e.g. {"minutes": 5}) shifted off `base`; Until Time is an
	expression resolving to a datetime. Shared with the Definition validator so an author-time check and a
	runtime park can never derive a different instant."""
	base = base or frappe.utils.now_datetime()
	if wait_mode == "Until Time":
		return frappe.utils.get_datetime(expr.resolve_expression(wait_expression, state))
	return actions.wait_resume_at(wait_expression, state, base)


def advance(instance):
	"""Walk the frozen graph to the next suspension and commit once. The caller already holds the
	`for_update` claim (F6). Returns the (mutated) instance doc."""
	version = versions_load(instance.workflow_version)
	nodes = {n.node_id: n for n in version.nodes}  # O(1) lookup, built once (F6)
	state = frappe.parse_json(instance.state_json or "{}")
	was_parked = instance.status == "Parked"
	entry_node = instance.current_node
	seen, hops = set(), 0
	deferred = []
	try:
		while True:
			node = nodes.get(instance.current_node)
			if node is None:
				raise _Permanent(f"node {instance.current_node!r} is not in the frozen graph")

			if node.node_type == "Terminal":
				_persist(instance, {"status": "Done", "current_node": node.node_id, "state_json": frappe.as_json(state), "active_key": None, "resume_at": None, "awaiting_signal": None})
				_step_log(instance, node, "done")
				frappe.db.commit()
				_run_deferred(deferred)
				return instance

			if node.node_type == "Wait":
				# The inbox FIRST (early/duplicate/stale signal - F1/F2): a Pending row matching this Wait's
				# (subject, signal, correlation) is consumed and its declared payload paths merge into state.
				if node.wait_mode in _EVENT_MODES:
					sig = _consume_signal(instance, node.signal_name, state.get("_corr"))
					if sig is not None:
						state.update(_map_payload(node.accepts_json, sig))
						was_parked = False
						_step_log(instance, node, "resumed", f"signal {node.signal_name}")
						instance.current_node = node.on_event
						continue
				# No signal buffered. If we were parked HERE and the clock is due, leave by the time edge.
				if was_parked and node.node_id == entry_node and node.wait_mode in _TIME_MODES and _clock_due(instance):
					was_parked = False
					_step_log(instance, node, "resumed", "timeout" if node.wait_mode == "Event-or-Timeout" else "timer")
					instance.current_node = node.on_timeout if node.wait_mode == "Event-or-Timeout" else node.next_node
					continue
				# Nothing to leave by - PARK (idempotent: a re-drive that arrives too early re-parks unchanged).
				_park(instance, node, state)
				frappe.db.commit()
				_run_deferred(deferred)
				return instance

			if instance.current_node in seen:
				raise _Permanent(f"cycle with no intervening Wait at node {instance.current_node}")
			hops += 1
			if hops > MAX_HOPS:
				raise _Permanent(f"hop budget exceeded ({MAX_HOPS})")
			seen.add(instance.current_node)

			started = time.monotonic()
			if node.node_type == "Step":
				subject_doc = frappe.get_doc(instance.subject_doctype, instance.subject_name)
				step_deferred, markers = _run_step(node, instance.subject_name, subject_doc, state, _axes(instance.subject_doctype, instance.subject_name))
				deferred += step_deferred
				nxt = node.next_node
				detail = f"action group {node.action_group}"
				if markers:  # a dormant send ("suppressed: sends dormant") records its marker in the audit, never a live message
					detail += " :: " + " | ".join(markers)
			elif node.node_type in ("Branch", "Assign"):
				nxt, detail = _next_control(node, state)  # the ONE control-flow step, shared with run_inline
			else:
				raise _Permanent(f"unknown node type {node.node_type!r}")

			_step_log(instance, node, "ok", detail, int((time.monotonic() - started) * 1000))
			instance.current_node = nxt
	except (frappe.QueryDeadlockError, frappe.QueryTimeoutError):  # real lock-wait / deadlock — TRANSIENT (F4)
		frappe.db.rollback()  # back to the last durable suspend
		if (instance.retry_count or 0) < MAX_RETRIES:
			_bump_retry(instance)  # leave it at its last durable state; the reconciler re-drives
		else:
			_fail(instance, "exhausted transient retries")
		return instance
	except Exception as e:  # bad config / bad expr — PERMANENT (F4)
		frappe.db.rollback()
		_fail(instance, str(e))
		return instance


def _next_control(node, state):
	"""Branch/Assign — the ONE control-flow step, shared by `advance` and `run_inline` (one interpreter, not
	two copies). A Branch routes on its condition; an Assign merges its evaluated dict into `state`. Returns
	`(next_node_id, detail)` — `advance` logs the detail, `run_inline` ignores it."""
	if node.node_type == "Branch":
		truthy = bool(expr.resolve_expression(node.condition, state))
		return (node.on_true if truthy else node.on_false), ("on_true" if truthy else "on_false")
	result = expr.resolve_expression(node.assign_json, state)
	if not isinstance(result, dict):
		raise _Permanent(f"Assign node {node.node_id} did not evaluate to a dict")
	state.update(result)
	return node.next_node, "keys: " + ",".join(sorted(result.keys()))


def has_wait(version):
	"""True iff the frozen graph parks anywhere - the ONE classifier the front-door uses to choose the
	shape (D4): a graph with a Wait is CONTINUOUS (a durable Instance carries its state across the park);
	a graph with none is EPHEMERAL (it runs to Terminal inline, persisting nothing, exactly like a rule)."""
	return any(n.node_type == "Wait" for n in version.nodes)


def run_inline(version_name, lead_name, trigger_doc, seed_state):
	"""EPHEMERAL execution (D4): walk the frozen graph inline to Terminal with NO persisted Instance - the
	rule-shaped Flow. The SAME node executor as `advance` (`_run_step`, the Branch/Assign logic, `expr`) -
	one interpreter, two shapes - minus the durable machinery a rule never needs (no Instance row, no
	active_key, no park, no signal inbox). A Wait node is a config error here: a graph that parks must run
	as a continuous Instance, and the front-door only routes a wait-free graph to this path.

	`seed_state` is the trigger context (the record's fields), so a Branch reads the trigger's values and an
	effect verb sees them exactly as the durable path sees signal-merged state. The whole walk runs inside
	ONE savepoint: a failure rolls back only the flow's own writes (the triggering save survives), and the
	caller logs it - an ephemeral effect can DO but never DENY. Deferred thunks fire after the savepoint
	releases. Returns the final state (for tests / callers); raises `_Permanent` on a broken graph."""
	version = versions_load(version_name)
	nodes = {n.node_id: n for n in version.nodes}
	state = dict(seed_state or {})
	axes = rules_lead_axes(lead_name)
	cursor = version.nodes[0].node_id
	seen, hops, deferred = set(), 0, []
	save_point = f"tc_wf_inline_{frappe.generate_hash(length=8)}"
	frappe.db.savepoint(save_point)
	try:
		while True:
			node = nodes.get(cursor)
			if node is None:
				raise _Permanent(f"node {cursor!r} is not in the frozen graph")
			if node.node_type == "Terminal":
				break
			if node.node_type == "Wait":
				raise _Permanent(f"ephemeral Flow reached Wait node {node.node_id} - a waiting Flow must run as a durable Instance")
			if cursor in seen:
				raise _Permanent(f"cycle with no intervening Wait at node {cursor}")
			hops += 1
			if hops > MAX_HOPS:
				raise _Permanent(f"hop budget exceeded ({MAX_HOPS})")
			seen.add(cursor)
			if node.node_type == "Step":
				step_deferred, _markers = _run_step(node, lead_name, trigger_doc, state, axes)
				deferred += step_deferred
				cursor = node.next_node
			elif node.node_type in ("Branch", "Assign"):
				cursor, _ = _next_control(node, state)  # the ONE control-flow step, shared with advance
			else:
				raise _Permanent(f"unknown node type {node.node_type!r}")
		frappe.db.release_savepoint(save_point)
	except Exception:
		frappe.db.rollback(save_point=save_point)  # undo only the flow's writes; the triggering save is untouched
		raise
	_run_deferred(deferred)
	return state


def rules_lead_axes(lead_name):
	"""Local indirection to the ONE grain accessor - the ephemeral path always has a resolved lead, so its
	axes are real (never the durable path's None-for-non-Lead)."""
	from tatva_connect.automation import rules

	return rules.lead_axes(lead_name)


def _clock_due(instance):
	"""True iff this Instance's clock deadline has arrived. A caller (the timer/reconciler sweep) only
	picks due rows, but a signal-wake path may re-enter a still-early Event-or-Timeout Wait, so the edge
	is gated on the deadline itself rather than on who woke it."""
	return bool(instance.resume_at) and frappe.utils.get_datetime(instance.resume_at) <= frappe.utils.now_datetime()


def _consume_signal(instance, signal_name, correlation):
	"""Claim the FIRST Pending inbox row matching (subject, signal, correlation) under a write lock, mark
	it Consumed (+ consumed_by), and return its parsed payload; `None` if none is buffered (→ park). A null
	awaiting correlation matches rows with a null/empty correlation (`None` in an `in` list becomes
	`IS NULL`). Claiming under `for_update` + a single mark is what makes a duplicate delivery advance
	exactly once (F2): two Pending rows with the same correlation, one consumed, the other purged by the
	sweep's stale-signal GC (`wakeups._purge_stale_signals`) - never a second advance."""
	filters = {"subject_doctype": instance.subject_doctype, "subject_name": instance.subject_name, "signal_name": signal_name, "status": "Pending"}
	filters["correlation"] = correlation if correlation else ["in", ["", None]]
	row = frappe.db.get_value(SIGNAL_DT, filters, ["name", "payload_json"], as_dict=True, order_by="creation asc", for_update=True)
	if not row:
		return None
	frappe.db.set_value(SIGNAL_DT, row.name, {"status": "Consumed", "consumed_by": instance.name}, update_modified=True)  # authz-ok: tier-a — workflow engine, scheduler/queue context
	return frappe.parse_json(row.payload_json or "{}")


def _map_payload(accepts_json, payload):
	"""Merge ONLY the declared payload paths of a consumed signal into state (the down-walk contract,
	F1/F2 safe). `accepts_json` is a JSON object of {dotted-path: state-key}; each path is plucked from the
	incoming JSON (`data.diagnosis`, `results.0.label` - dict keys and numeric list indices, a tiny resolver,
	NOT eval, NOT full JSONPath) and lands under its state key. An absent path lands `None`, so an arbitrary
	AI payload is bounded to a known, unambiguous shape - it can never write an undeclared key."""
	accepts = frappe.parse_json(accepts_json) if accepts_json else {}
	if not isinstance(accepts, dict):
		return {}
	return {state_key: _pluck(payload, path) for path, state_key in accepts.items()}


def _pluck(payload, dotted_path):
	"""Walk `dotted_path` into `payload`; `None` for any missing key or out-of-range/index-into-non-list
	step. A numeric segment indexes a list; any other segment keys a dict."""
	cur = payload
	for seg in dotted_path.split("."):
		if isinstance(cur, dict):
			cur = cur.get(seg)
		elif isinstance(cur, list) and seg.lstrip("-").isdigit():
			idx = int(seg)
			cur = cur[idx] if -len(cur) <= idx < len(cur) else None
		else:
			return None
		if cur is None:
			return None
	return cur


def _park(instance, node, state):
	"""Suspend at a Wait: persist Parked + the flavour columns (resume_at for a clock, awaiting_signal +
	awaiting_correlation for an event, both for Event-or-Timeout) - the shape the timer/reconciler sweep
	and `resume_for_signal` find the row by. The inbox itself is consumed on the NEXT advance, not here."""
	values = {"status": "Parked", "current_node": node.node_id, "state_json": frappe.as_json(state)}
	values["resume_at"] = wait_deadline(node.wait_mode, node.wait_expression, state) if node.wait_mode in _TIME_MODES else None
	if node.wait_mode in _EVENT_MODES:
		values["awaiting_signal"] = node.signal_name
		values["awaiting_correlation"] = state.get("_corr")
	else:
		values["awaiting_signal"] = None
	_persist(instance, values)
	_step_log(instance, node, "parked", "resume_at={} awaiting={}".format(values.get("resume_at"), values.get("awaiting_signal")))


def _run_step(node, lead_name, trigger_doc, state, axes):
	"""Run the Step's FROZEN Action Group actions (snapshotted into the version at freeze time - D1) through
	the existing `_ACTION_LANES` handlers, inside a savepoint (a bad action rolls the whole Step back).
	Returns `(deferred, markers)`: deferred thunks (webhook / WhatsApp live send) are fired only after the
	boundary commit, so a rolled-back segment sends nothing; markers are the string results a dormant send
	returns ("suppressed: sends dormant") - the audit records them so a suppressed send is provable without
	a live message. Guard-lane verbs are skipped: a Step is an effect, never a gate. Reading the frozen
	snapshot (never live rows) is what makes a parked Instance immutable to a later molecule edit.

	`lead_name` is the parent lead the effect verbs act ON (D7 - a Task/File flow resolves to its lead);
	`trigger_doc` is the record that fired the flow (the same subject doc for a Lead flow). Both the
	durable `advance` and the ephemeral `run_inline` pass these explicitly, so the ONE step executor is
	shared by both shapes with no second copy."""
	items = node.get("_frozen_items") or []
	deferred, markers = [], []
	save_point = f"tc_wf_step_{frappe.generate_hash(length=8)}"
	frappe.db.savepoint(save_point)
	try:
		for item in items:
			item = frappe._dict(item)
			lane, handler = actions._ACTION_LANES.get(item.action_type, (None, None))
			if lane != "effect" or item.action_type == "Wait":
				continue  # a guard-lane verb, or a Wait (a NODE in the Flow model, never an action), is not run here
			result = handler(item, lead_name, state, axes, trigger_doc)
			if callable(result):
				deferred.append(result)  # a thunk (Call Webhook / live WhatsApp) — fires only after the boundary commit
			elif isinstance(result, str) and result:
				markers.append(result)  # a dormant-send marker — no message left, recorded for the audit
		frappe.db.release_savepoint(save_point)
	except Exception:
		try:
			frappe.db.rollback(save_point=save_point)
		except Exception:  # nosec B110 — a full-transaction deadlock already discarded this savepoint
			pass
		raise  # re-raise the ORIGINAL error so advance() classifies it (transient deadlock vs permanent)
	return deferred, markers


def _axes(subject_doctype, subject_name):
	"""(vertical, group, program) of the subject, for the effect handlers' allowlist checks. A CRM Lead
	resolves through the ONE accessor the automation/activity engines use; a non-Lead subject has no grain
	axes on the durable path (the ephemeral path resolves the parent lead first, so it passes real axes)."""
	if subject_doctype == "CRM Lead":
		from tatva_connect.automation import rules

		return rules.lead_axes(subject_name)
	return (None, None, None)


def _persist(instance, values):
	"""Write the runtime columns and mirror them onto the in-memory doc, so the caller sees fresh state.
	`update_modified=True` keeps the (status, modified) retention index honest."""
	frappe.db.set_value(INSTANCE_DT, instance.name, values, update_modified=True)  # authz-ok: tier-a — workflow engine, scheduler/queue context
	for k, v in values.items():
		instance.set(k, v)


def _step_log(instance, node, outcome, detail="", duration_ms=0):
	"""One audit row per node execution. Written in the interpreter's transaction and committed at the
	boundary with everything else (a rolled-back segment writes no log)."""
	frappe.get_doc({
		"doctype": STEP_LOG_DT,
		"workflow_instance": instance.name,
		"subject_name": instance.subject_name,
		"node_id": node.node_id,
		"node_type": node.node_type,
		"outcome": outcome,
		"detail": detail,
		"duration_ms": duration_ms,
	}).insert(ignore_permissions=True)  # authz-ok: tier-a — workflow engine, scheduler/queue context


def _bump_retry(instance):
	"""A transient failure: increment the retry counter and commit, leaving the Instance at its last
	durable state for the reconciler to re-drive. Its own commit — the segment already rolled back, so this
	counter write is the only pending change. On the ENTRY path the Instance row may not be committed yet
	(the rollback dropped the uncommitted insert); there is nothing durable to retry, so log and return."""
	if not frappe.db.exists(INSTANCE_DT, instance.name):
		frappe.log_error(title="workflow: entry-segment transient failure (Flow never started)", message=f"instance={instance.name}")
		return
	_persist(instance, {"retry_count": (instance.retry_count or 0) + 1})
	frappe.db.commit()


def _fail(instance, reason):
	"""A permanent failure: mark Failed (terminal, so no retry storm) and drop the active_key so a fresh
	Instance can start. Runs after a rollback, so it commits its own single write plus an audit row. On the
	ENTRY path the Instance row may already be gone (the rollback dropped the uncommitted insert) — log the
	reason and return rather than write a dangling audit row to a vanished Instance."""
	if not frappe.db.exists(INSTANCE_DT, instance.name):
		frappe.log_error(title="workflow: entry-segment failed before commit", message=f"instance={instance.name} :: {reason[:1500]}")
		return
	_persist(instance, {"status": "Failed", "active_key": None})
	frappe.get_doc({
		"doctype": STEP_LOG_DT,
		"workflow_instance": instance.name,
		"subject_name": instance.subject_name,
		"node_id": instance.current_node,
		"node_type": "",
		"outcome": "failed",
		"detail": reason[:2000],
		"duration_ms": 0,
	}).insert(ignore_permissions=True)  # authz-ok: tier-a — workflow engine, scheduler/queue context
	frappe.db.commit()


def _run_deferred(thunks):
	"""Fire the segment's deferred side-effects AFTER the boundary commit, so a rolled-back segment sends
	nothing and a re-driven segment re-runs idempotent steps but never double-sends."""
	for thunk in thunks:
		thunk()


def versions_load(version_name):
	"""Local indirection so the interpreter reads the frozen graph through the ONE loader."""
	from tatva_connect.workflow_engine import versions

	return versions.load(version_name)
