"""The workflow interpreter - run to suspension, commit at the boundary.

`advance(journey)` walks the frozen graph from `journey.current_node` until it hits a suspension
(a Wait park, or a Terminal), then commits ONCE (per-SEGMENT, not per-node - F3). A crash mid-segment
auto-rolls-back to the last durable Parked/Done state; nothing is ever left `Running` with no owner.

Reuse, not reinvention: a verb node runs THE handler its type names, through the EXISTING
`automation.actions.VERBS` declaration, inside a savepoint; a Route evaluates through the ONE
predicate evaluator `automation.rules.predicate_match` (the same one the Trigger uses) and an Assign
through `automation.expr`; a For-Duration Wait's wake time comes from the ONE arithmetic
`automation.actions.wait_resume_at` (`frappe.utils.add_to_date`). No second executor, no second evaluator.

INVARIANT (F6): every `advance()` is entered ONLY after the caller has claimed the Journey row
`for_update`. This module does not re-claim; it trusts the claim and re-reads status via the passed doc.

A Wait[event mode] tries the durable signal inbox FIRST (`_consume_signal`): an early, duplicate, or
crash-interleaved delivery is correct by construction because it simply waits in the inbox. A consumed
signal's declared payload paths merge into state via `_map_payload` (a dotted-path resolver, NOT eval)
and the Wait leaves by its `event` output; otherwise, if the clock is due, it leaves by `timeout`
(Event-or-Timeout) or `next` (pure timer); else it parks. The scheduler sweep + `deliver_signal` +
`resume_for_signal` (the wake callers) live in `wakeups.py` / `signals.py` and hold the `for_update` claim.
"""
import hashlib
import time

import frappe

from tatva_connect.automation import actions, expr, rules
from tatva_connect.workflow_engine import refs, registry

JOURNEY_DT = "CRM Workflow Journey"
STEP_LOG_DT = "CRM Workflow Step Log"
SIGNAL_DT = "CRM Workflow Signal"

# W4.4 — born PENDING, terminal at CONSUMED (a park took it) or EXPIRED (the reaper aged it out).
PENDING, CONSUMED, EXPIRED = "Pending", "Consumed", "Expired"
TERMINAL_SIGNAL_STATES = (CONSUMED, EXPIRED)

# A journey is LIVE until it reaches a terminal status; STOPPED is the deliberate one (the subject left).
# DONE is the one that means the patient RECEIVED the journey, which is why W8.4's run-once reads it and
# neither of the other two terminals: Failed is the engine breaking, Stopped is an operator's Suspend.
DONE = "Done"
STOPPED = "Stopped"
LIVE_STATES = ("Running", "Parked")

MAX_HOPS = 100
MAX_RETRIES = 5
_EVENT_MODES = frozenset({"Until Event", "Event-or-Timeout"})
_TIME_MODES = frozenset({"For Duration", "Until Time", "Event-or-Timeout"})


class _Permanent(Exception):
	"""A permanent (non-retryable) failure - bad config, a bad expression, a broken graph. Terminal:
	the Journey goes Failed, so a re-drive cannot storm.

	`code` is optional and matches the Bouncer's code for the SAME fault where both layers can hit it — so
	an edge to a node that is not in the graph is named identically whether publish catches it or the journey
	does. It stays None for faults only the runtime can reach; the drift test proves the shared ones agree.
	"""

	def __init__(self, *args, code=None):
		super().__init__(*args)
		self.code = code


def wait_deadline(wait_mode, wait_expression, state, base=None):
	"""The wake time of a time-mode Wait, off the ONE arithmetic. For Duration / an Event-or-Timeout
	timeout, `wait_expression` is a delay dict (e.g. {"minutes": 5}) shifted off `base`; Until Time is an
	expression resolving to a datetime. Shared with the Definition validator so an author-time check and a
	runtime park can never derive a different instant."""
	base = base or frappe.utils.now_datetime()
	if wait_mode == "Until Time":
		return frappe.utils.get_datetime(expr.resolve_expression(wait_expression, state))
	return actions.wait_resume_at(wait_expression, state, base)


# Keys the ENGINE owns inside journey state are declared ONCE, by `registry.RESERVED_VARIABLES` — the same
# place that refuses an author's variable taking one of those names. A second tuple lived here, unread.


def _storable(state):
	"""What is persisted between segments: the journey's OWN values, nested by the node that wrote them.

	`state.buckets` holds exactly what writers put there. The subject's fields are never in it — they are
	read from the document each segment, because the document is where they live and Frappe already
	answers that question.
	"""
	return frappe.as_json(state.buckets)


