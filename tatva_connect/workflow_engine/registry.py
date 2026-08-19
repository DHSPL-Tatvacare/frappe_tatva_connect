# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The node-type registry — ONE declaration per node type, read by everything.

A node type is declared here and nowhere else. The palette lists what this file declares, the inspector
renders the config fields it declares, the validator enforces them, and the canvas draws exactly the
outputs it declares. Adding a node type is an entry in `NODE_TYPES`; it is never a schema migration, a
frontend switch statement, or a new branch in the interpreter.

WHY A REGISTRY AND NOT COLUMNS
------------------------------
The old model gave every node type its own columns — a Route's `condition`, a Wait's `wait_mode` /
`signal_name` / `accepts_json`, and five fixed edge columns — so every OTHER node type carried them
empty, the frontend hardcoded which fields to show for which type, and the canvas hardcoded which
handles to draw. Three copies of one fact, and adding a type meant editing all three.

Now: a node holds `config_json`, and this file says what belongs in it.

OUTPUTS ARE PART OF THE CONTRACT
--------------------------------
A node type declares the names of the edges that may leave it. `Route` derives one edge per row plus a
reserved `otherwise`; `Terminal` declares none. A Wait's outputs depend on its mode — waiting only on an event has no timeout
edge — so it declares them CONDITIONALLY, and both the validator and the canvas read that one rule
rather than each re-deriving it.
"""
import frappe
from frappe import _

from tatva_connect.workflow_engine import refs

# Wait modes, shared with the interpreter's own sets. Declared once here as the authoring vocabulary.
UNTIL_EVENT = "Until Event"
FOR_DURATION = "For Duration"
UNTIL_TIME = "Until Time"
EVENT_OR_TIMEOUT = "Event-or-Timeout"

TRIGGER = "Trigger"

# How a journey is born: a save starts ONE journey; a schedule takes a COHORT, each lead its own ordinary journey.
MODE_RECORD = "Record Event"
MODE_SCHEDULE = "Schedule"

# Frappe's own frequency names and its own crons (`scheduled_job_type.py:113`, a local we cannot import); hourly and finer are out because a cohort is a business rhythm, not a poll.
SCHEDULES = {
	"Daily": "0 0 * * *",
	"Weekly": "0 0 * * 0",
	"Monthly": "0 0 1 * *",
}


def _subject_options():
	"""The doctypes a workflow may watch — read from `automation.subjects.SUBJECTS`, the ONE resolver.

	Offering anything else would let an author pick a subject the engine cannot resolve to a lead, and
	the workflow would then be silently dead: it would match on save, fail to resolve a subject, and
	return without a trace. A wrong pick is impossible instead of merely discouraged.

	Static, because this is read at IMPORT to build the declaration — a database read here would run
	before a site exists. What a given GRAIN may actually pick is `subject_options` below, asked per request.
	"""
	from tatva_connect.automation.subjects import SUBJECTS

	return sorted(SUBJECTS)


# The subject that is not universally offered, and the line-level fact that decides it.
_DEAL_SUBJECT = "CRM Deal"


def subject_options(vertical=""):
	"""The subjects a workflow scoped to THIS product line may watch — the declaration above, gated.

	`CRM Vertical.deals_enabled` is the one fact that governs everything deal-related, asked through
	`access.surfaces.deals_enabled` — the SAME function the sidebar's Deals/Contacts/Organizations
	liveness sits on, so a line that does not sell offers no Deal anywhere. A blank vertical is the
	WILDCARD the rest of the app already means by a blank axis: it asks whether any line sells at all.
	"""
	from tatva_connect.access.surfaces import deals_enabled

	offered = _subject_options()
	if deals_enabled(vertical):
		return offered
	return [s for s in offered if s != _DEAL_SUBJECT]


def _field(name, label, fieldtype, **kwargs):
	"""One config field a node type declares. Shape matches the builder's verb params, so the inspector
	renders a node's config and an action's params with ONE renderer, not two.

	A `Grain` field names the master its axis is drawn from. It is deliberately NOT a plain Select of
	every known value: an axis left blank means ANY, so the control has to offer "no restriction" as a
	first-class choice, which a link to the master expresses and an options list does not."""
	return {"name": name, "label": label, "type": fieldtype, **kwargs}


NODE_TYPES = {
	TRIGGER: {
		"label": "Trigger",
		"description": "What starts this workflow. Exactly one per workflow, and the only node with no inbound edge.",
		"outputs": ["next"],
		"singleton": True,
		# The Trigger gates its OWN fields on its OWN mode — Wait's shipped pattern; what W1-contract.md:212 rejects is a node morphing on ANOTHER node's mode, and no other node type may gate on `mode`.
		"config": [
			_field("mode", "Starts on", "Select", options=[MODE_RECORD, MODE_SCHEDULE], reqd=True,
			       default=MODE_RECORD,
			       help="A record event starts one journey the moment a record is saved. A schedule takes everyone who matches, on a rhythm, and starts a journey for each."),
			# Declared in EVERY mode: "only when" on a save is "who is in the cohort" on a schedule, one predicate.
			_field("subject_doctype", "Subject", "Select", options=_subject_options(), reqd=True,
			       help="Which record this workflow watches. It decides every field offered below and in every node after this one, so set it first."),
			_field("event", "Event", "Select", options=["Created", "Updated", "Deleted"], reqd=True,
			       depends_on_value={"mode": [MODE_RECORD]},
			       help="Which save starts the journey."),
			# How often the cohort is taken; `cohort.next_run_at` names the API rejected and why.
			_field("schedule", "Repeats", "Select", options=list(SCHEDULES), reqd=True,
			       depends_on_value={"mode": [MODE_SCHEDULE]},
			       help="How often everyone matching Only when is gathered up again."),
			_field("schedule_time", "At", "Time", placeholder="09:00",
			       depends_on_value={"mode": [MODE_SCHEDULE]},
			       help="Time of day the schedule runs, on the site's own clock. Blank runs it at midnight."),
			_field("vertical", "Vertical", "Grain", link="CRM Vertical",
			       help="Narrows this workflow to one vertical. Leave it blank for any — and blank is what scopes the pickers below to everything."),
			_field("group", "Group", "Grain", link="CRM Group",
			       help="Narrows this workflow to one group. Leave it blank for any."),
			_field("program", "Program", "Grain", link="CRM Program",
			       help="Narrows this workflow to one programme. Leave it blank for any. Set it and every picker below offers only what that programme can reach."),
			# Declared in EVERY mode, like the subject it narrows — the authoring experience is singular and
			# nothing here may gate on `mode`. Sits after the subject that scopes it and BEFORE the predicate
			# that consumes it, so the chain reads top to bottom exactly as AI Voice Call's params do.
			# Blank means ANY, the same semantic a blank grain axis already carries.
			_field("working_set", "Fields used", "Field Set",
			       placeholder="Every field on the subject",
			       help="Trims every picker in this workflow to the handful of fields it really works with. Nothing is blocked — each picker keeps a Show all fields link."),
			_field("predicate", "Only when", "Predicate",
			       help="Who this workflow is for. On a schedule it is the cohort. Values come from Subject above — choose that first or there is nothing to test."),
			# W8.4. Declared in EVERY mode — a save lane can re-enrol too — but it is the SCHEDULE that
			# makes it necessary: a cohort re-selects everyone its criteria match, every tick, so a monthly
			# welcome journey sends the welcome every month for ever. Last, because it qualifies the whole
			# Trigger rather than any one field above it.
			_field("once_per_subject", "Only once per patient", "Check",
			       help="Enrols each patient once and never again, however often they match. A schedule re-selects everyone who matches on every run, so without this a monthly journey sends the same welcome every month."),
		],
	},
	"Route": {
		"label": "Route",
		"description": "Branches on the first matching condition, tried top to bottom. Anything matching no row takes Otherwise.",
		# Outputs are this node's OWN rows (one edge each) followed by a reserved `otherwise`. The rows lead
		# and the fixed base follows — the same `rows_from` seam Wait uses, reading own config, no mode-map.
		"outputs_by": {
			"base": ["otherwise"],
			"rows_from": {"declares": "routes", "key": "id"},
		},
		"config": [_field("routes", "Routes", "Route Rows", reqd=True,
		                  help="One branch per condition, tried top to bottom. Each row draws its own handle on the canvas — wire it there.")],
	},
	"Sample": {
		"label": "Sample",
		"description": "Splits by chance into arms, for a trial or a control group. A lead always lands in the same arm.",
		# The SAME `rows_from` seam Route reads its own config through — rows lead, the fixed base follows.
		# Sample is a SEPARATE node from Route and never a mode of it: this node's rule is that assignment
		# is stable per lead, and that rule is meaningless on a conditional, so merging them would put a
		# dead control on every Route.
		"outputs_by": {
			"base": ["remainder"],
			"rows_from": {"declares": "arms", "key": "id"},
		},
		"config": [_field("arms", "Arms", "Sample Rows", reqd=True,
		                  help="Each arm takes the share you give it. They need not add up to a hundred — whatever is left over takes Remainder.")],
	},
	"Set Variables": {
		"label": "Set Variables",
		"description": "Computes values into the journey for later nodes to read. To change who owns a lead, use Assign to User.",
		"outputs": ["next"],
		"config": [_field("assign", "Values", "Code", reqd=True,
		                  reads="expression", writes="expression_dict",
		                  placeholder='{"risk_band": "high"}',
		                  help="Names and their values, as one expression. Every name set here is offered to the nodes below this one.")],
	},
	"Wait": {
		"label": "Wait",
		"description": "Suspends the journey until an event arrives, a clock expires, or whichever comes first.",
		# Outputs depend on the mode: waiting only on an event has no timeout edge to draw or validate.
		"outputs_by": {
			"field": "mode",
			# A Wait naming a source node that OFFERS buttons draws one edge per DECLARED button, so an author routes a tap by wiring rather than by writing a condition. The rows come from the SEND's own declaration - never from whatever button happened to arrive, which would let a provider invent edges on our canvas.
			# `replaces` names the leg the rows stand in for, so Event-or-Timeout keeps the timeout edge it declares beside them - without it the rows replaced the whole map and the timer leg could not be wired.
			"rows_from": {"node_field": "source_node", "declares": "buttons", "key": "id", "replaces": "event"},
			"map": {
				UNTIL_EVENT: ["event"],
				FOR_DURATION: ["next"],
				UNTIL_TIME: ["next"],
				EVENT_OR_TIMEOUT: ["event", "timeout"],
			},
		},
		"config": [
			_field("mode", "Mode", "Select",
			       options=[UNTIL_EVENT, FOR_DURATION, UNTIL_TIME, EVENT_OR_TIMEOUT], reqd=True,
			       help="What ends the wait: something happening, a length of time, a moment on the calendar, or whichever of an event and a timeout comes first. It also decides which handles this node draws."),
			_field("source_node", "Waiting on", "Node",
			       help="Which earlier node this wait listens to. Only nodes that report an outcome AND always run before this one are offered — wire this node up on the canvas and they appear.",
			       depends_on_value={"mode": [UNTIL_EVENT, EVENT_OR_TIMEOUT]}),
			_field("event_name", "Outcome", "Outcome",
			       help="Which of that node's outcomes wakes the journey. Choose Waiting on first — this list is that node's own.",
			       depends_on_value={"mode": [UNTIL_EVENT, EVENT_OR_TIMEOUT]}),
			# {dotted payload path: state key} — the VALUES are what this Wait writes, hence `payload_map`.
			_field("accepts", "Accepts", "Code", writes="payload_map",
			       placeholder='{"data.button_id": "chosen_button"}',
			       help="Optional. Lifts values out of the arriving event into the journey so later nodes can read them — each entry maps a path in the event to a name you choose. Blank means wake me, I need nothing from it.",
			       depends_on_value={"mode": [UNTIL_EVENT, EVENT_OR_TIMEOUT]}),
			# A2 — one field was the delay, the timeout AND the instant, so it served none of them; two jobs, two declarations, gated on this node's OWN mode, which is the pattern this node type already ships.
			_field("duration", "Wait for", "Duration",
			       help="How long the journey waits before moving on.",
			       depends_on_value={"mode": [FOR_DURATION, EVENT_OR_TIMEOUT]}),
			# Both ways of naming an instant: a care journey's real one is usually the patient's own date.
			_field("until_time", "Wait until", "Instant",
			       modes=[refs.LITERAL, refs.FROM_CONTEXT, refs.EXPRESSION],
			       mode_controls={refs.LITERAL: "datetime"},
			       help="A moment on the calendar, or a date the run is carrying — choose a value to wait for the patient's own appointment or review date.",
			       depends_on_value={"mode": [UNTIL_TIME]}),
		],
	},
	"Terminal": {
		"label": "End",
		"description": "Ends the journey. Declares no outputs, so the canvas draws no handle to drag from.",
		"outputs": [],
		"config": [],
	},
}


def declaration(node_type):
	"""One node type's declaration, or throw. The ONE lookup — nothing keys into NODE_TYPES directly."""
	found = NODE_TYPES.get(node_type)
	if not found:
		frappe.throw(
			_("Unknown node type {0}. Known: {1}").format(node_type, ", ".join(sorted(NODE_TYPES))),
			title=_("Unknown node type"),
		)
	return found


