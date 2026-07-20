# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Whole-graph rules — the release contract, made real.

`registry.validate_node` judges ONE node in isolation: does it carry the settings its type declares, and
are its edges named outputs it actually has. That is necessary and not sufficient. A graph made entirely
of individually-valid nodes can still be nonsense: no Trigger, a Branch with only its `true` leg wired,
an edge pointing at a node someone deleted, a loop with no Wait in it, a Terminal nothing reaches.

Every one of those used to publish and activate cleanly, then die on a live lead as `_Permanent` — the
author discovering it from a failed run days later, on a real patient's record. The docstrings on
`campaigns.api.publish` and `crm_workflow.apply_transition` described this contract as though it existed.
It did not. This module is that contract.

RETURNS problems, never throws. One publish should tell an author everything wrong with their graph, not
make them fix it one round trip at a time — the same reason `validate_node` returns a list.

Each problem is `{node_id, field, message}`. `node_id` is None for a fault that belongs to the graph
rather than to any one node ("this workflow has no Trigger"). The canvas marks the named node; a bare
sentence could only be toasted.
"""
import frappe
from frappe import _

from tatva_connect.workflow_engine import contract, refs, registry, upstream

# A graph may loop, but only through a Wait. A loop with no Wait in it spins the interpreter until the
# hop budget kills the run, which is a hang the author cannot see coming.
_SUSPENDS = "Wait"

# The Wait modes that park on an outcome, from the registry's own vocabulary — never re-spelled here.
_WAITS_ON_EVENT = (registry.UNTIL_EVENT, registry.EVENT_OR_TIMEOUT)


def problems(nodes, entry_node=None):
	"""Every whole-graph fault, as human sentences. `nodes` is the authored graph:
	`[{node_id, node_type, edges: [{from_output, to_node}], config}]`."""
	if not nodes:
		return [_at(None, _("This workflow has no nodes."))]

	found = []
	found += _node_problems(nodes)
	found += _trigger_problems(nodes, entry_node)
	found += _edge_problems(nodes)
	found += _reachability_problems(nodes, entry_node)
	found += _loop_problems(nodes)
	found += _reference_problems(nodes)
	found += _wait_problems(nodes)
	found += _write_target_problems(nodes)
	return found


def _write_target_problems(nodes):
	"""A write must aim at a record the run can reach, and at a field the operator allowed automation to set.

	Both were enforced only at EXECUTION. A workflow naming a misspelt field, or a doctype that is neither
	the subject nor the lead, published green and then died `_Permanent` on the first live record — the
	author finding out days later, from a real patient's lead.

	Two rules, read from declarations rather than a list of verbs, so a second verb taking a `Target` or a
	`Field` is covered the day it is added:

	  • a `Target` value must be the Lead or the subject the Trigger watches. `_resolve_write_target`
	    raises for anything else, and the trigger doc's doctype IS the subject doctype.
	  • a `Field` value must be a can_set field of that target — MEMBERSHIP ONLY, via
	    `fields.is_set_declared`. The grain-specific decision deliberately stays at execution: the
	    workflow's declared grain is a RULE grain whose blank axis means ANY, and handing that to
	    `is_settable` (which expects a lead's DATA grain) is the exact defect this gate must not commit.

	A node whose target is already reported is not asked the field question too — one fault, one message.
	"""
	from tatva_connect.automation import actions, fields

	subject = next(
		(_config_of(n).get("subject_doctype") for n in nodes if n["node_type"] == registry.TRIGGER), None
	)
	reachable = set(actions.reachable_targets(subject))  # the ONE answer, shared with runtime + authoring

	found = []
	for node in nodes:
		config = _config_of(node)
		declared = registry.config_fields(node["node_type"])

		for field in declared:
			value = config.get(field["name"])
			if field["type"] == "Target" and value and value not in reachable:
				found.append(_at(node["node_id"], _("{0} writes to {1}, which this workflow never touches. It can write to: {2}")
				                 .format(node["node_id"], value, ", ".join(sorted(reachable))), field["name"]))

		for field in declared:
			value = config.get(field["name"])
			if field["type"] != "Field" or not value:
				continue
			target = config.get(field.get("doctype_from") or "")
			if target and target in reachable and not fields.is_set_declared(target, value):
				found.append(_at(node["node_id"], _("{0} is not a field automation is allowed to set on {1}.")
				                 .format(value, target), field["name"]))
	return found


def _collision_problems(nodes):
	"""A node id may not be the slug of a record the run can reach.

	The namespaced contract makes two values with the same NAME distinguishable, and it does that by making
	the SOURCE unique. A node called `crm_lead` breaks exactly that: `crm_lead.status` would then name both
	the lead's column and whatever that node emitted, and `Values._lookup` asks the node bucket first — so
	the node would silently shadow the subject, which is the collision this whole contract removes. It
	cannot be resolved at runtime and it must not be publishable.

	`actions.reachable_targets` is the ONE answer to what a run can reach, shared with the runtime write
	resolver and with `_write_target_problems` above.
	"""
	from tatva_connect.automation import actions

	subject = next(
		(_config_of(n).get("subject_doctype") for n in nodes if n["node_type"] == registry.TRIGGER), None
	)
	taken = {refs.slug(dt): dt for dt in actions.reachable_targets(subject)}
	return [
		_at(node["node_id"], _("{0} is also the name of the {1} this workflow reads. Rename the node.")
		    .format(node["node_id"], taken[node["node_id"]]))
		for node in nodes
		if node["node_id"] in taken
	]


def _reference_problems(nodes):
	"""Every value a node reads is one something upstream actually produces.

	The half of the node contract that was declared and never enforced. `emits` said what each verb
	writes and `upstream` turned that into what each node may read, but the gate never asked the
	question — so a misspelled variable published green and then either killed the run on a live record
	(a predicate raises) or did nothing at all and said nothing (`Variable` reads resolve to None: the
	assignee silently becomes `nobody`, the due date silently becomes the default, the written value
	silently becomes None).

	A node downstream of a Set Variables whose keys cannot be enumerated is SKIPPED. Absence cannot be
	proven there, and a false "no upstream node produces x" would block a workflow that is correct —
	which would teach authors to distrust the check.

	Every reference compared here is `<source>.<field>`, which is what makes the comparison meaningful at
	all: a bare `status` matched whichever `status` happened to be in the bag first, so the gate blessed a
	reference that resolved to the wrong value at runtime and reported nothing.
	"""
	available, opaque_after = upstream.available_map(nodes)
	found = _collision_problems(nodes)
	for node in nodes:
		node_id = node["node_id"]
		if node_id in opaque_after:
			continue
		for ref in contract.reads_of(node["node_type"], _config_of(node)):
			if ref["name"] in available.get(node_id, set()):
				continue
			found.append(_at(
				node_id,
				_("{0} reads {1}, which nothing before it produces and the subject does not have.")
				.format(ref["label"], ref["name"]),
				ref["field"],
			))
	return found


def _wait_problems(nodes):
	"""A Wait on an outcome must name a node that will really have run, and really emits that outcome.

	Get this wrong and the run parks FOR EVER, invisibly: the correlation token comes from
	`_emitted[source_node]`, a node that never ran never minted one, and the null that results is matched
	only against rows whose correlation is empty — which no real signal ever is. `Until Event` sets no
	`resume_at`, so no timer sweep will ever select the row either. The run sits Parked with no error, no
	step log and no clock, and the lead simply never gets its next task.

	Author time is the only place this is catchable. At runtime it looks exactly like a run that is
	legitimately still waiting.
	"""
	by_id = _by_id(nodes)
	found = []
	for node in nodes:
		if node["node_type"] != _SUSPENDS:
			continue
		config = _config_of(node)
		if config.get("mode") not in _WAITS_ON_EVENT:
			continue
		node_id = node["node_id"]
		if not config.get("event_name"):
			found.append(_at(node_id, _("{0} waits on an outcome but does not say which one.")
			                 .format(node_id), "event_name"))
		source = config.get("source_node")
		if not source:
			# Legitimate: a signal delivered from OUTSIDE the graph carries no node token, and such a Wait
			# answers to any signal of its name for this subject. Only a NAMED source can be unreachable.
			continue
		if source not in by_id:
			found.append(_at(node_id, _("{0} waits on {1}, which is not in this workflow.")
			                 .format(node_id, source), "source_node"))
			continue
		if source not in upstream_ancestors(nodes, node_id):
			found.append(_at(node_id, _("{0} waits on {1}, which does not always run before it — the run would park for ever.")
			                 .format(node_id, source), "source_node"))
			continue
		outcome = config.get("event_name")
		emits = registry.outcomes_for(by_id[source]["node_type"])
		if outcome and outcome not in emits:
			found.append(_at(node_id, _("{0} never reports {1}. It reports: {2}")
			                 .format(source, outcome, ", ".join(emits) or _("nothing")), "event_name"))
	return found


def upstream_ancestors(nodes, node_id):
	"""The nodes that certainly run before this one — the same walk the authoring picker uses."""
	return set(upstream._ancestors(_by_id(nodes), node_id))


def _node_problems(nodes):
	"""Each node, judged for COMPLETENESS — the half `validate_node` defers while authoring.

	A node save checks shape only, so an author can put a Branch on the canvas and configure it later.
	Publish is where "later" runs out: this is the same validator, in the same one place, asked the
	stricter question. Without it, moving the check out of save would have deleted it rather than moved
	it, and a workflow with an unconfigured Branch would activate and then die on a real lead.
	"""
	found = []
	for node in nodes:
		config = _config_of(node)
		outputs = [e["from_output"] for e in _edges_of(node)]
		found += [
			{"node_id": node["node_id"], **p}
			for p in registry.validate_node(node["node_type"], config, outputs, mode=registry.PUBLISH)
		]
	return found


def _config_of(node):
	"""A node arrives either as an authored row (`config_json` text) or as a test's plain `config`."""
	if "config_json" in node:
		return frappe.parse_json(node.get("config_json") or "{}") or {}
	return node.get("config") or {}


def _at(node_id, message, field=None):
	"""A graph-level fault, tagged with the node it belongs to (or None when it belongs to the graph)."""
	return {"node_id": node_id, "field": field, "message": message}


def _by_id(nodes):
	return {n["node_id"]: n for n in nodes if n.get("node_id")}


def _edges_of(node):
	return [e for e in (node.get("edges") or []) if e.get("to_node")]


def _trigger_problems(nodes, entry_node):
	"""Exactly one Trigger, and it is where runs begin.

	The entry used to fall back to "the first node by sequence" when `entry_node` was unset, so a graph
	whose earliest-authored node was a Create Task started THERE and skipped the Trigger — silently
	running a workflow that had never qualified its subject.
	"""
	triggers = [n for n in nodes if n["node_type"] == registry.TRIGGER]
	if not triggers:
		return [_at(None, _("This workflow has no Trigger, so nothing would ever start it."))]
	if len(triggers) > 1:
		return [_at(None, _("A workflow may have only one Trigger; this one has {0}.").format(len(triggers)))]
	if entry_node and entry_node != triggers[0]["node_id"]:
		return [_at(entry_node, _("Runs must begin at the Trigger, not at {0}.").format(entry_node))]
	return []


def _edge_problems(nodes):
	"""Every edge lands on a node that exists, and every declared output is wired.

	An unwired output is the quiet one: a Branch with only `true` connected runs fine until the day a
	subject takes the false path, and then dies with "node None is not in the frozen graph".
	"""
	known = _by_id(nodes)
	found = []
	for node in nodes:
		config = _config_of(node)
		wired = {e["from_output"] for e in _edges_of(node)}

		for edge in _edges_of(node):
			if edge["to_node"] not in known:
				found.append(_at(
					node["node_id"],
					_("{0} points at {1}, which is not in this workflow.").format(node["node_id"], edge["to_node"]),
				))

		for output in registry.outputs_for(node["node_type"], config):
			if output not in wired:
				found.append(_at(
					node["node_id"],
					_("{0} has nothing connected to its {1} output.").format(node["node_id"], output),
				))
	return found


def _reachability_problems(nodes, entry_node):
	"""Every node is reachable from the start, and the graph can actually end.

	An unreachable node is usually a leftover the author forgot to delete, and it is worth saying so —
	it will never run, and a reader of the canvas cannot tell that by looking.
	"""
	known = _by_id(nodes)
	start = entry_node or next((n["node_id"] for n in nodes if n["node_type"] == registry.TRIGGER), None)
	if not start or start not in known:
		return []  # already reported by _trigger_problems

	# Ordinary breadth-first walk from the start.
	seen, queue = {start}, [start]
	while queue:
		node = known[queue.pop()]
		for edge in _edges_of(node):
			if edge["to_node"] in known and edge["to_node"] not in seen:
				seen.add(edge["to_node"])
				queue.append(edge["to_node"])

	found = []
	for node in nodes:
		if node["node_id"] not in seen:
			found.append(_at(node["node_id"], _("{0} cannot be reached from the Trigger, so it would never run.").format(node["node_id"])))

	if not any(known[n]["node_type"] == "Terminal" for n in seen):
		found.append(_at(None, _("No End node can be reached, so a run would never finish.")))
	return found


def _loop_problems(nodes):
	"""A cycle is legal only if a Wait sits on it.

	Without one the interpreter walks the loop as fast as it can until the hop budget stops it — the run
	fails, having done whatever its nodes do, over and over, on the way there.
	"""
	known = _by_id(nodes)
	found, walking, done = [], set(), set()

	def walk(node_id, path):
		if node_id in walking:
			ring = path[path.index(node_id):]
			if not any(known[n]["node_type"] == _SUSPENDS for n in ring):
				found.append(_at(node_id, _("{0} loops back on itself with no Wait in between, so a run would spin.").format(
					" → ".join(ring)
				)))
			return
		if node_id in done:
			return
		walking.add(node_id)
		for edge in _edges_of(known[node_id]):
			if edge["to_node"] in known:
				walk(edge["to_node"], [*path, edge["to_node"]])
		walking.discard(node_id)
		done.add(node_id)

	for node in nodes:
		if node["node_id"] not in done:
			walk(node["node_id"], [node["node_id"]])
	return found