def _subject_loader(journey):
	"""A zero-argument loader for the subject's fields AS THEY ARE NOW, or `None` when there is no subject.

	A LOADER and not a snapshot, because it is called on the first reference of the segment and not before.
	State used to be captured once, when the workflow was triggered, and never updated. Every later segment
	therefore judged the lead as it had been at the start: "wait 30 days, then if the lead is still New"
	tested a 30-day-old value, and a WhatsApp template after a Wait rendered from stale fields. It is also
	why dates decayed — a datetime survives one JSON round trip as a string, and every comparison after the
	first park was string-vs-datetime.
	"""
	if not journey.subject_doctype or not journey.subject_name:
		return None

	def load():
		if not frappe.db.exists(journey.subject_doctype, journey.subject_name):
			return {}  # deleted mid-flight; the journey fails at its next real read, not while reading state
		from tatva_connect.automation import context as ctx_build

		doc = frappe.get_doc(journey.subject_doctype, journey.subject_name)
		# The SAME builder the Trigger uses, unpacked back to this record's own bucket. A second walk here
		# is how the two evaluators came to speak different languages in the first place.
		return ctx_build.context_for(doc, {}).buckets.get(refs.slug(journey.subject_doctype), {})

	return load


def _refreshed_state(journey):
	"""The evaluation context for this segment: the journey's own values, plus the subject read live.

	`refs.Values`, not a ChainMap. A ChainMap merged two FLAT dicts, so a node's `status` and the lead's
	`status` were one name with the node's winning — the lead's own status was unreadable below a Call API,
	and the identical predicate that matched at the Trigger could not match at the Route. Namespacing
	makes that collision impossible by construction rather than by ordering, and the subject stays a
	loader, so it is still read as it is NOW and still never copied into what persists.
	"""
	return refs.Values(
		buckets=frappe.parse_json(journey.state_json or "{}"),
		records={refs.slug(journey.subject_doctype): _subject_loader(journey)}
		if journey.subject_doctype
		else {},
	)


def advance(journey):
	"""Walk the frozen graph to the next suspension and commit once. The caller already holds the
	`for_update` claim (F6). Returns the (mutated) journey doc."""
	version = versions_load(journey.workflow_version)
	nodes = {n.node_id: n for n in version.nodes}  # O(1) lookup, built once (F6)
	state = _refreshed_state(journey)
	was_parked = journey.status == "Parked"
	entry_node = journey.current_node
	seen, hops = set(), 0
	deferred = []
	try:
		while True:
			node = nodes.get(journey.current_node)
			if node is None:
				raise _Permanent(f"node {journey.current_node!r} is not in the frozen graph",
				                 code=registry.CODE_NODE_NOT_IN_GRAPH)

			if node.node_type == "Terminal":
				_persist(journey, {"status": DONE, "current_node": node.node_id, "state_json": _storable(state), "active_key": None, "resume_at": None, "awaiting_signal": None})
				_step_log(journey, node, "done")
				frappe.db.commit()
				_run_deferred(deferred)
				return journey

			if node.node_type == "Wait":
				wait = _config(node)
				# The inbox first: a Pending row for this Wait is consumed and merged before parking.
				if wait.get("mode") in _EVENT_MODES:
					sig = _consume_signal(journey, wait.get("event_name"), _wait_correlation(wait, state))
					if sig is not None:
						state.writing_as(node.node_id).update(_map_payload(wait.get("accepts"), sig))
						was_parked = False
						# A tap leaves by the branch the SEND declared for that button. The engine matches an arrived id against DECLARED edges - it never invents one for an id nobody offered.
						tapped = (sig or {}).get("button_id")
						leaving = tapped if tapped and _edge(node, tapped) else "event"
						_step_log(journey, node, "resumed", f"event {wait.get('event_name')}")
						journey.current_node = _edge(node, leaving)
						continue
				# No event buffered. If we were parked HERE and the clock is due, leave by the time edge.
				if was_parked and node.node_id == entry_node and wait.get("mode") in _TIME_MODES and _clock_due(journey):
					was_parked = False
					timed_out = wait.get("mode") == "Event-or-Timeout"
					_step_log(journey, node, "resumed", "timeout" if timed_out else "timer")
					journey.current_node = _edge(node, "timeout" if timed_out else "next")
					continue
				# Nothing to leave by - PARK (idempotent: a re-drive that arrives too early re-parks unchanged).
				_park(journey, node, state)
				frappe.db.commit()
				_run_deferred(deferred)
				return journey

			if journey.current_node in seen:
				raise _Permanent(f"cycle with no intervening Wait at node {journey.current_node}")
			hops += 1
			if hops > MAX_HOPS:
				raise _Permanent(f"hop budget exceeded ({MAX_HOPS})")
			seen.add(journey.current_node)

			started = time.monotonic()
			if actions.lane_of(node.node_type) == "effect":
				step_deferred, marker = _run_verb(node, journey.subject_name, _trigger_doc(journey), state, _axes(journey.subject_doctype, journey.subject_name), journey_name=journey.name)
				deferred += step_deferred
				# The verb's DECLARED output is both where the journey goes and what the audit records. It used to pick the edge and then be thrown away, so a refused send was filed as `ok` beside its own failure reason.
				output = _verb_output(node, state)
				nxt = _edge(node, output)
				# `next` is the generic carry-on edge and names no result, so a verb that declares no outputs still records `ok` — it ran, and that is all that happened at it.
				outcome = "ok" if output == "next" else output
				detail = marker or "ran"  # a dormant send records its marker, never a live message
			elif node.node_type in ("Route", "Sample", "Set Variables"):
				nxt, detail = _next_control(node, state, journey.subject_name)  # the ONE control-flow step, shared with run_inline
				outcome = "ok"  # a control node declares no output; that it ran IS what happened at it
			elif node.node_type == registry.TRIGGER:
				# The dispatcher already matched and qualified; at execution the Trigger is a pass-through.
				nxt, detail = _edge(node, "next"), "entered"
				outcome = "ok"
			else:
				raise _Permanent(f"unknown node type {node.node_type!r}")

			# W12 — WHO this step reached, popped exactly as the output edge above is: a send verb writes it
			# into the engine namespace, and consuming it here stops the next node inheriting a recipient
			# that was never its own.
			_step_log(journey, node, outcome, detail, int((time.monotonic() - started) * 1000),
			          channel=state.pop(refs.CHANNEL, None), contact=state.pop(refs.CONTACT, None))
			journey.current_node = nxt
	except (frappe.QueryDeadlockError, frappe.QueryTimeoutError):  # real lock-wait / deadlock — TRANSIENT (F4)
		frappe.db.rollback()  # back to the last durable suspend
		if (journey.retry_count or 0) < MAX_RETRIES:
			_bump_retry(journey)  # leave it at its last durable state; the reconciler re-drives
		else:
			_fail(journey, "exhausted transient retries")
		return journey
	except Exception as e:  # bad config / bad expr — PERMANENT (F4)
		frappe.db.rollback()
		_fail(journey, str(e))
		return journey