def outcomes_for(node_type):
	"""The events this node type can emit — the choices a downstream Wait may name.

	Declared with the verb, so a Wait offers exactly what the node it waits on can actually produce. A
	free-text event name was the old shape, and a typo there parked a journey for ever with nothing able to
	wake it: unwakeable, indistinguishable from a journey that is legitimately still waiting.
	"""
	from tatva_connect.automation import actions

	return actions.outcomes_of(node_type)


def _rows_from(declared, config, graph_config):
	"""The declared rows a node turns into one edge each, or None when this rule does not do that.

	ONE mode of `outputs_by`, deliberately inside it rather than beside it: a node type still has exactly
	ONE answer to "what can leave here" and one reader (B7). The rows live in one of two places, and the
	spec says which by whether it names a `node_field`:

	  * A SIBLING's config (Wait): `node_field` names the field holding another node's id, and that node's
	    `declares` rows become the edges. IT APPLIES ONLY WHEN THE FIELD IT READS APPLIES — a Wait switched
	    to a pure timer stops drawing one edge per button of the node it no longer waits on. The value is
	    only IGNORED, never cleared, so flipping the mode twice does not lose the wiring.
	  * This node's OWN config (Route): no `node_field`, so the rows are `config[declares]` directly. No
	    sibling to reach, so `graph_config` is not needed.

	`graph_config` is `{node_id: config}` for the graph being judged; the sibling case needs it, the own
	case does not.
	"""
	spec = declared["outputs_by"].get("rows_from")
	if not spec:
		return None
	node_field = spec.get("node_field")
	if node_field:
		if not graph_config:
			return None
		reads = next((f for f in declared["config"] if f["name"] == node_field), None)
		if reads and not _applies(reads, config or {}):
			return None
		source = ((graph_config or {}).get((config or {}).get(node_field)) or {})
	else:
		source = config or {}
	rows = source.get(spec["declares"]) or []
	return [row.get(spec["key"]) for row in rows if isinstance(row, dict) and row.get(spec["key"])]


def outputs_for(node_type, config=None, graph_config=None):
	"""The edge names that may leave this node, given its config. ONE resolver, ONE answer.

	Used by the validator to reject an edge nobody declared and by the canvas to draw handles. The fixed
	part of the answer is a mode-map lookup where a node's outputs vary by a field (Wait), or a constant
	`base` where they do not (Route) — never a fake mode invented to force a fixed shape into the map.
	Declared rows then take their place: they REPLACE a named leg where the fixed part reserves one (Wait's
	buttons stand in for its `event` leg), or they lead and the fixed part follows where there is no leg to
	replace (Route's rows, then `otherwise`). An unset or unknown value yields no rows rather than guessing.
	"""
	declared = declaration(node_type)
	if "outputs" in declared:
		return list(declared["outputs"])
	rule = declared["outputs_by"]
	fixed = list(rule["map"].get((config or {}).get(rule["field"]), [])) if "field" in rule else list(rule["base"])
	rows = _rows_from(declared, config, graph_config)
	if not rows:
		return fixed
	replaces = rule["rows_from"].get("replaces")
	if replaces is None:
		return [*rows, *fixed]
	if replaces not in fixed:
		return fixed
	at = fixed.index(replaces)
	return [*fixed[:at], *rows, *fixed[at + 1:]]


def config_fields(node_type):
	"""The config fields this node type declares, for the inspector and the validator."""
	return list(declaration(node_type)["config"])


# A fault's severity. `blocks` stops a publish — the graph cannot run. `warns` is a true statement that is
# NOT a reason to refuse: a switch shipped off leaves a node mute, which is the intended resting state, not
# an error. Publish counts blocks; the author still sees the warns. There are exactly these two.
BLOCKS, WARNS = "blocks", "warns"

# The ONE code shared across the author-time gate and the runtime engine. An edge to a node that is not in
# the graph is caught by the Bouncer (`graph._edge_problems`) and, if it ever slips past, by the engine
# (`interpreter._Permanent`). They name it by THIS constant so the two can never drift into two catalogs —
# every other code is a call-site literal, but this one must match across a layer boundary, so it is named.
CODE_NODE_NOT_IN_GRAPH = "node-not-in-graph"


