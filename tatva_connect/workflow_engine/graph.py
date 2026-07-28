# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Whole-graph rules — the release contract, made real.

`registry.validate_node` judges ONE node in isolation: does it carry the settings its type declares, and
are its edges named outputs it actually has. That is necessary and not sufficient. A graph made entirely
of individually-valid nodes can still be nonsense: no Trigger, a Route with one of its legs left unwired,
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
		return [_at(None, _("This workflow has no nodes."), code="graph.empty",
		            fix=_("Add a Trigger node to start the workflow."))]

	# Resolved ONCE and threaded: the Trigger, subject and grain used to be looked up four separate times
	# in this file, and a per-type rule that needed one got hand-written here instead of onto its row.
	context = registry.graph_context(nodes)

	found = []
	found += _node_problems(nodes, context)
	found += _trigger_problems(nodes, entry_node, context)
	found += _edge_problems(nodes, context)
	found += _reachability_problems(nodes, entry_node, context)
	found += _loop_problems(nodes)
	found += _reference_problems(nodes, context)
	found += _wait_problems(nodes)
	found += _template_mapping_problems(nodes, context)
	found += _template_account_problems(nodes, context)
	found += _endpoint_problems(nodes, context)
	return found


def _collision_problems(nodes, context):
	"""A node id may not be the slug of a record the run can reach.

	The namespaced contract makes two values with the same NAME distinguishable, and it does that by making
	the SOURCE unique. A node called `crm_lead` breaks exactly that: `crm_lead.status` would then name both
	the lead's column and whatever that node emitted, and `Values._lookup` asks the node bucket first — so
	the node would silently shadow the subject, which is the collision this whole contract removes. It
	cannot be resolved at runtime and it must not be publishable.

	`actions.reachable_targets` is the ONE answer to what a run can reach, shared with the runtime write
	resolver and with the `Target` row's check in `registry`, which is where the write-target rule moved
	when it stopped being hand-written here.
	"""
	from tatva_connect.automation import actions

	taken = {refs.slug(dt): dt for dt in actions.reachable_targets(context["subject"])}
	return [
		_at(node["node_id"], _("{0} is also the name of the {1} this workflow reads. Rename the node.")
		    .format(node["node_id"], taken[node["node_id"]]),
		    code="node.name-collision", fix=_("Rename the node so it does not shadow a record the run reads."))
		for node in nodes
		if node["node_id"] in taken
	]


def _reference_problems(nodes, context):
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
	found = _collision_problems(nodes, context)
	for node in nodes:
		node_id = node["node_id"]
		if node_id in opaque_after:
			continue
		for ref in contract.reads_of(node["node_type"], _config_of(node)):
			if ref["ref"] in available.get(node_id, set()):
				continue
			found.append(_at(
				node_id,
				_("{0} reads {1}, which nothing before it produces and the subject does not have.")
				.format(ref["label"], ref["ref"]),
				ref["field"],
				code="ref.unresolved",
				fix=_("Pick a value that a node before this one produces, or a field of the subject."),
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
			                 .format(node_id), "event_name", code="wait.no-event",
			                 fix=_("Choose the outcome this Wait resumes on.")))
		source = config.get("source_node")
		if not source:
			# Legitimate: a signal delivered from OUTSIDE the graph carries no node token, and such a Wait
			# answers to any signal of its name for this subject. Only a NAMED source can be unreachable.
			continue
		if source not in by_id:
			found.append(_at(node_id, _("{0} waits on {1}, which is not in this workflow.")
			                 .format(node_id, source), "source_node", code="wait.source-missing",
			                 fix=_("Wait on a node that is in this workflow.")))
			continue
		if source not in upstream_ancestors(nodes, node_id):
			found.append(_at(node_id, _("{0} waits on {1}, which does not always run before it — the run would park for ever.")
			                 .format(node_id, source), "source_node", code="wait.source-unreachable",
			                 fix=_("Wait on a node that always runs before this one.")))
			continue
		outcome = config.get("event_name")
		emits = registry.outcomes_for(by_id[source]["node_type"])
		if outcome and outcome not in emits:
			found.append(_at(node_id, _("{0} never reports {1}. It reports: {2}")
			                 .format(source, outcome, ", ".join(emits) or _("nothing")), "event_name",
			                 code="wait.outcome-unknown", fix=_("Pick an outcome this node can actually report.")))
	return found