def _trigger_doc(journey):
	"""The record whose save started this journey — a Task, a File, or the lead itself.

	The subject of a durable journey is ALWAYS the resolved parent lead, so this used to hand every verb the
	lead and call it the trigger doc. A Call API set to send the trigger doc therefore sent the lead, a
	Create Task could never see the file it was raised for, and an Update Field aimed at the triggering
	Task failed the journey outright. The author's choice changed nothing and nothing said so.

	Falls back to the subject: a journey started before this was recorded, or one the lead itself fired, has
	no separate trigger, and the lead is then the honest answer rather than a missing one.
	"""
	if journey.get("trigger_doctype") and journey.get("trigger_name"):
		if frappe.db.exists(journey.trigger_doctype, journey.trigger_name):
			return frappe.get_doc(journey.trigger_doctype, journey.trigger_name)
	return frappe.get_doc(journey.subject_doctype, journey.subject_name)


def _config(node):
	"""This node's own configuration. One reader — a node type's fields live in its `config_json`, so the
	engine never grows a column-per-node-type and a new type needs no interpreter change."""
	return registry.config_of(node)


def _edge(node, output):
	"""The node this named output leads to, or None. The ONE routing lookup: every node type asks by the
	output name its registry entry declares, so adding a type adds no branch here."""
	for edge in node.get("edges") or []:
		if edge.get("output") == output:
			return edge.get("to")
	return None


def _next_control(node, state, subject=None):
	"""Route/Sample/Assign — the ONE control-flow step, shared by `advance` and `run_inline` (one
	interpreter, not two copies). Returns `(next_node_id, detail)` — `advance` logs the detail,
	`run_inline` ignores it.

	A Route routes on the SAME predicate structure the Trigger uses, through the same evaluator: one
	control for the author, one meaning at runtime. Rows are tried top to bottom and the FIRST whose
	condition matches takes its edge — order is logic. A lead matching none takes `otherwise`, which is
	reserved and always wired, so it can never fall out of the graph.

	A Sample asks the other question a graph can ask when it splits — which arm did this person land in —
	and answers it from `_arm_of`, never from chance at the moment of asking."""
	config = _config(node)
	if node.node_type == "Route":
		for row in config.get("routes") or []:
			if rules.predicate_match(row.get("condition"), state):
				return _edge(node, row["id"]), row["id"]
		return _edge(node, "otherwise"), "otherwise"
	if node.node_type == "Sample":
		arm = _arm_of(node, config, subject)
		return _edge(node, arm), arm
	result = expr.resolve_expression(config.get("assign"), state)
	if not isinstance(result, dict):
		raise _Permanent(f"Assign node {node.node_id} did not evaluate to a dict")
	# Through the node's own writer view, so `{"stage": "Qualified"}` lands at `<node_id>.stage`. An author
	# names a value; which node produced it is the engine's to know, and `upstream` offers it under exactly
	# this reference — so the picker and the journey agree without the author ever typing a node id.
	state.writing_as(node.node_id).update(result)
	return _edge(node, "next"), "keys: " + ",".join(sorted(result.keys()))