def problem(message, field=None, code=None, severity=BLOCKS, fix=None):
	"""One authoring fault, as DATA — the ONE constructor every fault flows through. The canvas needs to
	know WHICH field on WHICH node is wrong so it can mark it; a sentence can only be shown in a toast, and
	a toast naming `b1` in a graph of twenty nodes is barely better than silence. `node_id` is added by the
	caller, the only layer that knows it — `graph._at` wraps this rather than building a second dict.

	Five keys: `code` is a stable lint-rule id (survives a reworded message and gives the rule list a key),
	`severity` decides whether publish refuses, `message` is what is wrong, `fix` is the one line on what to
	do. The call site is the authority on all four, because the check that KNOWS the fault names it.
	"""
	return {"code": code, "severity": severity, "field": field, "message": message, "fix": fix}


# When a rule is enforced. A node is authored over many saves, so SHAPE is checked every time and
# COMPLETENESS only when the author says the graph is finished. Enforcing completeness at save makes the
# canvas unusable: dragging a Route on and saving before configuring it is ordinary work, not an error,
# and a node type that cannot yet be configured becomes permanently unsaveable.
DRAFT, PUBLISH = "draft", "publish"



def _contract():
	"""Lazy: `contract` reaches `registry`, so importing it at module scope is a cycle. Rejected
	`frappe.get_attr` with dotted strings — it would resolve per call and hide a typo until that one
	field was configured; a lambda fails at import like every other declaration here."""
	from tatva_connect.workflow_engine import contract

	return contract


def _expression_problem(value):
	"""The `reads=expression` check. `expr.assert_parses` was DEAD CODE — its only caller was a test —
	which is why publish never syntax-checked an author's expression and a typo died on a live record."""
	from tatva_connect.automation import expr

	if not value:
		return None
	try:
		expr.assert_parses(value)
	except SyntaxError as e:
		return _("{0} is not a valid expression: {1}").format(value, e.msg)
	return None


def _json_problem(value):
	"""The `reads=ctx_json` / `writes=payload_map` check — it must PARSE. `frappe.parse_json` is the
	platform's own reader and is what the runtime uses, so a body this accepts is one the journey can read."""
	if not value:
		return None
	try:
		frappe.parse_json(value)
	except Exception:
		return _("This is not valid JSON.")
	return None


def _predicate_rows_trees(rows):
	"""The condition each of a LIST of predicate rows holds."""
	return [row.get("condition") for row in rows or [] if isinstance(row, dict)]


def _predicate_rows_keys(rows):
	"""Every field a LIST of predicate rows references — `_predicate_fields` over each row's condition."""
	keys = set()
	for tree in _predicate_rows_trees(rows):
		keys |= _contract()._predicate_fields(tree)
	return keys


# WHICH NAMES a field of this kind references, its check, and — for a kind holding a PREDICATE — the trees inside it, so the value gate runs off this declaration and never off a per-type check. A field declares `reads` only when its TYPE does not already imply one — see FIELD_TYPES.
READ_KINDS = {
	"variable": {"keys": lambda v: {v} if isinstance(v, str) and v else set(), "check": None},
	"predicate": {"keys": lambda v: _contract()._predicate_fields(v), "check": None, "trees": lambda v: [v]},
	"predicate_rows": {"keys": _predicate_rows_keys, "check": None, "trees": _predicate_rows_trees},
	"expression": {"keys": lambda v: _contract()._expression_keys(v), "check": _expression_problem},
	"ctx_json": {"keys": lambda v: _contract()._ctx_json_keys(v), "check": _json_problem},
	"value_rows": {"keys": lambda v: _contract().value_row_keys(v), "check": None},
	"value_pair": {"keys": lambda v: _contract().value_pair_keys(v), "check": None},
}

# WHICH NAMES a field of this kind contributes for later nodes to read. A resolver returning None means
# "cannot be enumerated", which makes the node an opaque writer.
WRITE_KINDS = {
	"expression_dict": {"keys": lambda v: _upstream()._expression_dict_keys(v), "check": _expression_problem},
	"payload_map": {"keys": lambda v: _upstream()._payload_map_keys(v), "check": _json_problem},
}


def _upstream():
	from tatva_connect.workflow_engine import upstream

	return upstream


# THE ONE TABLE OF FIELD TYPES. A row per type: the control the inspector draws, the check the validator
# applies, whether frappe-ui's own FormControl renders it, and the read kind the type IMPLIES.
# `reads` is set only where a type can read exactly one way. `Code` and `Data` carry three and two
# different semantics respectively across their fields, so those declare `reads` on the FIELD instead —
# putting it here would force `Call API.request_body`, `Set Variables.assign` and `Wait.accepts` to share
# one kind and the publish gate would extract references the wrong way for two of them, silently.
def validate_node(node_type, config, edge_outputs, mode=PUBLISH, graph_context=None):
	"""Every rule a node must satisfy, in one place. Returns a list of `problem()` records.

	Returns rather than throws so one save can report every problem at once — a builder that surfaces
	them one per attempt makes the author fix a five-field node in five round trips.

	`graph_config` is `{node_id: config}` for the graph this node sits in. A node whose outputs derive from
	a SIBLING's declaration - a Wait drawing one branch per button its source node offers - cannot be
	judged in isolation, so a caller that knows the graph passes it and one that does not still gets every
	other rule.

	`mode=DRAFT` defers the "this setting is required" rule and nothing else. A setting the type never
	declared, a value outside its options, a malformed predicate or an edge on an undeclared output are
	all wrong whatever the author intends next, so they are refused at every save.
	"""
	problems = []
	completeness = mode == PUBLISH
	declared = declaration(node_type)
	# Declared defaults are in play before any rule reads the config — see `with_defaults`.
	config = with_defaults(node_type, config)

	known = {f["name"] for f in declared["config"]}
	for name in sorted(set(config) - known):
		problems.append(problem(
			_("{0} does not take a setting called {1}.").format(node_type, name), name,
			code="field.unknown", fix=_("Remove this setting — the node type does not use it."),
		))

	for field in declared["config"]:
		if field.get("reqd") and not _applies(field, config):
			continue
		if completeness and field.get("reqd") and not config.get(field["name"]):
			problems.append(problem(
				_("{0} needs {1}.").format(node_type, field["label"]), field["name"],
				code="field.required", fix=_("Fill in {0}.").format(field["label"]),
			))
		value = config.get(field["name"])
		if field.get("writes"):
			problems.extend(
				problem(m, field["name"], code="field.not-settable",
				        fix=_("Pick a field automation is allowed to write."))
				for m in _written_name_problems(node_type, config, field)
			)
		# Three tables, three questions, and ALL of them run. This used to `continue` after the row check,
		# so a type carrying both a row check and a read kind would silently skip the read check.
		row = FIELD_TYPES[field["type"]]
		if row["check"]:
			# The context reaches a check only at PUBLISH. A rule ABOUT THE GRAPH cannot judge a node while
			# the graph is still being built - the author may add the Trigger, or change the subject, after
			# this node. These rules lived in the publish gate before they moved onto the table, and moving
			# a rule must not change WHEN it fires: enforcing them at save refused a node an author was
			# midway through writing, with no way forward.
			problems.extend(
				problem(m, field["name"], code="field.invalid", fix=_("Correct {0}.").format(field["label"]))
				for m in row["check"](value, field, config, graph_context if completeness else None)
			)
		kind = read_kind_of(field)
		if kind and READ_KINDS[kind]["check"]:
			found = READ_KINDS[kind]["check"](value)
			if found:
				problems.append(problem(found, field["name"], code="field.reads-invalid",
				                        fix=_("Fix {0} so it parses.").format(field["label"])))
		# Graph-shaped like every `check` above, so it keeps their timing: context arrives at PUBLISH only.
		if kind and READ_KINDS[kind].get("trees") and completeness:
			problems.extend(
				problem(m, field["name"], code="field.invalid",
				        fix=_("Pick the value from the list rather than typing it."))
				for tree in READ_KINDS[kind]["trees"](value)
				for m in _unmatchable_values(tree, graph_context)
			)
		if field.get("writes") and WRITE_KINDS[field["writes"]]["check"]:
			found = WRITE_KINDS[field["writes"]]["check"](config.get(field["name"]))
			if found:
				problems.append(problem(found, field["name"], code="field.writes-invalid",
				                        fix=_("Fix {0} so it parses.").format(field["label"])))

	allowed = set(outputs_for(node_type, config, (graph_context or {}).get("configs")))
	for output in sorted(set(edge_outputs or []) - allowed):
		problems.append(problem(
			_("{0} declares no output called {1}.").format(node_type, output)
			if allowed else _("{0} has no outgoing edges.").format(node_type),
			code="output.undeclared",
			fix=_("Wire only outputs this node declares."),
		))
	return problems


def _predicate_problems(value, field, config=None, context=None):
	"""Structural rules for a predicate tree. Empty is fine — an empty gate is open.

	Shape only: whether a rule's FIELD exists is a question about the subject, and the subject is chosen
	on the same node, so it is answered by the evaluator against a real context rather than guessed here.
	What this catches is the malformed tree — an unknown node type, a `not` with two children, a rule
	with no operator — none of which can be right for any subject. A rule's VALUE is the `reads`
	declaration's question, answered once for every kind holding a predicate — see READ_KINDS.
	"""
	if not value:
		return []
	try:
		_walk_predicate(value, field["label"])
	except ValueError as bad:
		return [str(bad)]
	return _unwatchable_transitions(value, field, context)