def _template_mapping_problems(nodes, context):
	"""Every placeholder a picked template declares has a mapping row. This RAISED mid-run in
	`sends._template_parameters`, days after the author left; now refused at publish. The placeholder names
	come from the SAME adapter the send-time builder reads, and the missing set from the SAME
	`sends.missing_value_rows` — one rule, two callers, never a re-implementation.
	"""
	from tatva_connect.automation import sends

	slots_by_verb = {
		"Send WhatsApp": ("whatsapp_template", sends.whatsapp_template_slots),
		"Send Email": ("email_template", sends.email_template_slots),
	}
	found = []
	for node in nodes:
		spec = slots_by_verb.get(node["node_type"])
		if not spec:
			continue
		template_field, slots_of = spec
		config = _config_of(node)
		template = config.get(template_field)
		if not template:
			continue  # a blank template is the `reqd` rule's business (W2.2), not this move's
		for name in sends.missing_value_rows(slots_of(template), config.get("template_values")):
			found.append(_at(
				node["node_id"],
				_("{0} has no value declared for {1} — every placeholder needs a row.").format(template, name),
				"template_values", code="template.slot-unmapped",
				fix=_("Map {0}, or the message would go out with a blank in it.").format(name),
			))
	return found


def _template_account_problems(nodes, context):
	"""A picked WhatsApp template must belong to the account the workflow's grain routes to. This RAISED in
	`sends.send_whatsapp`; now refused at publish — but ONLY when the grain pins one account. A blank or
	ambiguous grain resolves per-lead at runtime, so this ABSTAINS and the runtime backstop remains. Both
	sides call the ONE `sends.template_account_mismatch`."""
	from tatva_connect.automation import sends
	from tatva_connect.whatsapp import routing

	grain = context["grain"]
	account = routing.resolve_account_for_grain(grain["vertical"], grain["group"], grain["program"])
	if not account:
		return []  # the grain pins no single account — a per-lead runtime question, not author error
	found = []
	for node in nodes:
		if node["node_type"] != "Send WhatsApp":
			continue
		template = _config_of(node).get("whatsapp_template")
		if not template or not frappe.db.exists("WhatsApp Templates", template):
			continue  # a blank or dangling template link is a different rule's business
		mismatch = sends.template_account_mismatch(template, account)
		if mismatch:
			found.append(_at(
				node["node_id"], mismatch, "whatsapp_template", code="template.account-mismatch",
				fix=_("Pick a template that belongs to {0}, the account this workflow's grain routes to.").format(account),
			))
	return found


def _endpoint_problems(nodes, context):
	"""A Call API node's curated Webhook must still exist. This RAISED in `_action_call_api` on the first
	run that reached it; now refused at publish. A blank endpoint is the `reqd` rule's business, so this
	abstains on it and speaks only to an endpoint that was picked and has since been deleted."""
	found = []
	for node in nodes:
		if node["node_type"] != "Call API":
			continue
		endpoint = _config_of(node).get("webhook_endpoint")
		if endpoint and not frappe.db.exists("Webhook", endpoint):
			found.append(_at(
				node["node_id"], _("The endpoint {0} does not exist.").format(endpoint),
				"webhook_endpoint", code="endpoint.missing", fix=_("Pick a curated Webhook that exists."),
			))
	return found


def upstream_ancestors(nodes, node_id):
	"""The nodes that certainly run before this one — the same walk `upstream.emitters_at` offers from.

	This sentence claimed to describe the authoring picker while being false: the picker filtered the raw
	graph in JS and offered a Wait its own descendants, which this gate then refused. `emitters_at` now
	answers that picker off `upstream._ancestors`, so the two really are one walk.
	"""
	return set(upstream._ancestors(_by_id(nodes), node_id))


def _node_problems(nodes, context):
	"""Each node, judged for COMPLETENESS — the half `validate_node` defers while authoring.

	A node save checks shape only, so an author can put a Route on the canvas and configure it later.
	Publish is where "later" runs out: this is the same validator, in the same one place, asked the
	stricter question. Without it, moving the check out of save would have deleted it rather than moved
	it, and a workflow with an unconfigured Route would activate and then die on a real lead.
	"""
	found = []
	for node in nodes:
		outputs = [e["from_output"] for e in _edges_of(node)]
		found += [
			{"node_id": node["node_id"], **p}
			for p in registry.validate_node(
				node["node_type"], _config_of(node), outputs, mode=registry.PUBLISH, graph_context=context,
			)
		]
	return found