def _arm_of(node, config, subject):
	"""Which arm this subject lands in — a STABLE HASH of subject + node, never a draw.

	THIS IS THE WHOLE REASON SAMPLE IS A SEPARATE NODE FROM ROUTE. A control group that reshuffles is not
	a control group: the same lead judged again — a resume off the sweep, a re-run, a second cohort — must
	land where it landed the first time, and nothing is stored to make that true. The node id is in the
	digest so two Samples in one graph split independently rather than in lockstep.

	`sha256` and not Python's `hash()`: `hash()` of a str is salted per process (`PYTHONHASHSEED`), so
	every worker restart would re-randomise every assignment while looking perfectly deterministic inside
	one process. `random.Random(seed)` would also be stable, and is rejected because it says "draw" where
	this says "read" — there is no chance here at execution time, only arithmetic on a digest.

	The share ladder is walked in the author's own row order, and a subject past the last arm takes the
	reserved `remainder`, so the arms never have to add up to 100.
	"""
	digest = hashlib.sha256(f"{subject}::{node.node_id}".encode()).hexdigest()
	# Two decimals of a percent, which is the finest share an author can express.
	point = int(digest[:8], 16) % 10000 / 100.0
	ceiling = 0.0
	for row in config.get("arms") or []:
		try:
			ceiling += float(row.get("percent"))
		except (TypeError, ValueError):
			continue
		if point < ceiling and row.get("id"):
			return row["id"]
	return "remainder"


def has_wait(version):
	"""True iff the frozen graph parks anywhere - the ONE classifier the front-door uses to choose the
	shape (D4): a graph with a Wait is CONTINUOUS (a durable Journey carries its state across the park);
	a graph with none is EPHEMERAL (it runs to Terminal inline, persisting nothing, exactly like a rule)."""
	return any(n.node_type == "Wait" for n in version.nodes)


def run_inline(version_name, lead_name, trigger_doc, seed_state):
	"""EPHEMERAL execution (D4): walk the frozen graph inline to Terminal with NO persisted Journey - the
	rule-shaped Flow. The SAME node executor as `advance` (`_run_verb`, the Route/Assign logic, `expr`) -
	one interpreter, two shapes - minus the durable machinery a rule never needs (no Journey row, no
	active_key, no park, no signal inbox). A Wait node is a config error here: a graph that parks must run
	as a continuous Journey, and the front-door only routes a wait-free graph to this path.

	`seed_state` is the trigger context (the record's fields), so a Route reads the trigger's values and an
	effect verb sees them exactly as the durable path sees signal-merged state. The whole walk runs inside
	ONE savepoint: a failure rolls back only the flow's own writes (the triggering save survives), and the
	caller logs it - an ephemeral effect can DO but never DENY. Deferred thunks fire after the savepoint
	releases. Returns the final state (for tests / callers); raises `_Permanent` on a broken graph."""
	from tatva_connect.workflow_engine import versions

	version = versions_load(version_name)
	nodes = {n.node_id: n for n in version.nodes}
	# The trigger context AS BUILT — a `refs.Values`, carried through rather than flattened. An ephemeral
	# run has no persisted state, so the Trigger's own namespaced buckets are the whole vocabulary and the
	# same references resolve here as at dispatch.
	state = seed_state if isinstance(seed_state, refs.Values) else refs.Values(buckets=seed_state or {})
	axes = rules_lead_axes(lead_name)
	cursor = versions.entry_node_of(version)  # the ONE entry-resolution brain (shared with the durable start)
	seen, hops, deferred = set(), 0, []
	save_point = f"tc_wf_inline_{frappe.generate_hash(length=8)}"
	frappe.db.savepoint(save_point)
	try:
		while True:
			node = nodes.get(cursor)
			if node is None:
				raise _Permanent(f"node {cursor!r} is not in the frozen graph",
				                 code=registry.CODE_NODE_NOT_IN_GRAPH)
			if node.node_type == "Terminal":
				break
			if node.node_type == "Wait":
				raise _Permanent(f"ephemeral Flow reached Wait node {node.node_id} - a waiting Flow must run as a durable Journey")
			if cursor in seen:
				raise _Permanent(f"cycle with no intervening Wait at node {cursor}")
			hops += 1
			if hops > MAX_HOPS:
				raise _Permanent(f"hop budget exceeded ({MAX_HOPS})")
			seen.add(cursor)
			if actions.lane_of(node.node_type) == "effect":
				step_deferred, _marker = _run_verb(node, lead_name, trigger_doc, state, axes)
				deferred += step_deferred
				cursor = _edge(node, _verb_output(node, state))
			elif node.node_type in ("Route", "Sample", "Set Variables"):
				cursor, _ = _next_control(node, state, lead_name)  # the ONE control-flow step, shared with advance
			elif node.node_type == registry.TRIGGER:
				cursor = _edge(node, "next")  # a pass-through here too — see `advance`
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