def _unwatchable_transitions(tree, field, context):
	"""`changed to` on a field that carries no before-value matches NOTHING, and said so nowhere.

	Only a WATCHED field gets its `__before` captured (`context.diff_watched_fields`), so a transition
	operator on any other field is dead on every record — published green, never fired. Four of the six
	subjects have no field catalog at all, so every transition an author could build on them was silent.

	`fields.is_watchable` was written for exactly this question and had no caller; this is it.
	"""
	from tatva_connect.automation import fields, rules

	subject = (context or {}).get("subject")
	if not subject:
		return []
	by_slug = {refs.slug(dt): dt for dt in (subject, "CRM Lead")}
	found = []
	for rule in _contract()._predicate_rules(tree):
		if rule.get("operator") not in rules._CHANGE_OPS:
			continue
		parsed = refs.parse(rule.get("field") or "")
		doctype = by_slug.get(parsed[0]) if parsed else None
		if not doctype or fields.is_watchable(doctype, parsed[1]):
			continue
		found.append(
			_("{0} tests whether {1} changed, but {2} is not watched — nothing records what it held before, "
			  "so this can never match.").format(field["label"], parsed[1], doctype)
		)
	return found


# `contains` is absent deliberately — a substring of a composite key tests one stage leaf across every programme, which is correct.
_MATCHED_OPS = frozenset({"is", "is not", "is one of", "is not one of"})


def _unmatchable_values(tree, context):
	"""A predicate value that names no record of the grain-scoped master it is compared against.

	A grain-scoped master's PK is a composite `::` string and its Link column holds THAT, not the word a
	person says. `taxonomy.picklist` is the one seam that knows it and every other consumer resolves
	through it; a predicate was the only one that never did, so a branch comparing Zone to `North`
	published green and matched nobody for ever. A master row is config, not a runtime fact — the same
	reading `_link_grain_problems` already takes at publish.
	"""
	if not tree or not context:
		return []
	index = refs.readable_index(context.get("subject") or "", "CRM Lead")
	found = []
	for rule in _contract()._predicate_rules(tree):
		master = _composite_master(index.get(rule["field"]))
		if not master or rule.get("operator") not in _MATCHED_OPS:
			continue
		found += [_unmatchable_message(one, master, rule["field"], context)
		          for one in _value_items(rule.get("value"), rule["operator"])
		          if not frappe.db.exists(master, one)]
	return found


def _value_items(value, operator):
	"""One value, or a membership operator's list — split by the SAME rule the evaluator splits by."""
	from tatva_connect.automation import rules as rules_brain

	if not isinstance(value, str) or not value.strip():
		return []
	return rules_brain._split_list(value) if operator in rules_brain._MEMBERSHIP_OPS else [value]


def _composite_master(descriptor):
	"""The master a Link descriptor points at, when that master's keys are composite; else None."""
	from tatva_connect.taxonomy import picklist

	if not descriptor or descriptor.get("type") != "Link":
		return None
	master = (descriptor.get("pick") or {}).get("target") or descriptor.get("options")
	return master if master in picklist._COMPOSITE_PK_MASTERS else None


def _unmatchable_message(value, master, ref, context):
	"""Says what is wrong, and — when the seam can resolve it — the key the author meant."""
	from tatva_connect.taxonomy import picklist

	fieldname = (refs.parse(ref) or ("", ref))[1].rsplit(".", 1)[-1]
	grain = tuple((context.get("grain") or {}).get(axis) or "" for axis in _GRAIN_AXES)
	meant = picklist.resolve_value(master, value, grain, fieldname)
	if meant and meant != value:
		return _("{0} is not a {1} — the column holds its key. Use {2}.").format(value, master, meant)
	return _("{0} is not a {1}, so this condition can never match.").format(value, master)


def _walk_predicate(node, label, depth=0):
	from tatva_connect.automation.rules import ALL, ANY, KNOWN_OPERATORS, NOT, RULE

	if depth > 20:
		raise ValueError(_("{0} is nested too deeply.").format(label))
	if not isinstance(node, dict):
		raise ValueError(_("{0} is malformed — every part of a condition must be an object.").format(label))
	kind = node.get("type")
	if kind == RULE:
		if not node.get("field"):
			raise ValueError(_("A condition in {0} does not say which field to test.").format(label))
		if node.get("operator") not in KNOWN_OPERATORS:
			raise ValueError(
				_("{0} is not an operator this system knows.").format(node.get("operator") or "—")
			)
		return
	if kind not in (ALL, ANY, NOT):
		raise ValueError(_("{0} contains an unknown condition type {1}.").format(label, kind))
	children = node.get("children") or []
	if not children:
		raise ValueError(_("A group in {0} is empty — remove it or put a condition in it.").format(label))
	if kind == NOT and len(children) != 1:
		raise ValueError(_("A 'not' in {0} must hold exactly one condition.").format(label))
	for child in children:
		_walk_predicate(child, label, depth + 1)


# The engine's own namespace, and the one source name an author may not write as. Bookkeeping used to sit
# in the SAME flat dict as the journey's variables under four bare names, so a capture called `_emitted`
# replaced the map the wake depends on and the journey parked for ever with nothing able to reach it. It now
# lives under `_engine.*` and an author's value lives under its NODE, so the collision is gone
# structurally. This stays because the two namespaces must not be confusable by sight either.
RESERVED_SOURCE = refs.ENGINE

# The names an author may not take, spelled ONCE — in `refs`, where the engine's namespace is declared.
# It used to be four bare keys because the engine shared one flat dict with the journey's variables; there is
# now exactly one name to refuse, because there is exactly one engine source.
RESERVED_VARIABLES = (refs.ENGINE,)


def _reserved_problems(names):
	"""The ONE reserved-name rule, applied to a bag of names. Both the Mapping check and the `writes`
	check below phrase the refusal identically because they are the same rule about the same name.

	A leading underscore is refused wholesale rather than just `_engine`: the engine's namespace is the
	underscore-prefixed one, and an author who writes `_token` means to reach engine bookkeeping whether or
	not that exact key exists today.
	"""
	return [
		_("{0} is reserved for the engine. Choose another name for this value.").format(name)
		for name in names
		if name and str(name).startswith("_")
	]


def _variable_problems(value, field, config=None, context=None):
	"""Every rule a captured variable name must satisfy."""
	return _reserved_problems([
		(row or {}).get("variable") for row in (value or []) if isinstance(row, dict)
	])


def _written_name_problems(node_type, config, field):
	"""Reserved names, asked of a field that WRITES journey state rather than one that captures into it.

	`Set Variables.assign` and `Wait.accepts` are `Code`, not `Mapping`, so the reserved-name rule never
	reached them: `{"_emitted": "x"}` published green, `state.update` then replaced the correlation map
	the wake depends on, and the journey parked for ever. For `accepts` this was reachable by anyone who can
	edit the subject, since `signals.deliver_signal` routes an external payload through that map.

	The keys are read through `upstream.write_fields_of` — the same enumerator the journey's own available-
	values walk uses — so what is refused here is exactly what the journey would really merge. A field whose
	keys cannot be enumerated (a computed `assign` key) yields `None` and is NOT checked: it cannot be,
	without evaluating the author's expression. That gap is the price of allowing computed keys at all,
	and the runtime is unchanged by it.

	Imported lazily: `upstream` imports this module at load time, so a module-level import here would be
	circular.
	"""
	from tatva_connect.workflow_engine import upstream

	problems = []
	for declared, keys in upstream.write_fields_of(node_type, config):
		if declared["name"] != field["name"] or keys is None:
			continue
		problems += _reserved_problems(sorted(keys))
	return problems


def graph_context(nodes):
	"""The graph-level facts a check may need, resolved ONCE for the whole graph.

	There were two validators and a rule landed in whichever could REACH its facts: `validate_node` saw
	one node and a config map, `graph.py` saw everything and was hand-written. So `Target` and `Field`
	grew per-type switches in `graph.py` purely because that is where `subject` was reachable, and the
	Trigger was looked up four separate times in that one file.

	Generalised deliberately rather than passing `grain` alone: the next rule needing a different graph
	fact would strand exactly the same way and get hand-written again.

	`grain` is a RULE grain — a BLANK axis means ANY and is kept blank, never flattened to "". It is only
	ever compared through `taxonomy.grain`, never as a tuple.
	"""
	triggers = [n for n in nodes if n.get("node_type") == TRIGGER]
	trigger = triggers[0] if triggers else None
	trigger_config = config_of(trigger) if trigger else {}
	return {
		"configs": {n["node_id"]: config_of(n) for n in nodes if n.get("node_id")},
		"triggers": triggers,
		"trigger": trigger,
		"subject": trigger_config.get("subject_doctype") or "",
		"grain": {axis: trigger_config.get(axis) or "" for axis in _GRAIN_AXES},
	}


