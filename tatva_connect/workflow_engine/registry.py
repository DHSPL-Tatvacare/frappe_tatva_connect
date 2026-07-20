# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The node-type registry — ONE declaration per node type, read by everything.

A node type is declared here and nowhere else. The palette lists what this file declares, the inspector
renders the config fields it declares, the validator enforces them, and the canvas draws exactly the
outputs it declares. Adding a node type is an entry in `NODE_TYPES`; it is never a schema migration, a
frontend switch statement, or a new branch in the interpreter.

WHY A REGISTRY AND NOT COLUMNS
------------------------------
The old model gave every node type its own columns — a Branch's `condition`, a Wait's `wait_mode` /
`signal_name` / `accepts_json`, and five fixed edge columns — so every OTHER node type carried them
empty, the frontend hardcoded which fields to show for which type, and the canvas hardcoded which
handles to draw. Three copies of one fact, and adding a type meant editing all three.

Now: a node holds `config_json`, and this file says what belongs in it.

OUTPUTS ARE PART OF THE CONTRACT
--------------------------------
A node type declares the names of the edges that may leave it. `Branch` declares `true` and `false`;
`Terminal` declares none. A Wait's outputs depend on its mode — waiting only on an event has no timeout
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


def _subject_options():
	"""The doctypes a workflow may watch — read from `automation.subjects.SUBJECTS`, the ONE resolver.

	Offering anything else would let an author pick a subject the engine cannot resolve to a lead, and
	the workflow would then be silently dead: it would match on save, fail to resolve a subject, and
	return without a trace. A wrong pick is impossible instead of merely discouraged.
	"""
	from tatva_connect.automation.subjects import SUBJECTS

	return sorted(SUBJECTS)