def _clock_due(journey):
	"""True iff this Journey's clock deadline has arrived. A caller (the timer/reconciler sweep) only
	picks due rows, but a signal-wake path may re-enter a still-early Event-or-Timeout Wait, so the edge
	is gated on the deadline itself rather than on who woke it."""
	return bool(journey.resume_at) and frappe.utils.get_datetime(journey.resume_at) <= frappe.utils.now_datetime()


def _wait_correlation(wait, state):
	"""What THIS Wait correlates on. The ONE reader — both the park and the consume side ask it.

	A Wait that names an upstream node answers to the token that node minted, which is what makes the
	wake per-task. A Wait that names none falls back to the journey-level `_corr`, which is what a signal
	delivered from outside the graph carries. Two readers of this rule is exactly the bug it was written
	after: parking on the token while consuming on `_corr` left the row in the inbox and the journey parked
	for ever, with every part looking individually correct.
	"""
	source = wait.get("source_node")
	if source:
		return (state.get(refs.EMITTED) or {}).get(source)
	return state.get(refs.CORRELATION)


def _consume_signal(journey, signal_name, correlation):
	"""Claim the FIRST Pending inbox row matching (subject, signal, correlation) under a write lock, mark
	it Consumed (+ consumed_by), and return its parsed payload; `None` if none is buffered (→ park). A null
	awaiting correlation matches rows with a null/empty correlation (`None` in an `in` list becomes
	`IS NULL`). Claiming under `for_update` + a single mark is what makes a duplicate delivery advance
	exactly once (F2): two Pending rows with the same correlation, one consumed, the other purged by the
	sweep's stale-signal GC (`wakeups._purge_stale_signals`) - never a second advance."""
	filters = pending_signal_filters(journey.subject_doctype, journey.subject_name, signal_name, correlation)
	row = frappe.db.get_value(SIGNAL_DT, filters, ["name", "payload_json"], as_dict=True, order_by="creation asc", for_update=True)
	if not row:
		return None
	frappe.db.set_value(SIGNAL_DT, row.name, {"status": CONSUMED, "consumed_by": journey.name}, update_modified=True)  # authz-ok: tier-a — workflow engine, scheduler/queue context
	return frappe.parse_json(row.payload_json or "{}")


def pending_signal_filters(subject_doctype, subject_name, signal_name, correlation):
	"""The ONE description of "an inbox row that would wake this park".

	`_consume_signal` claims by it. The journey-history surface ASKS by it, to tell a park that a buffered
	signal will end from one that nothing has arrived for — and a second filter dict there would be a
	second opinion about what counts as a match, which is exactly the class of bug that left journeys
	parked for ever while every part looked individually correct.

	A null correlation matches rows with a null/empty correlation (`None` inside an `in` list becomes
	`IS NULL`).

	W4.4: `status == PENDING` is ALSO the age rule. An inbox row the reaper has aged out is EXPIRED, and
	expiry is what takes it out of this filter — so a stale event cannot wake a journey, and no waking
	surface has to remember to check a date. One state machine, one reader.
	"""
	return {
		"subject_doctype": subject_doctype,
		"subject_name": subject_name,
		"event_name": signal_name,
		"status": PENDING,
		"correlation": correlation if correlation else ["in", ["", None]],
	}


def _map_payload(accepts_json, payload):
	"""Merge ONLY the declared payload paths of a consumed signal into state (the down-walk contract,
	F1/F2 safe). `accepts_json` is a JSON object of {dotted-path: state-key}; each path is plucked from the
	incoming JSON (`data.diagnosis`, `results.0.label` - dict keys and numeric list indices, a tiny resolver,
	NOT eval, NOT full JSONPath) and lands under its state key. An absent path lands `None`, so an arbitrary
	AI payload is bounded to a known, unambiguous shape - it can never write an undeclared key."""
	return {state_key: _pluck(payload, path) for path, state_key in accepts_map(accepts_json).items()}