def _target_problems(value, field, config, context):
	"""A write must aim at a record the journey can actually reach. MOVED off `graph._write_target_problems`.

	Refuses: a doctype that is neither the Lead nor the subject the Trigger watches — `_resolve_write_target`
	raises for anything else at runtime, so the author would find out from a dead journey on a real patient.
	Does NOT refuse: whether a particular record exists, or whether this lead has one. Both are runtime.
	"""
	if not value or not context:
		return []
	from tatva_connect.automation import actions

	reachable = set(actions.reachable_targets(context["subject"]))
	if value in reachable:
		return []
	return [_("{0} writes to {1}, which this workflow never touches. It can write to: {2}")
	        .format(field["label"], value, ", ".join(sorted(reachable)))]


def _settable_problems(value, field, config, context):
	"""A written field must be one the operator allowed automation to set. MOVED off `graph`.

	MEMBERSHIP ONLY, via `fields.is_set_declared`. The grain-specific decision stays at execution on
	purpose: the workflow's grain is a RULE grain whose blank axis means ANY, and handing that to
	`is_settable` — which expects a lead's DATA grain — is the exact defect this gate must not commit.
	Does NOT refuse a field on a target that is itself already refused: one fault, one message.
	"""
	target = _written_doctype(field, config)
	if not value or not context or not target:
		return []
	from tatva_connect.automation import actions, fields

	if target not in set(actions.writable_records(context["subject"])):
		return []
	if fields.is_set_declared(target, value):
		return []
	return [_("{0} is not a field automation is allowed to set on {1}.").format(value, target)]


def _written_doctype(field, config):
	"""The RECORD a `doctype_from` sibling names. A child-row node names a `CRM Lead Section`, and the
	record its rows live on is that section's target — resolved by the section brain, so the gate and the
	handler read the same config the same way."""
	from tatva_connect.partner_api.doctype.crm_lead_section import crm_lead_section

	value = config.get(field.get("doctype_from") or "")
	section = crm_lead_section.section_for_child(value)
	return section.target_doctype if section else value


def _settable_rows_problems(value, field, config, context):
	"""W8.1 — `_settable_problems`, asked once per row, and the value that row writes judged beside it.

	Delegates rather than re-deciding: a rows field writes the same fields a single-field one did, so the
	membership question has the same answer and must have the same asker.

	A row whose FIELD is already refused is not asked about its VALUE — one fault, one message, the same
	restraint `_settable_problems` shows for a field on a target that is itself refused.
	"""
	target = _written_doctype(field, config)
	# Read ONCE for the whole table, and only at PUBLISH — on this table `context` is how a check is told which pass it is in.
	known = _target_fields(target) if context and target else {}
	found = []
	for row in value or []:
		if not isinstance(row, dict) or not row.get("name"):
			continue
		refused = _settable_problems(row["name"], field, config, context)
		found += refused or _literal_value_problems(row, known.get(row["name"]))
	return found


def _target_fields(doctype):
	"""The written record's own field vocabulary, keyed by fieldname — asked of `describe.fields_for_doctype`.

	THE resolver the builder's own pickers read, so what publish judges a value against is exactly what the
	author was offered. A bare `get_meta` walk here would be a second vocabulary AND a smaller one: a CRM
	Task's real business fields are `CRM Task Type Field` rows and never columns of the doctype.
	"""
	from tatva_connect.automation import describe

	return {declared["key"]: declared for declared in describe.fields_for_doctype(doctype)}


# WORDING ONLY, never a decision — the plain word for a type a literal can fail on, translated where it is said, like every other label this file declares.
_TYPE_WORDS = {
	"Date": "a date",
	"Datetime": "a date and time",
	"Int": "a whole number",
	"Float": "a number",
	"Currency": "an amount",
	"Percent": "a percentage",
	"Check": "a tick — 1 or 0",
}


def _literal_value_problems(row, declared):
	"""A literal the target field can never hold, refused HERE — where the author can still fix it.

	A word typed into a date, or a source nobody ever declared, publishes green today and then dies inside a
	background job on the first live patient, with no author anywhere near it. This is the same argument
	`_duration_problems` makes: publish is the only place the fault is catchable against the person who
	typed it.

	ONLY a literal is judgeable. From Context, Expression and Increment are resolved from a live run, so
	nothing here could judge them and pretending otherwise would block an author with no way forward — the
	modes are named exactly as `contract.resolve_row` branches on them, so a row carrying no mode is judged
	as the literal the runtime would really write. A blank is not judged: whether the row needs a value at
	all is the `reqd` rule's question, and answering it twice gives one mistake two messages.

	`describe.coerces` is the asker — the value half of the builder contract, already the one answer to "can
	this field hold this". A Select is a fixed list rather than a cast, so it is refused by `_option_problems`,
	the sentence this file already says for a value outside a declared list, asked about the TARGET field's
	options rather than a config field's.
	"""
	from tatva_connect.automation import describe

	if row.get("mode") in (refs.FROM_CONTEXT, refs.EXPRESSION, refs.INCREMENT):
		return []
	value = row.get("value")
	if not declared or value in (None, ""):
		return []
	ftype = declared["type"]
	if ftype == "Select" and declared.get("options"):
		return _option_problems(value, {"label": declared["label"], "options": declared["options"]},
		                        config=None, context=None)
	if describe.coerces(value, ftype):
		return []
	word = _TYPE_WORDS.get(ftype)
	return [_("{0} is not a valid {1} — that field holds {2}.").format(
		value, declared["label"], _(word) if word else _("a {0} value").format(ftype))]


def _link_grain_problems(value, field, config, context):
	"""A grain-scoped link must name something the workflow's grain could ever reach.

	Scoping is derived from the TARGET'S OWN SCHEMA (`_carries_grain`), exactly as `_scope_kind` derives
	which controls are narrowed — never from a per-field flag someone has to remember to set. A link whose
	target carries no axes (a Webhook, a template, a User) is not grain-scoped and is never checked here.

	`grain.overlaps` is the ONE matcher and the right half of it: both sides are RULES, so EITHER may
	leave an axis blank meaning ANY. `covers` would compare the workflow's blank axis as a literal empty
	string — the shape that once hid 129 fields from 1,894 leads with every test green.

	Refuses: a value whose own grain can never overlap the workflow's. Does NOT refuse a missing record,
	nor anything about a lead — whether a given patient matches is a runtime fact publish cannot know.
	"""
	link = field.get("link")
	if not value or not context or not link or not _carries_grain(link):
		return []
	from tatva_connect.taxonomy import grain as grain_brain

	axes = frappe.db.get_value(link, value, _GRAIN_AXES, as_dict=True)
	if not axes:
		return []
	if grain_brain.overlaps(axes, *(context["grain"].get(a) for a in _GRAIN_AXES)):
		return []
	return [_("{0} is outside this workflow's grain, so it could never be used.").format(value)]


def _option_problems(value, field, config, context):
	"""The value is one the field declares. MOVED here from `validate_node`'s body, where it read
	`field.get("options")` for every type and only ever applied to this one.

	Refuses: a stored value outside the declared list — a stale option after a rename, or a hand-edited
	config_json. Deliberately does NOT refuse a blank: whether the setting is required is the `reqd` rule's
	question, and answering it twice would give the author two messages for one mistake.

	A field that declares no options refuses nothing — `options` is this vocabulary's word for "the fixed
	list of values this field accepts", and a type whose values are not a fixed list simply has none. A
	`Code` field used to declare `options="JSON"` here, which is frappe's DocType word for which syntax the
	DESK's code editor highlights and means nothing in this table; one key with two meanings is the second
	brain this file exists to prevent, so those declarations are gone and what a Code field holds is said by
	its read/write kind, which is also what parses it.
	"""
	options = field.get("options")
	if not value or not options or value in options:
		return []
	return [
		_("{0} is not a valid {1}. Choose one of: {2}").format(value, field["label"], ", ".join(options))
	]


def _share_problems(value, field, config, context):
	"""A Sample's arms must add up to a share that can actually be honoured.

	NO NEW VALIDATOR SEAM — this is a `check` on the field type, exactly where `Select`, `Link`, `Field`
	and `Predicate` already put theirs, so `graph.py` gains nothing and the rule fires wherever
	`validate_node` runs. It refuses only what is arithmetically impossible: a share that is not a positive
	number, and a total above the whole. It does NOT demand the arms sum to exactly 100 — the leftover IS
	the Remainder edge, which is the point of having one.
	"""
	found, total = [], 0.0
	for row in value if isinstance(value, list) else []:
		if not isinstance(row, dict):
			continue
		name = row.get("label") or row.get("id") or "?"
		try:
			share = float(row.get("percent"))
		except (TypeError, ValueError):
			found.append(_("{0} needs a percentage.").format(name))
			continue
		if share <= 0:
			found.append(_("{0} is {1}% — an arm nobody can land in is a branch that never runs.").format(
				name, _trim(share)))
			continue
		total += share
	if total > 100:
		found.append(_("The arms add up to {0}% — more than the whole, so the last of them cannot be "
		               "honoured.").format(_trim(total)))
	return found