def _config_of(node):
	"""Delegates: both node shapes are `registry.config_of`'s business, and knowing it twice is how the
	graph context and this file disagreed about what a node was configured with."""
	return registry.config_of(node)


def _at(node_id, message, field=None, code=None, severity=registry.BLOCKS, fix=None):
	"""A graph-level fault, tagged with the node it belongs to (or None when it belongs to the graph).

	Adds `node_id` to the ONE problem shape — it does NOT build a second dict, so the Bouncer and the node
	validator speak one vocabulary. `code`/`severity`/`fix` are the call site's own: the rule that catches
	the fault is the authority on how bad it is and what to do about it.
	"""
	return {"node_id": node_id, **registry.problem(message, field, code=code, severity=severity, fix=fix)}


def _by_id(nodes):
	return {n["node_id"]: n for n in nodes if n.get("node_id")}


def _edges_of(node):
	return [e for e in (node.get("edges") or []) if e.get("to_node")]


def _trigger_problems(nodes, entry_node, context):
	"""Exactly one Trigger, and it is where runs begin.

	The entry used to fall back to "the first node by sequence" when `entry_node` was unset, so a graph
	whose earliest-authored node was a Create Task started THERE and skipped the Trigger — silently
	running a workflow that had never qualified its subject.
	"""
	triggers = context["triggers"]
	if not triggers:
		return [_at(None, _("This workflow has no Trigger, so nothing would ever start it."),
		            code="trigger.missing", fix=_("Add a Trigger node."))]
	if len(triggers) > 1:
		return [_at(None, _("A workflow may have only one Trigger; this one has {0}.").format(len(triggers)),
		            code="trigger.duplicate", fix=_("Keep exactly one Trigger."))]
	if entry_node and entry_node != triggers[0]["node_id"]:
		return [_at(entry_node, _("Runs must begin at the Trigger, not at {0}.").format(entry_node),
		            code="trigger.entry", fix=_("Make the Trigger the entry node."))]
	return _schedule_problems(triggers[0])


def _schedule_problems(trigger):
	"""A scheduled Trigger names a schedule that can actually be read.

	Unreadable, the workflow publishes green and simply never fires — no error, no run, no clue, and the
	author finds out when the month's cohort does not go out. `registry.SCHEDULES` is the one vocabulary,
	so a value outside it is refused here rather than silently answering `None` at `cohort.next_run_at`.
	"""
	config = _config_of(trigger)
	if config.get("mode") != registry.MODE_SCHEDULE:
		return []
	if config.get("schedule") in registry.SCHEDULES:
		return []
	return [_at(trigger["node_id"],
	            _("This Trigger runs on a schedule but does not say how often, so it would never fire."),
	            code="trigger.schedule.missing", field="schedule",
	            fix=_("Choose how often the cohort repeats."))]


def _edge_problems(nodes, context):
	"""Every edge lands on a node that exists, and every declared output is wired.

	An unwired output is the quiet one: a Route with a leg left unconnected runs fine until the day a
	subject takes that path, and then dies with "node None is not in the frozen graph".
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
					code=registry.CODE_NODE_NOT_IN_GRAPH,
					fix=_("Point this edge at a node that exists, or delete it."),
				))

		for output in registry.outputs_for(node["node_type"], config, context["configs"]):
			if output not in wired:
				found.append(_at(
					node["node_id"],
					_("{0} has nothing connected to its {1} output.").format(node["node_id"], output),
					code="output.unwired",
					fix=_("Connect this output to a node."),
				))
	return found


def _reachability_problems(nodes, entry_node, context):
	"""Every node is reachable from the start, and the graph can actually end.

	An unreachable node is usually a leftover the author forgot to delete, and it is worth saying so —
	it will never run, and a reader of the canvas cannot tell that by looking.
	"""
	known = _by_id(nodes)
	start = entry_node or (context["trigger"] or {}).get("node_id")
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
			found.append(_at(node["node_id"], _("{0} cannot be reached from the Trigger, so it would never run.").format(node["node_id"]),
			                 code="node.unreachable", fix=_("Wire this node into the graph, or delete it.")))

	if not any(known[n]["node_type"] == "Terminal" for n in seen):
		found.append(_at(None, _("No End node can be reached, so a run would never finish."),
		                 code="graph.no-terminal", fix=_("Add an End node the graph can reach.")))
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
				), code="loop.no-wait", fix=_("Put a Wait on the loop, or break the cycle.")))
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