def accepts_map(accepts_json):
	"""A Wait's declared `{dotted path: state key}` map, parsed. The ONE reader of that field.

	`upstream` needs the same answer to tell the publish gate which keys a Wait contributes, and a second
	parser there would be a second opinion about what "malformed" means — the gate would then reject a
	graph the runtime happily runs, or bless one it does not. Anything that is not an object is an empty
	map: it writes nothing, so it contributes nothing.
	"""
	accepts = frappe.parse_json(accepts_json) if accepts_json else {}
	return accepts if isinstance(accepts, dict) else {}


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


def _park(journey, node, state):
	"""Suspend at a Wait: persist Parked + the flavour columns (resume_at for a clock, awaiting_signal +
	awaiting_correlation for an event, both for Event-or-Timeout) - the shape the timer/reconciler sweep
	and `resume_for_signal` find the row by. The inbox itself is consumed on the NEXT advance, not here."""
	wait = _config(node)
	mode = wait.get("mode")
	values = {"status": "Parked", "current_node": node.node_id, "state_json": _storable(state)}
	values["resume_at"] = wait_deadline(mode, wait.get("expression"), state) if mode in _TIME_MODES else None
	if mode in _EVENT_MODES:
		correlation = _wait_correlation(wait, state)
		# A Wait that NAMES a node it never got a token from is unwakeable, not patient: the null it parks
		# on is matched only against rows whose correlation is empty, and the node it is waiting for mints
		# a token. Fail loudly rather than sit Parked with no clock and no reachable event, which reads
		# exactly like waiting normally. A Wait naming NO node is the external-signal case and is fine.
		# `graph._wait_problems` refuses this at publish; this catches versions frozen before it existed.
		if wait.get("source_node") and not correlation and mode == registry.UNTIL_EVENT:
			raise _Permanent(
				f"{node.node_id} waits on {wait.get('source_node') or 'nothing'}, which has not run — "
				"there is no correlation to wake it and no timeout to end it"
			)
		values["awaiting_signal"] = wait.get("event_name")
		values["awaiting_correlation"] = correlation
	else:
		values["awaiting_signal"] = None
	_persist(journey, values)
	# The diary row is written; set the alarm so the clock is kept to the minute rather than to the */15
	# sweep. After-commit and losable by design — the sweep still finds this row if the alarm never fires.
	if values.get("resume_at"):
		from tatva_connect.workflow_engine import wakeups

		wakeups.schedule_wake(journey.name, values["resume_at"])
	_step_log(journey, node, "parked", "resume_at={} awaiting={}".format(values.get("resume_at"), values.get("awaiting_signal")))


def _verb_output(node, state):
	"""Which edge this verb leaves by. Almost always `next`; a verb that ROUTES on its own result — a
	Call API that succeeded or failed — names its output in state, and the choice is validated against
	what the type actually declares so a handler can never invent an edge the canvas never drew."""
	chosen = state.pop(refs.OUTPUT, None)
	if chosen and chosen in registry.outputs_for(node.node_type, _config(node)):
		return chosen
	return "next"


def _run_verb(node, lead_name, trigger_doc, state, axes, journey_name=None):
	"""Run THIS node's verb, with the node's own config as its parameters, inside a savepoint.

	A node is a verb now — its type names what it does and its config is exactly that verb's declared
	parameters. The old shape was a generic `Step` carrying a list of actions, so one node could half
	succeed; a node that IS one verb either happened or did not, and the savepoint means a failure takes
	nothing with it.

	Returns `(deferred, marker)`. A deferred thunk (a live WhatsApp send) fires only after
	the boundary commit, so a rolled-back segment sends nothing. A marker is what a dormant send returns
	("suppressed: sends dormant") — recorded in the audit so a suppressed send is provable without a
	message having left.

	`lead_name` is the parent lead the verb acts ON; `trigger_doc` is the record that fired the workflow.
	Both the durable `advance` and the ephemeral `run_inline` pass these, so one executor serves both.
	"""
	verb = node.node_type
	handler = actions.handler_of(verb)
	if handler is None:
		raise _Permanent(f"no handler for verb {verb!r}")

	# A verb that can be ANSWERED (a task someone completes) gets a token identifying this node in this
	# run. The handler stamps it on whatever it creates, and a Wait downstream correlates on the same
	# token — which is what makes the wake per-task instead of per-lead.
	if journey_name and actions.outcomes_of(verb):
		token = f"{journey_name}::{node.node_id}"
		state[refs.TOKEN] = token
		state.setdefault(refs.EMITTED, {})[node.node_id] = token
	else:
		state.pop(refs.TOKEN, None)

	save_point = f"tc_wf_step_{frappe.generate_hash(length=8)}"
	frappe.db.savepoint(save_point)
	try:
		# `action_type` is set because a handler identifies its verb from it — same value, one source.
		params = frappe._dict(_config(node))
		params.action_type = verb
		# The handler writes through THIS node's view, so a Call API capturing `order_id` lands it at
		# `<node_id>.order_id`. A verb is one verb reused by the rule lane and by both interpreter paths and
		# must not know its node id; the interpreter does, so the scoping belongs here and only here.
		result = handler(params, lead_name, state.writing_as(node.node_id), axes, trigger_doc)
		frappe.db.release_savepoint(save_point)
	except Exception:
		try:
			frappe.db.rollback(save_point=save_point)
		except Exception:  # nosec B110 — a full-transaction deadlock already discarded this savepoint
			pass
		raise  # re-raise the ORIGINAL error so advance() classifies it (transient deadlock vs permanent)

	if callable(result):
		return [result], None
	return [], result if isinstance(result, str) and result else None