def _trim(number):
	"""A share reads as `40` and `12.5`, never `40.0` — the author typed one of those and not the other."""
	return int(number) if float(number).is_integer() else number


def _delay_units():
	"""The units a delay may be written in — READ OFF `add_to_date` ITSELF, never typed out here.

	They are its integer-defaulted parameters, which is exactly the set it shifts a date by: `date` defaults
	to None and the three formatting switches default to booleans, so both fall out without being named. A
	hand-written tuple would be a second vocabulary that drifts the day frappe adds one, and it is the same
	list the control offers — so what an author may pick and what publish accepts cannot disagree.
	"""
	import inspect

	return tuple(
		name
		for name, param in inspect.signature(frappe.utils.add_to_date).parameters.items()
		if isinstance(param.default, int) and not isinstance(param.default, bool)
	)


def _time_problems(value, field, config, context):
	"""A time of day must really be one. `_duration_problems`' twin, and the same defect it describes:
	the declaration said `Data`, so `quarter past nine` published CLEAN and `cohort._at_time_of_day`
	then silently fell back to midnight — a daily cohort the operator set for 09:00 firing at 00:00, with
	nothing anywhere saying so. Parsed by `frappe.utils.get_time`, the same reader the runtime uses."""
	if not value:
		return []
	from frappe.utils import get_time

	try:
		get_time(value)
	except Exception:
		return [_("{0} is not a time of day — write it as 09:00.").format(value)]
	return []


def _duration_problems(value, field, config, context):
	"""A delay must really be `add_to_date` kwargs — refused HERE, where the author can still fix it.

	This is the whole defect behind the "Duration or time" box: the declaration said `Data`, so any string
	was accepted, and `wait_resume_at` then threw on a LIVE journey with a message about add_to_date kwargs
	that means nothing to the person who typed `2 minutes`. Publish is the only place it is catchable
	against the author rather than against a patient.

	Enumerated through `expr.dict_literal_keys`, the same reader `upstream._expression_dict_keys` uses for a
	Set Variables node, so "is this a dict literal and what are its keys" is answered once. It returns None
	for anything computed — `{"days": ctx["x"]}` — and that is passed, not guessed at: only running it could
	say, and a false block would stop an author with no way forward. A broken expression is already reported
	by the `expression` read kind, so this stays silent on it rather than saying the same thing twice.
	"""
	if not value:
		return []
	from tatva_connect.automation import expr

	try:
		keys = expr.dict_literal_keys(value)
	except (SyntaxError, ValueError, TypeError):
		return []
	if keys is None:
		return []
	units = _delay_units()
	if not keys:
		return [_("{0} is empty, so the journey would wait for no time at all.").format(field["label"])]
	unknown = sorted(k for k in keys if k not in units)
	if unknown:
		return [_("{0} is not a length of time. Use one of: {1}").format(
			", ".join(unknown), ", ".join(units))]
	return []


# `scalar` — does the stored value fit on a line as itself? A list or a tree does not, and a card that
# prints one shows `[object Object]`. `summary` is how such a value is NAMED instead: `{"count": noun}`
# renders "3 required", `{"phrase": text}` a fixed sentence where a count means nothing. Every non-scalar
# type MUST declare a summary, and that pairing is locked — the card was the SEVENTH consumer of this
# vocabulary to name types itself, and it named only three of the five it needed.
#
# `check(value, field, graph_config)` — what PUBLISH refuses for this type. A check may not invent: given
# no `graph_config` it returns no problem rather than guessing, because a false problem blocks an author
# who has no way to fix it. Why each row carries what it carries:
#   Data / Small Text  no check — any string is a legal value; emptiness is the `reqd` rule's question.
#   Select             the value is one the field declares. Blank is `reqd`'s business, not this one's.
#   Code               no ROW check — it spans three semantics, so the parse belongs to its READ/WRITE
#                      kind (`expression`, `ctx_json`, `payload_map`) and is already done there. A JSON
#                      check here would refuse every valid `Set Variables.assign`.
#   Link               the value's own grain can overlap the workflow's (`_link_grain_problems`); a MISSING record is not refused here — for a grain-scoped master that half sits with the predicate below.
#   Grain              no check — an axis is a link to a master and a BLANK axis means ANY, so there is
#                      no wrong value to refuse; the master link is enforced by the Link field itself.
#   Variable           no check — "does this resolve upstream" needs the whole graph and this node's
#                      position in it. `graph._reference_problems` already answers it from
#                      `upstream.available_map`; a row check would be a second implementation.
#   Field Map          every named row is a field automation may write; the whole-graph half of that
#                      question stays with `graph._write_target_problems`.
#   Predicate          the tree is well formed and its operators exist; whether a rule's VALUE can ever match is asked off the `reads` kind instead, so Route's ROWS inherit it too.
#   Mapping            every captured name is a legal variable name.
#   Value Map          no check — its rows are validated by the `value_rows` read kind.
#   Button List        no check — a button is an id and a label; a duplicate id is caught where it
#                      becomes an edge, by `outputs_for`.
#   Target/Node/Outcome  no check — all three name something ELSEWHERE in the graph, so they are answered
#                      by `graph._write_target_problems` and `graph._wait_problems`.
# NOTHING here refuses a runtime fact. Whether a lead has a number, whether a provider accepts it, and
# whether a lead matches the grain are unknowable at publish and must not be pretended at.
# The readings a card can give a scalar value, and the whole vocabulary of them. `raw` is the value itself — the author typed it, or it IS the human word. The other three name a reading the stored value alone cannot give: a composite primary key, `add_to_date` kwargs, a tick. A row naming anything else is refused by the canvas contract test, and the frontend renders from this word and holds no list of field types.
CARD_READINGS = ("raw", "title", "delay", "label")

FIELD_TYPES = {
	"Data": {"control": "data", "check": None, "primitive": True, "reads": None, "scalar": True, "summary": {"as": "raw"}},
	"Select": {"control": "select", "check": _option_problems, "primitive": True, "reads": None, "scalar": True, "summary": {"as": "raw"}},
	"Small Text": {"control": "textarea", "check": None, "primitive": True, "reads": None, "scalar": True, "summary": {"as": "raw"}},
	# `time` reaches the inspector's own primitive fallback as `<FormControl type="time">` — no widget of its own.
	"Time": {"control": "time", "check": _time_problems, "primitive": True, "reads": None, "scalar": True, "summary": {"as": "raw"}},
	# A boolean goes through frappe-ui's own FormControl, which the inspector already falls through to for
	# every primitive — so a tick needs no branch of its own in the inspector and no bespoke widget.
	"Check": {"control": "checkbox", "check": None, "primitive": True, "reads": None, "scalar": True, "summary": {"as": "label"}},
	"Code": {"control": "code", "check": None, "primitive": False, "reads": None, "scalar": True, "summary": {"as": "raw"}},
	"Link": {"control": "link", "check": _link_grain_problems, "primitive": False, "reads": None, "scalar": True, "summary": {"as": "title"}},
	"Grain": {"control": "grain", "check": None, "primitive": False, "reads": None, "scalar": True, "summary": {"as": "raw"}},
	"Variable": {"control": "value-picker", "check": None, "primitive": False, "reads": "variable", "scalar": True, "summary": {"as": "raw"}},
	"Predicate": {"control": "predicate", "check": _predicate_problems, "primitive": False, "reads": "predicate", "scalar": False, "summary": {"phrase": "has a condition"}},
	"Route Rows": {"control": "route-rows", "check": None, "primitive": False, "reads": "predicate_rows", "scalar": False, "summary": {"count": "routes"}},
	# `reads` is None and that is not an oversight: an arm is a share of chance, so it references no journey
	# state at all. The `check` is where a Sample's one refusable fact lives — see `_share_problems`.
	"Sample Rows": {"control": "sample-rows", "check": _share_problems, "primitive": False, "reads": None, "scalar": False, "summary": {"count": "arms"}},
	"Mapping": {"control": "mapping", "check": _variable_problems, "primitive": False, "reads": None, "scalar": False, "summary": {"count": "captured"}},
	"Value Map": {"control": "value-map", "check": None, "primitive": False, "reads": "value_rows", "scalar": False, "summary": {"count": "mapped"}},
	# A2 — a delay, stored as the `add_to_date` kwargs `wait_resume_at` has always taken; NOT frappe's `Duration` fieldtype and NOT the fork's `DurationInput`/`formatDuration`, which both speak SECONDS, and a month is not a fixed number of them.
	"Duration": {"control": "duration", "check": _duration_problems, "primitive": False, "reads": "expression", "scalar": True, "summary": {"as": "delay"}, "units": _delay_units()},
	# A2 — ONE value with ONE mode: `Value Map`'s row, for a field that holds a single value.
	"Instant": {"control": "instant", "check": None, "primitive": False, "reads": "value_pair", "scalar": False, "summary": {"phrase": "is set"}},
	# W8.1 — Value Map's twin for rows the AUTHOR names: same read kind, so gate and vocabulary arrive with it.
	"Field Map": {"control": "field-map", "check": _settable_rows_problems, "primitive": False, "reads": "value_rows", "scalar": False, "summary": {"count": "fields"}},
	"Button List": {"control": "button-list", "check": None, "primitive": False, "reads": None, "scalar": False, "summary": {"count": "buttons"}},
	# A vocabulary only the PROVIDER knows — fetched server-side from the account a sibling field names, so
	# no credential reaches the browser. No `check`: what a provider offers is a runtime fact, and refusing
	# an agent id at publish would mean calling the provider from the publish gate.
	"Remote Select": {"control": "remote-select", "check": None, "primitive": False, "reads": None, "scalar": True, "summary": {"as": "raw"}},
	# W3.1 — which of the subject's fields this workflow works with. A DISPLAY narrowing and nothing more:
	# `check` and `reads` are both None DELIBERATELY. A read kind would hand these names to
	# `contract.reads_of`, and a field the schema later lost would turn a tidier picker into a publish
	# BLOCK — two answers to "what may be read", which is the second brain this whole declaration avoids.
	# Journey state still falls through to the live document and publish still accepts any real field.
	"Field Set": {"control": "field-set", "check": None, "primitive": False, "reads": None, "scalar": False, "summary": {"count": "fields"}},
	"Target": {"control": "graph-select", "check": _target_problems, "primitive": False, "reads": None, "scalar": True, "summary": {"as": "raw"}},
	"Node": {"control": "graph-select", "check": None, "primitive": False, "reads": None, "scalar": True, "summary": {"as": "raw"}},
	"Outcome": {"control": "graph-select", "check": None, "primitive": False, "reads": None, "scalar": True, "summary": {"as": "raw"}},
}