def _guard_verbs():
	"""The verbs that may be declared as Requirements — read from the automation engine's ONE lane table.

	A requirement runs SYNCHRONOUSLY inside validate and blocks the save by raising, so only a verb the
	lane table marks `guard` belongs here. Reading the table rather than listing the verbs means a guard
	added there is offerable the same day, and an effect verb can never be declared as a requirement.
	"""
	from tatva_connect.automation import actions

	return sorted(actions.verbs_in_lane("guard"))


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
		"description": "What starts this workflow: the subject it watches, the event, the grain it applies to, and the conditions a subject must meet. Exactly one per workflow, and the only node with no inbound edge.",
		"outputs": ["next"],
		"singleton": True,
		"config": [
			_field("subject_doctype", "Subject", "Select", options=_subject_options(), reqd=True),
			_field("event", "Event", "Select", options=["Created", "Updated", "Deleted"], reqd=True),
			_field("vertical", "Vertical", "Grain", link="CRM Vertical"),
			_field("group", "Group", "Grain", link="CRM Group"),
			_field("program", "Program", "Grain", link="CRM Program"),
			_field("predicate", "Only when", "Predicate"),
			_field("requirements", "Requirements", "Requirements", verbs=_guard_verbs()),
		],
	},
	"Branch": {
		"label": "Branch",
		"description": "Routes on a predicate. Every branch is explicit — there is no implicit fallthrough.",
		"outputs": ["true", "false"],
		"config": [_field("condition", "Condition", "Predicate", reqd=True)],
	},
	"Set Variables": {
		"label": "Set Variables",
		"description": "Computes values into the run's state for later nodes to read. Nothing to do with people — to change who owns a lead, use Assign to User.",
		"outputs": ["next"],
		"config": [_field("assign", "Values", "Code", options="JSON", reqd=True,
		                  reads="expression", writes="expression_dict")],
	},
	"Wait": {
		"label": "Wait",
		"description": "Suspends the run until an event arrives, a clock expires, or whichever comes first.",
		# Outputs depend on the mode: waiting only on an event has no timeout edge to draw or validate.
		"outputs_by": {
			"field": "mode",
			"map": {
				UNTIL_EVENT: ["event"],
				FOR_DURATION: ["next"],
				UNTIL_TIME: ["next"],
				EVENT_OR_TIMEOUT: ["event", "timeout"],
			},
		},
		"config": [
			_field("mode", "Mode", "Select",
			       options=[UNTIL_EVENT, FOR_DURATION, UNTIL_TIME, EVENT_OR_TIMEOUT], reqd=True),
			_field("source_node", "Waiting on", "Node",
			       depends_on_value={"mode": [UNTIL_EVENT, EVENT_OR_TIMEOUT]}),
			_field("event_name", "Outcome", "Outcome",
			       depends_on_value={"mode": [UNTIL_EVENT, EVENT_OR_TIMEOUT]}),
			# {dotted payload path: state key} — the VALUES are what this Wait writes, hence `payload_map`.
			_field("accepts", "Accepts", "Code", options="JSON", writes="payload_map",
			       depends_on_value={"mode": [UNTIL_EVENT, EVENT_OR_TIMEOUT]}),
			_field("expression", "Duration or time", "Data", reads="expression",
			       depends_on_value={"mode": [FOR_DURATION, UNTIL_TIME, EVENT_OR_TIMEOUT]}),
		],
	},
	"Terminal": {
		"label": "End",
		"description": "Ends the run. Declares no outputs, so the canvas draws no handle to drag from.",
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
	free-text event name was the old shape, and a typo there parked a run for ever with nothing able to
	wake it: unwakeable, indistinguishable from a run that is legitimately still waiting.
	"""
	from tatva_connect.automation import actions

	return actions.outcomes_of(node_type)


def outputs_for(node_type, config=None):
	"""The edge names that may leave this node, given its config.

	The single answer to "what can leave here", used by the validator to reject an edge nobody declared
	and by the canvas to draw handles. A conditional declaration resolves against the config field it
	names; an unset or unknown value yields no outputs rather than guessing one.
	"""
	declared = declaration(node_type)
	if "outputs" in declared:
		return list(declared["outputs"])
	rule = declared["outputs_by"]
	return list(rule["map"].get((config or {}).get(rule["field"]), []))


def config_fields(node_type):
	"""The config fields this node type declares, for the inspector and the validator."""
	return list(declaration(node_type)["config"])


def problem(message, field=None):
	"""One authoring fault, as DATA. The canvas needs to know WHICH field on WHICH node is wrong so it can
	mark it; a sentence can only be shown in a toast, and a toast naming `b1` in a graph of twenty nodes
	is barely better than silence. `node_id` is added by the caller, which is the only layer that knows it.
	"""
	return {"field": field, "message": message}


# When a rule is enforced. A node is authored over many saves, so SHAPE is checked every time and
# COMPLETENESS only when the author says the graph is finished. Enforcing completeness at save makes the
# canvas unusable: dragging a Branch on and saving before configuring it is ordinary work, not an error,
# and a node type that cannot yet be configured becomes permanently unsaveable.
DRAFT, PUBLISH = "draft", "publish"


def validate_node(node_type, config, edge_outputs, mode=PUBLISH):
	"""Every rule a node must satisfy, in one place. Returns a list of `problem()` records.

	Returns rather than throws so one save can report every problem at once — a builder that surfaces
	them one per attempt makes the author fix a five-field node in five round trips.

	`mode=DRAFT` defers the "this setting is required" rule and nothing else. A setting the type never
	declared, a value outside its options, a malformed predicate or an edge on an undeclared output are
	all wrong whatever the author intends next, so they are refused at every save.
	"""
	problems = []
	completeness = mode == PUBLISH
	declared = declaration(node_type)
	config = config or {}

	known = {f["name"] for f in declared["config"]}
	for name in sorted(set(config) - known):
		problems.append(problem(_("{0} does not take a setting called {1}.").format(node_type, name), name))

	for field in declared["config"]:
		if field.get("reqd") and not _applies(field, config):
			continue
		if completeness and field.get("reqd") and not config.get(field["name"]):
			problems.append(problem(_("{0} needs {1}.").format(node_type, field["label"]), field["name"]))
		value = config.get(field["name"])
		if field.get("writes"):
			problems.extend(problem(m, field["name"]) for m in _written_name_problems(node_type, config, field))
		if field["type"] == "Requirements":
			problems.extend(problem(m, field["name"]) for m in _requirement_problems(value, field))
			continue  # a list of verbs, not a scalar — the generic option check below does not apply
		if field["type"] == "Target":
			continue  # a real doctype name, offered from the graph — no static option list to check against
		if field["type"] == "Mapping":
			problems.extend(problem(m, field["name"]) for m in _variable_problems(value, field))
			continue  # a list of {path, variable} rows, not a scalar
		if field["type"] == "Predicate":
			problems.extend(problem(m, field["name"]) for m in _predicate_problems(value, field))
			continue  # a tree, not a scalar
		options = field.get("options")
		if value and isinstance(options, list) and value not in options:
			problems.append(problem(
				_("{0} is not a valid {1}. Choose one of: {2}").format(value, field["label"], ", ".join(options)),
				field["name"],
			))

	allowed = set(outputs_for(node_type, config))
	for output in sorted(set(edge_outputs or []) - allowed):
		problems.append(problem(
			_("{0} declares no output called {1}.").format(node_type, output)
			if allowed else _("{0} has no outgoing edges.").format(node_type)
		))
	return problems


def _predicate_problems(value, field):
	"""Structural rules for a predicate tree. Empty is fine — an empty gate is open.

	Shape only: whether a rule's FIELD exists is a question about the subject, and the subject is chosen
	on the same node, so it is answered by the evaluator against a real context rather than guessed here.
	What this catches is the malformed tree — an unknown node type, a `not` with two children, a rule
	with no operator — none of which can be right for any subject.
	"""
	if not value:
		return []
	try:
		_walk_predicate(value, field["label"])
	except ValueError as bad:
		return [str(bad)]
	return []


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
# in the SAME flat dict as the run's variables under four bare names, so a capture called `_emitted`
# replaced the map the wake depends on and the run parked for ever with nothing able to reach it. It now
# lives under `_engine.*` and an author's value lives under its NODE, so the collision is gone
# structurally. This stays because the two namespaces must not be confusable by sight either.
RESERVED_SOURCE = refs.ENGINE

# The names an author may not take, spelled ONCE — in `refs`, where the engine's namespace is declared.
# It used to be four bare keys because the engine shared one flat dict with the run's variables; there is
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


def _variable_problems(value, field):
	"""Every rule a captured variable name must satisfy."""
	return _reserved_problems([
		(row or {}).get("variable") for row in (value or []) if isinstance(row, dict)
	])


def _written_name_problems(node_type, config, field):
	"""Reserved names, asked of a field that WRITES run state rather than one that captures into it.

	`Set Variables.assign` and `Wait.accepts` are `Code`, not `Mapping`, so the reserved-name rule never
	reached them: `{"_emitted": "x"}` published green, `state.update` then replaced the correlation map
	the wake depends on, and the run parked for ever. For `accepts` this was reachable by anyone who can
	edit the subject, since `signals.deliver_signal` routes an external payload through that map.

	The keys are read through `upstream.write_fields_of` — the same enumerator the run's own available-
	values walk uses — so what is refused here is exactly what the run would really merge. A field whose
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


def _requirement_problems(value, field):
	"""Every rule a Requirements value must satisfy. Empty is fine — a workflow may demand nothing."""
	if not value:
		return []
	if not isinstance(value, list):
		return [_("{0} must be a list of requirements.").format(field["label"])]
	problems = []
	allowed = field.get("verbs") or []
	for entry in value:
		if not isinstance(entry, dict) or not entry.get("verb"):
			problems.append(_("Every requirement needs a verb."))
			continue
		if entry["verb"] not in allowed:
			problems.append(
				_("{0} cannot be a requirement — a requirement must block the save. Choose one of: {1}")
				.format(entry["verb"], ", ".join(allowed))
			)
	return problems


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
	picker looks exactly like a scoped one until a run is refused at execution.
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


def _wire(field):
	"""One config field as the BUILDER receives it: grain scoping, plus any vocabulary its control would
	otherwise have to re-type. Computed per request for the same reason `_scoped` is."""
	return _value_modes(_scoped(field))


def _value_modes(field):
	"""A `value_rows` field carries the two modes its rows may take.

	Its control has to render a mode switch, and typing `Literal` / `From Context` into the frontend would
	be a second vocabulary for one idea. The runtime compares against `contract.FROM_CONTEXT` itself
	(`sends._template_parameters`), so a drifted spelling would quietly send the literal string
	`crm_lead.first_name` to a patient instead of their name, and nothing would report it.

	Imported lazily: `contract` reaches `registry`, which builds its verb node types out of `actions` —
	at module scope this is a cycle.
	"""
	if field.get("reads") != "value_rows":
		return field

	from tatva_connect.workflow_engine import contract

	return {**field, "modes": [contract.LITERAL, contract.FROM_CONTEXT]}


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
def node_types():
	"""The palette, the inspector and the validator's shared source, as plain data for the builder.

	One endpoint: the frontend hardcodes no node type, no config field and no output name. A type added
	here appears in the palette, renders its own inspector and validates itself, with no frontend change.
	"""
	return [
		{
			"type": node_type,
			"label": declared["label"],
			"description": declared["description"],
			"singleton": declared.get("singleton", False),
			"config": [_wire(f) for f in declared["config"]],
			"outputs": declared.get("outputs"),
			"outputs_by": declared.get("outputs_by"),
			"outcomes": outcomes_for(node_type),
		}
		for node_type, declared in NODE_TYPES.items()
	]


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