def _axes(subject_doctype, subject_name):
	"""(vertical, group, program) of the subject, for the effect handlers' allowlist checks. A CRM Lead
	resolves through the ONE accessor the automation/activity engines use; a non-Lead subject has no grain
	axes on the durable path (the ephemeral path resolves the parent lead first, so it passes real axes)."""
	if subject_doctype == "CRM Lead":
		from tatva_connect.automation import rules

		return rules.lead_axes(subject_name)
	return (None, None, None)


def stop_for_subject(subject_doctype, subject_name, reason):
	"""End every LIVE journey this subject has, across all workflows. Returns how many were stopped.

	ONE behaviour with two triggers (a lead deleted, a lead's grain changed), so it is written once here
	and called twice from `triggers.py` — a second stop path is the defect this is shaped to avoid.

	It runs INSIDE the caller's transaction and commits nothing: both triggers are `doc_events` on a save
	the user is making, and a commit there would commit their whole pending write. The subject's live
	journeys are at most one per workflow, so there is nothing to chunk.
	"""
	return _stop_matching({"subject_doctype": subject_doctype, "subject_name": subject_name}, reason)


def stop_for_workflow(workflow_name, reason):
	"""End every LIVE journey of this workflow. Returns how many were stopped. The QUEUED entry point.

	W10 — the same behaviour as `stop_for_subject`, keyed on the workflow instead of the subject, so both
	go through the one `_stop_matching` below and there is no second terminal transition to disagree with.

	IT COMMITS, so it must never be called inside a request: `apply_transition` and `on_trash` enqueue it.
	A workflow's live journeys are one per LEAD, so this set is the cohort's size — thousands — and one
	transaction holding them all is a lock nobody else can get past.

	CHUNKED, AND WITHOUT A CURSOR, which is where this parts company with `drain.run_cohort`. That walk
	needs a keyset cursor because a lead it passes over still matches the criteria on the next pass; here
	the filter is SELF-CONSUMING — a journey this pass stops is terminal, so the next `get_all` cannot
	return it. Each pass therefore asks the same question and gets a strictly smaller answer, and the loop
	ends when a pass finds nothing. A cursor would add a way to skip a row and no guarantee at all.

	Nor is there a claim like `drain._claim`: the LIFECYCLE is the claim. `apply_transition` refuses
	SUSPENDED → SUSPENDED, so a second suspend cannot start a second drain, and `job_id`/`deduplicate`
	closes the rest.
	"""
	from tatva_connect.workflow_engine import thresholds

	stopped = 0
	while True:
		claimed = _stop_matching({"workflow": workflow_name}, reason, limit=thresholds.STOP_CHUNK)
		frappe.db.commit()
		stopped += claimed
		if not claimed:
			return stopped


def _stop_matching(filters, reason, limit=None):
	"""THE terminal stop, however it was reached. Returns how many journeys it really ended.

	It is the engine's OWN terminal transition, not a new one: the same `_persist` write that `Done` and
	`Failed` use, clearing the columns that make a row re-drivable. `active_key` goes NULL so the unique
	index frees the subject for a future start; `resume_at`/`awaiting_signal`/`awaiting_correlation` go
	NULL so neither the timer sweep nor a delivered signal can ever wake it again. Nothing is deleted —
	a stopped journey is still readable, and `stop_reason` is what it says when read.

	Each row is claimed `for_update` and re-checked inside the lock, exactly as `wakeups.drive_journey`
	does: a sweep may be driving this very journey, and the loser of that race must take nothing rather
	than stop a journey that has already moved on. NO raw SQL — the claim is `get_value(for_update=True)`.
	"""
	stopped = 0
	for name in frappe.get_all(
		JOURNEY_DT, filters={**filters, "status": ["in", LIVE_STATES]}, pluck="name", limit=limit,
	):
		if not frappe.db.get_value(JOURNEY_DT, {"name": name, "status": ["in", LIVE_STATES]}, "name", for_update=True):
			continue  # already terminal, or another driver holds it — re-checked INSIDE the lock
		_persist(frappe.get_doc(JOURNEY_DT, name), {
			"status": STOPPED,
			"stop_reason": reason,
			"active_key": None,
			"resume_at": None,
			"awaiting_signal": None,
			"awaiting_correlation": None,
		})
		stopped += 1
	return stopped