def config_of(node):
	"""A node's declared settings, as an object. THE one reader.

	It was written out five times — `graph`, `interpreter`, `triggers` and twice in the node controller —
	so a malformed blob had five chances to be handled five ways. `frappe.parse_json` is the platform's
	own reader and is what the runtime already used; this only stops the fifth copy being written.
	"""
	node = node or {}
	# Three shapes reach here: an authored row carrying `config_json` text, a Document, and a caller
	# holding a plain `config` dict. Asked with `.get` because `in` is not a Document's contract, and
	# `graph._config_of` used to know the dict/text split privately - a second reader beside THE one.
	raw = node.get("config_json")
	if raw is not None:
		return frappe.parse_json(raw or "{}") or {}
	return node.get("config") or {}


def read_kind_of(field):
	"""The read kind this field resolves through — its TYPE's inherent kind, or the one it declares.

	The ONE answer, so `contract._references` and the validator cannot disagree about what a field reads.
	"""
	return FIELD_TYPES[field["type"]]["reads"] or field.get("reads")


def field_types():
	"""The table as plain data for the inspector — the control and whether frappe-ui renders it. The
	checks stay server-side; a control name is all the canvas needs to look one up."""
	return {
		name: {"control": row["control"], "primitive": row["primitive"], "summary": row["summary"]}
		for name, row in FIELD_TYPES.items()
	}


@frappe.whitelist()
def graph_outputs(nodes):
	"""Every node's outputs, resolved for the graph it actually sits in. THE answer the canvas draws handles from.

	`outputs_for` was re-implemented in JS — both resolution modes, `rows_from` included — under a comment
	claiming it mirrored this module exactly. Nothing checked the claim, and that function rendered zero
	nodes for a day. The canvas cannot ask a per-node endpoint for this: a Wait's outputs are a fact about
	ANOTHER node's config, so the question is only answerable for a whole graph at once.

	Answers for an UNSAVED graph, like every other authoring endpoint — handles have to redraw while the
	author is still wiring, which is the only moment they matter.
	"""
	if not frappe.has_permission("CRM Workflow", "read"):
		frappe.throw(_("Not permitted"), frappe.PermissionError)

	nodes = frappe.parse_json(nodes) if isinstance(nodes, str) else (nodes or [])
	graph_config = {n["node_id"]: config_of(n) for n in nodes if n.get("node_id")}
	return {
		node["node_id"]: list(outputs_for(node["node_type"], graph_config[node["node_id"]], graph_config))
		for node in nodes
		if node.get("node_id")
	}


def with_defaults(node_type, config):
	"""The config as the node EFFECTIVELY carries it — a declared default fills a key the author never set.

	Frappe's own semantic: a field with a default HAS that default until someone changes it. Without this
	the gate reads a missing key as "no value", so a Trigger authored before `mode` existed had its `event`
	treated as gated off — and a gated-off required field skips every one of its checks, so a bad Event
	value stopped being caught. A default that only the UI knows about is a default the validator disagrees
	with.
	"""
	declared = declaration(node_type) or {}
	effective = dict(config or {})
	for field in declared.get("config") or []:
		if field.get("default") is not None and effective.get(field["name"]) in (None, ""):
			effective[field["name"]] = field["default"]
	return effective


def applied_fields(node_type, config):
	"""The config fields IN PLAY for this node given its own config — the ONE reader of the gate.

	The inspector already asks the same question client-side (`useNodeTypes.appliedFieldsFor`); this is
	the server's answer to it, so a rule that must know "is this field even asked for" reads the gate
	rather than re-deciding it. A gated-off field is not missing, it is out of play.
	"""
	declared = declaration(node_type) or {}
	effective = with_defaults(node_type, config)
	return [f for f in (declared.get("config") or []) if _applies(f, effective)]


def _applies(field, config) -> bool:
	"""Is this field in play, given the config? A field gated on another field's value is not required
	while that gate is shut — a Wait on a pure timer must not be asked for an event name."""
	gate = field.get("depends_on_value")
	if not gate:
		return True
	return all(config.get(name) in values for name, values in gate.items())


# The axes are declared ONCE, by the grain brain. A second tuple here drifts the day an axis is added.
from tatva_connect.taxonomy.grain import AXES as _GRAIN_AXES


def _carries_grain(doctype):
	"""Does this doctype carry the three grain axes? Then a link to it is scoped by the workflow's grain.

	Derived from the target's own schema rather than tagged per field. Tagging means every new link
	someone adds is unscoped until they remember the flag — and nobody notices, because an unscoped
	picker looks exactly like a scoped one until a journey is refused at execution.
	"""
	if not doctype or not frappe.db.exists("DocType", doctype):
		return False
	meta = frappe.get_meta(doctype)
	return all(meta.has_field(axis) for axis in _GRAIN_AXES)


# HOW a control is narrowed to the workflow's grain — resolved in `_scope_kind` and nowhere else.
GRAIN_COLUMNS = "grain_columns"    # the link target carries the three axes; the client filters on them
ENTITLED_USERS = "entitled_users"  # the target carries no axes; entitlement answers instead


def _entitled_user_options(vertical, group, program, txt, limit):
	"""The users a workflow at this grain may assign to — asked of the ONE entitlement brain.

	The workflow's grain is a RULE grain, so this is the POSSIBILITY question
	(`grain_overlaps_entitlement`), not the actuality question execution asks of a real lead. A blank axis
	means ANY: a workflow with no grain may offer anyone, which the data-grain resolver would have got
	exactly backwards by comparing the blank as an empty string.
	"""
	from tatva_connect.access import entitlement

	return entitlement.users_entitled_to((vertical, group, program), txt=txt, limit=limit)


# A kind's server resolver, or None when the client already has everything it needs to filter.
SCOPE_KINDS = {
	GRAIN_COLUMNS: None,
	ENTITLED_USERS: _entitled_user_options,
}


def _scope_kind(field):
	"""HOW this control is narrowed to the workflow's grain — the ONE decision.

	DERIVED from the link target's own axis columns when it has them, and DECLARED with `scope` when it
	cannot have them. `User` carries no grain axis and never will, so the derivation quietly answered "not
	scoped" and the assignee picker offered every user on the site. Rather than special-case one doctype,
	a field may name a scoping kind, and every kind is resolved here.
	"""
	declared = field.get("scope")
	if declared:
		if declared not in SCOPE_KINDS:
			frappe.throw(
				_("{0} declares an unknown scoping kind {1}.").format(field.get("name"), declared),
				title=_("Unknown scope"),
			)
		return declared
	# The emitted field carries `type`, not `fieldtype` — `fieldtype` is only `_field()`'s parameter name.
	if field.get("type") == "Link" and field.get("link") and _carries_grain(field["link"]):
		return GRAIN_COLUMNS
	return None


def _scoped(field):
	"""Mark a control that is narrowed by the workflow's grain, and say how. Computed per request, not at
	import: the schema is not readable while the module is still loading."""
	kind = _scope_kind(field)
	if kind is None:
		return {**field, "grain_scoped": False} if field.get("type") == "Link" else field
	return {**field, "grain_scoped": True, "scope_kind": kind}


def _wire(field, outputs_rule=None):
	"""One config field as the BUILDER receives it: grain scoping, the vocabulary its control would
	otherwise re-type, WHICH CONTROL to draw, and how a card names its value.

	The control travels with the field so the inspector looks one up instead of carrying its own ladder of
	type names. That ladder was the sixth consumer of the type vocabulary and the only one Python could not
	lock — adding `Button List` meant remembering to edit a `v-if` chain in a .vue file.

	`shapes_outputs` marks the ONE field a type keys its outputs on, so the canvas knows when handles must
	be re-resolved without reading the resolution rule to find out. The inspector used to answer that by
	reaching into `declaration.outputs_by.field` — interpreting the rule to decide when to ask about it.
	"""
	shaped = _value_modes(_scoped(field))
	row = FIELD_TYPES[shaped["type"]]
	return {
		**shaped,
		"control": row["control"],
		"primitive": row["primitive"],
		"summary": row["summary"],
		"shapes_outputs": _shapes_outputs(field, outputs_rule),
		# A vocabulary the TYPE owns rather than any one field — present only where the type declares one.
		**({"units": list(row["units"])} if row.get("units") else {}),
	}


def _shapes_outputs(field, outputs_rule):
	"""A field shapes this node's OWN handles if the outputs rule keys on it: the mode-map `field` (Wait's
	`mode`), or the OWN-config rows a `rows_from` reads (Route's `routes`). A sibling-sourced `rows_from`
	(a Wait reading another node's buttons) shapes handles from the OTHER node, not a field of this one."""
	if not outputs_rule:
		return False
	if field["name"] == outputs_rule.get("field"):
		return True
	rows_from = outputs_rule.get("rows_from") or {}
	return not rows_from.get("node_field") and field["name"] == rows_from.get("declares")


# WHICH CONTROL enters a row's value, per mode — declared, so no frontend decides it by position as `ValueMap.vue` does.
_MODE_CONTROLS = {
	refs.LITERAL: "data",
	refs.FROM_CONTEXT: "value-picker",
	refs.EXPRESSION: "textarea",
	refs.INCREMENT: "number",
}


def _value_modes(field):
	"""A `value_rows` field carries the modes its rows may take — its own, or the default pair.

	Its control has to render a mode switch, and typing `Literal` / `From Context` into the frontend would
	be a second vocabulary for one idea. The runtime compares against `contract.FROM_CONTEXT` itself
	(`contract.resolve_row`), so a drifted spelling would quietly send the literal string
	`crm_lead.first_name` to a patient instead of their name, and nothing would report it.

	A field MAY declare a different list, and W8.1's `Update Field.updates` does: it keeps the Expression
	mode the single-field node always had and adds `Increment by`, neither of which a template slot can
	take. The default is unchanged, so every send verb is untouched by that.

	Imported lazily: `contract` reaches `registry`, which builds its verb node types out of `actions` —
	at module scope this is a cycle.
	"""
	if read_kind_of(field) not in ("value_rows", "value_pair"):
		return field

	from tatva_connect.workflow_engine import contract

	modes = field.get("modes") or [contract.LITERAL, contract.FROM_CONTEXT]
	# A field may name a different control for a mode it fills differently (a Wait's literal instant is a calendar) — declared, so the control that draws it still never decides what a mode means.
	declared = field.get("mode_controls") or {}
	return {**field, "modes": modes,
	        "mode_controls": {m: declared.get(m, _MODE_CONTROLS[m]) for m in modes}}


@frappe.whitelist()
def scoped_options(scope_kind, vertical=None, group=None, program=None, txt=None, limit=20):
	"""The rows a declared-scope control may offer at a workflow's grain. ONE endpoint for every kind.

	A control whose scoping cannot be expressed as a filter on the target doctype's own columns asks here
	instead, naming the kind it declared. Read-only and permission-gated on the same right that gates the
	rest of the authoring contract.
	"""
	if not frappe.has_permission("CRM Workflow", "read"):
		frappe.throw(_("Not permitted"), frappe.PermissionError)
	resolver = SCOPE_KINDS.get(scope_kind)
	if resolver is None:
		frappe.throw(
			_("{0} is not a scoping kind this server resolves.").format(scope_kind), title=_("Unknown scope")
		)
	return resolver(vertical, group, program, txt, frappe.utils.cint(limit) or 20)


@frappe.whitelist()
def node_types(vertical=None):
	"""The palette, the inspector and the validator's shared source, as plain data for the builder.

	`vertical` is the product line the workflow being authored is scoped to. It gates exactly one option
	list — the Trigger's Subject — because a Deal exists only where the line sells; omitted, the blank
	axis is the wildcard and the question becomes "does any line sell", which is the answer a palette with
	no workflow in front of it can honestly give. The authoritative per-grain gate is
	`CRMWorkflow.validate`, which sees the declared vertical and the chosen subject together.

	One endpoint: the frontend hardcodes no node type, no config field and no output name. A type added
	here appears in the palette, renders its own inspector and validates itself, with no frontend change.

	`outputs_by` is deliberately NOT shipped. It is a RESOLUTION RULE, and the canvas re-implemented it the
	whole time it was on the wire. Resolved outputs come from `graph_outputs`, which can see the whole
	graph; a rule handed to a client is an invitation to interpret it, and the invitation was accepted.

	A config field a PROVIDER makes possible is dropped here when no adapter on the node's channel offers
	it — the calling-hours bypass is a Bolna request field, not a control every voice provider has. Asked
	at request time through the same `offered_fields` the verb params use, so the palette and the builder
	can never disagree about whether a field exists.
	"""
	from tatva_connect.channels import resolve

	subjects = subject_options(vertical or "")
	return [
		{
			"type": node_type,
			"label": declared["label"],
			"description": declared["description"],
			"singleton": declared.get("singleton", False),
			"config": [_gated(_wire(f, declared.get("outputs_by")), subjects)
			           for f in resolve.offered_fields(declared["config"], declared.get("channel"))],
			"outputs": declared.get("outputs"),
			"outcomes": outcomes_for(node_type),
		}
		for node_type, declared in NODE_TYPES.items()
	]


def _gated(field, subjects):
	"""The Subject select is the ONE option list decided per REQUEST rather than at import — see `subject_options`."""
	if field.get("name") != "subject_doctype":
		return field
	return {**field, "options": subjects}


def _verb_node_types():
	"""One node type per EFFECT verb — the node IS the verb, and its config IS that verb's parameters.

	There used to be a generic `Step` node holding a list of actions, which meant a node's meaning was
	invisible on the canvas (every box said "Step") and a verb's parameters lived in a 26-column table
	holding the union of every verb's fields. A node that says "Send WhatsApp" and carries exactly the
	settings Send WhatsApp takes is both readable and impossible to misconfigure.

	Guard verbs are deliberately NOT node types: a guard runs inside `validate` to block a save, which is
	declared on the Trigger as a Requirement. A verb either qualifies a save or acts after it, never both.
	"""
	from tatva_connect.automation import actions

	return {
		verb: {
			"label": declared["label"],
			"description": declared["description"],
			# Most verbs simply continue; one that ROUTES on its own result declares its outputs, and the
			# canvas draws a handle per output with no change here.
			"outputs": list(declared.get("outputs") or ["next"]),
			"is_verb": True,
			"config": [_field(**_verb_field(param)) for param in declared["params"]],
			# Which channel this verb sends on, carried so `node_types` can drop a config field no adapter
			# on it supports. Resolved THERE and not here: this runs at import, and resolving a capability
			# imports adapter modules — which this app keeps lazy on purpose.
			"channel": declared.get("outcomes_channel"),
		}
		for verb, declared in actions.VERBS.items()
		if declared["lane"] == "effect"
	}


def _verb_field(param):
	"""A verb parameter in the registry's own field shape — one renderer for config and params alike.

	Everything the verb declared is carried through; only `type` is renamed to `fieldtype`. It used to
	copy a HARDCODED LIST of keys, which meant any declaration not on that list was silently dropped
	between the verb and the canvas — `grain_scoped` was declared, discarded here, and the picker
	therefore offered task types from every grain while the author believed it was scoped. A whitelist
	of keys is a contract that quietly stops carrying whatever is added to it next.
	"""
	shaped = {k: v for k, v in param.items() if k != "type"}
	shaped["fieldtype"] = param["type"]
	return shaped


NODE_TYPES.update(_verb_node_types())