def _persist(journey, values):
	"""Write the runtime columns and mirror them onto the in-memory doc, so the caller sees fresh state.
	`update_modified=True` keeps the (status, modified) retention index honest."""
	frappe.db.set_value(JOURNEY_DT, journey.name, values, update_modified=True)  # authz-ok: tier-a — workflow engine, scheduler/queue context
	for k, v in values.items():
		journey.set(k, v)


def _step_log(journey, node, outcome, detail="", duration_ms=0, channel=None, contact=None):
	"""One audit row per node execution. Written in the interpreter's transaction and committed at the
	boundary with everything else (a rolled-back segment writes no log).

	W12 — `channel`/`contact` are blank for every node that reaches nobody, and for a send they are the
	CANONICAL address (`sends._canonical_contact`), never what the provider was handed. That is what makes
	one patient one row-identity across WhatsApp and voice, which is the whole basis of the contact cap.
	"""
	frappe.get_doc({
		"doctype": STEP_LOG_DT,
		"journey": journey.name,
		"subject_name": journey.subject_name,
		"node_id": node.node_id,
		"node_type": node.node_type,
		"outcome": outcome,
		"channel": channel or "",
		"contact": contact or "",
		"detail": detail,
		"duration_ms": duration_ms,
	}).insert(ignore_permissions=True)  # authz-ok: tier-a — workflow engine, scheduler/queue context
	_publish_step(journey, node, outcome, detail)


def _publish_step(journey, node, outcome, detail):
	"""Tell any open canvas which node this journey just executed.

	The SAME shape the WhatsApp history refresh proved: ONE event carrying its own state, published to
	the WORKFLOW'S doc room. Not the site room — with neither doctype nor user Frappe broadcasts to every
	Desk user on the site, which would put one lead's journey onto every open browser. `doc_subscribe` joins
	this room only for a user who may read the workflow, so the permission is Frappe's, not ours.

	Best-effort by construction: a canvas nobody has open must never be able to fail a journey, so a socket
	that is down is swallowed. The audit row is already written — this is a notification, not the record.
	"""
	try:
		frappe.publish_realtime(
			"workflow_step",
			{
				"workflow": journey.workflow,
				"journey": journey.name,
				"node_id": node.node_id,
				"outcome": outcome,
				"detail": detail,
			},
			doctype="CRM Workflow",
			docname=journey.workflow,
			after_commit=True,
		)
	except Exception:  # nosec B110 — a notification must never take a journey down
		pass


def _bump_retry(journey):
	"""A transient failure: increment the retry counter and commit, leaving the Journey at its last
	durable state for the reconciler to re-drive. Its own commit — the segment already rolled back, so this
	counter write is the only pending change. On the ENTRY path the Journey row may not be committed yet
	(the rollback dropped the uncommitted insert); there is nothing durable to retry, so log and return."""
	if not frappe.db.exists(JOURNEY_DT, journey.name):
		frappe.log_error(title="workflow: entry-segment transient failure (Flow never started)", message=f"journey={journey.name}")
		return
	_persist(journey, {"retry_count": (journey.retry_count or 0) + 1})
	frappe.db.commit()


def _fail(journey, reason):
	"""A permanent failure: mark Failed (terminal, so no retry storm) and drop the active_key so a fresh
	Journey can start. Runs after a rollback, so it commits its own single write plus an audit row. On the
	ENTRY path the Journey row may already be gone (the rollback dropped the uncommitted insert) — log the
	reason and return rather than write a dangling audit row to a vanished Journey."""
	if not frappe.db.exists(JOURNEY_DT, journey.name):
		frappe.log_error(title="workflow: entry-segment failed before commit", message=f"journey={journey.name} :: {reason[:1500]}")
		return
	_persist(journey, {"status": "Failed", "active_key": None})
	frappe.get_doc({
		"doctype": STEP_LOG_DT,
		"journey": journey.name,
		"subject_name": journey.subject_name,
		"node_id": journey.current_node,
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
