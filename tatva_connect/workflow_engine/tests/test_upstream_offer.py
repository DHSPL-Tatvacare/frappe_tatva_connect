# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""DATA AND CONFIG FLOW TOP TO BOTTOM. What an upstream node emits is offerable; nothing else is.

`node_context` already answered that for VALUES — `variables` comes from `upstream.available_at`, which
walks ancestors. It did not answer it for NODES, so the inspector built its own offer:

    props.graph.filter((n) => n.node_id !== props.node.node_id && declarationFor(n.node_type)?.outcomes)

Not-self, and can-emit. No position at all. A Wait was therefore offered every emitting node in the
graph INCLUDING ITS OWN DESCENDANTS — measured in the browser on 2026-07-22, where `wait-1` offered
`send-whatsapp-1`, the node it blocks.

Publish already refuses that graph (`graph._wait_problems` — "does not always run before it, the journey
would park for ever"), so this was never a live outage. It is an authoring surface inviting a mistake
the gate then rejects, and it is C17.1 exactly: the canvas re-deciding what the backend already answers.

THE FIX IS ONE ANSWER, NOT A SECOND FILTER. `upstream.emitters_at` walks the same `_ancestors` the value
picker walks, and `node_context` ships it. The inspector asks. Both lists it renders — the NODE and its
OUTCOME — come out of that one answer, so they cannot disagree with each other or with publish.

The lock that matters is `TestThePickerAndPublishCannotDisagree`: it does not compare against a
remembered list of node ids, it drives publish for every node the picker offers and every node it
withholds. If the two walks ever diverge, the direction of the divergence does not matter — it goes red.
"""
import json
import re
from pathlib import Path

from frappe.tests.utils import FrappeTestCase

from tatva_connect.workflow_engine import context, graph, registry, upstream

_UP = "send-up"
_DOWN = "send-down"
_WAIT = "wait-1"


def _node(node_id, node_type, config=None, edges=None):
	return {
		"node_id": node_id,
		"node_type": node_type,
		"config_json": json.dumps(config or {}),
		"edges": [{"from_output": o, "to_node": t} for o, t in (edges or {}).items()],
	}


def _graph(source_node=None):
	"""Trigger → send-up → wait-1 → send-down → End.

	One graph carrying BOTH directions: an emitter genuinely upstream of the Wait, and an emitter
	genuinely downstream of it. A fixture with only the offender proves half the rule, and the half it
	skips is the one a too-aggressive filter would break.
	"""
	config = {"mode": "Until Event", "event_name": "delivered"}
	if source_node:
		config["source_node"] = source_node
	return [
		_node("trigger-1", "Trigger", {"subject_doctype": "CRM Lead", "event": "Created"}, {"next": _UP}),
		_node(_UP, "Send WhatsApp", {}, {"sent": _WAIT, "failed": _WAIT}),
		_node(_WAIT, "Wait", config, {"next": _DOWN}),
		_node(_DOWN, "Send WhatsApp", {}, {"sent": "end", "failed": "end"}),
		_node("end", "Terminal"),
	]


def _offered_at(node_id, nodes=None):
	return [e["node_id"] for e in upstream.emitters_at(nodes or _graph(), node_id)]


class TestTheOfferIsPositional(FrappeTestCase):
	"""THE red. Before this, both directions answered the same because position was never asked."""

	def test_a_node_downstream_of_the_wait_is_not_offered(self):
		self.assertNotIn(
			_DOWN, _offered_at(_WAIT),
			f"{_WAIT} is offered {_DOWN}, which journeys AFTER it — the journey would park for ever",
		)

	def test_a_node_upstream_of_the_wait_is_still_offered(self):
		"""The other direction, and the one a blunt fix breaks. Refusing everything passes the test above."""
		self.assertIn(_UP, _offered_at(_WAIT), f"{_UP} runs before {_WAIT} and must be offerable")

	def test_a_node_is_never_offered_itself(self):
		self.assertNotIn(_DOWN, _offered_at(_DOWN))

	def test_a_node_that_emits_nothing_is_not_offered(self):
		"""A Wait on a node with no outcomes builds a wait nothing can ever satisfy."""
		self.assertNotIn("trigger-1", _offered_at(_WAIT), "the Trigger reports no outcome to wait on")

	def test_the_offer_carries_the_outcomes_that_node_reports(self):
		"""The OUTCOME list resolves from the same answer, so the two pickers cannot disagree: the second
		one used to re-find the source in the frontend's own filtered array."""
		offered = {e["node_id"]: e for e in upstream.emitters_at(_graph(), _WAIT)}
		self.assertEqual(
			sorted(offered[_UP]["outcomes"]), sorted(registry.outcomes_for("Send WhatsApp")),
			"the offer's outcomes disagree with the node's own declaration",
		)

	def test_every_offer_carries_the_label_a_person_reads(self):
		"""Composed once, server-side. The inspector spelt `${node_id} · ${type}` itself, which is a second
		place the wording lives and a second place it can drift from the value picker's group heading."""
		for entry in upstream.emitters_at(_graph(), _WAIT):
			self.assertTrue(entry.get("label"), f"{entry['node_id']} is offered with nothing to display")
			self.assertIn(entry["node_id"], entry["label"])


class TestTheAnswerReachesTheWire(FrappeTestCase):
	"""C17.2 — a backend answer the wire does not carry is half a change, and the half that ships is the
	silent one: no JS error, clean build, an empty picker."""

	def test_node_context_ships_the_emitters(self):
		payload = context.node_context(json.dumps(_graph()), _WAIT)
		self.assertIn("emitters", payload, "the inspector has nothing to ask with")
		self.assertEqual([e["node_id"] for e in payload["emitters"]], _offered_at(_WAIT))

	def test_every_shipped_entry_carries_what_the_inspector_renders(self):
		for entry in context.node_context(json.dumps(_graph()), _WAIT)["emitters"]:
			for key in ("node_id", "label", "outcomes"):
				self.assertIn(key, entry, f"the inspector reads {key} and would get undefined")


class TestThePickerAndPublishCannotDisagree(FrappeTestCase):
	"""B3 — the declaration IS the enforcement. Two walks answering one question is the defect class; this
	drives BOTH of them over the same graph rather than naming what either should say."""

	_UNREACHABLE = "does not always run before it"

	def _source_faults(self, source_node):
		return [
			p["message"] for p in graph.problems(_graph(source_node), entry_node="trigger-1")
			if p.get("field") == "source_node"
		]

	def test_everything_the_picker_offers_publishes_clean(self):
		for node_id in _offered_at(_WAIT):
			with self.subTest(source=node_id):
				faults = self._source_faults(node_id)
				self.assertEqual(faults, [], f"the picker offers {node_id} and publish refuses it: {faults}")

	def test_everything_the_picker_withholds_publish_really_refuses(self):
		"""The direction that proves the filter is not merely narrower than the gate. A picker that hides a
		node publish would have accepted is a different bug wearing the same fix."""
		offered = set(_offered_at(_WAIT))
		withheld = [
			n["node_id"] for n in _graph()
			if n["node_id"] != _WAIT and n["node_id"] not in offered
			and registry.outcomes_for(n["node_type"])
		]
		self.assertTrue(withheld, "the fixture proves nothing unless something is genuinely withheld")
		for node_id in withheld:
			with self.subTest(source=node_id):
				self.assertTrue(
					any(self._UNREACHABLE in m for m in self._source_faults(node_id)),
					f"the picker hides {node_id} but publish would have accepted it",
				)

	def test_the_gate_and_the_offer_walk_the_same_ancestors(self):
		"""`graph.upstream_ancestors`' docstring claims the authoring picker uses this walk. It said so
		while being false. This is what makes it true."""
		for node in _graph():
			with self.subTest(node=node["node_id"]):
				self.assertTrue(
					set(_offered_at(node["node_id"])) <= graph.upstream_ancestors(_graph(), node["node_id"]),
					"the offer contains a node the gate does not consider an ancestor",
				)


	def test_a_node_on_ONE_arm_of_a_split_is_not_certain_at_the_join(self):
		"""The defect this walk shipped with, and the reason both the picker and the gate were wrong at once.

		Two arms of a Route rejoin. Both can REACH the join, so a walk that asks "can this reach me" counts
		both as having run — but exactly one did. A Wait at the join naming the other arm is then accepted
		by `_wait_problems`, whose own words are "does not always run before it — the journey would park for
		ever", and a journey down the unchosen arm does precisely that: Parked, no error, no step log, no
		clock, and the patient never gets their next task.

		The question is DOMINANCE, not reachability, and this is the lock on it. The straight-line case is
		asserted beside it because a fix that refuses everything would also pass the first half.
		"""
		def node(node_id, node_type, config, edges):
			return {"node_id": node_id, "node_type": node_type, "config_json": json.dumps(config),
			        "edges": [{"from_output": o, "to_node": t} for o, t in edges.items()]}

		trigger = {"mode": "Record Event", "subject_doctype": "CRM Lead", "event": "Created"}
		split = [
			node("start", "Trigger", trigger, {"next": "gate"}),
			node("gate", "Route", {"routes": [{"id": "a", "condition": None}, {"id": "b", "condition": None}]},
			     {"a": "task", "b": "note", "otherwise": "end"}),
			node("task", "Create Task", {"task_type": "x"}, {"next": "join"}),
			node("note", "Create Note", {"comment_mode": "Literal", "comment_text": "hi"}, {"next": "join"}),
			node("join", "Wait", {"mode": "Until Event", "event_name": "task.completed", "source_node": "task"},
			     {"event": "end"}),
			node("end", "Terminal", {}, {}),
		]
		self.assertNotIn("task", graph.upstream_ancestors(split, "join"),
		                 "a node on one arm of a split is not certain at the join")
		self.assertTrue(
			[p for p in graph.problems(split, "start")
			 if p.get("severity") == registry.BLOCKS and p.get("code") == "wait.source-unreachable"],
			"publish accepted a Wait that parks every journey down the other arm",
		)

		straight = [
			node("start", "Trigger", trigger, {"next": "task"}),
			node("task", "Create Task", {"task_type": "x"}, {"next": "join"}),
			node("join", "Wait", {"mode": "Until Event", "event_name": "task.completed", "source_node": "task"},
			     {"event": "end"}),
			node("end", "Terminal", {}, {}),
		]
		self.assertIn("task", graph.upstream_ancestors(straight, "join"),
		              "a node on the only path to this one IS certain")
		self.assertFalse(
			[p for p in graph.problems(straight, "start")
			 if p.get("severity") == registry.BLOCKS and p.get("code") == "wait.source-unreachable"],
			"a legitimate Wait on the node directly above it was refused",
		)


class TestNoInspectorListIsBuiltFromTheRawGraph(FrappeTestCase):
	"""THE STRUCTURAL GUARD. Without it this grows back the way `outputs_for` did — a JS re-derivation of
	a backend answer, under a comment claiming it mirrors one.

	B12 — it names no function and no field. It forbids the SHAPE: reaching into the raw graph prop to
	compose an offer. Whatever the next such list is called, it goes red.
	"""

	_CANVAS = Path("/home/frappe/frappe-bench/apps/crm/frontend/src/tatva/workflows")
	_DERIVES = re.compile(r"props\.graph\s*\.\s*(filter|map|find|some|every|reduce|flatMap)\b")

	def test_no_component_derives_an_offer_from_props_graph(self):
		if not self._CANVAS.exists():
			self.skipTest("frontend not mounted in this container")
		offenders = []
		for path in sorted(self._CANVAS.glob("*.vue")):
			for n, line in enumerate(path.read_text().splitlines(), 1):
				if self._DERIVES.search(line):
					offenders.append(f"{path.name}:{n}")
		self.assertEqual(
			offenders, [],
			f"the graph prop is being re-decided in the canvas instead of asked of the backend: {offenders}",
		)

	def test_the_graph_still_reaches_the_backend(self):
		"""The graph is not banned — it is the QUESTION. Deleting it would pass the test above by making the
		canvas answer nothing at all.

		Asked by the CANVAS rather than the inspector since `authoring_context`: the answer is a fact about
		the graph, and the inspector is a panel `:key` destroys on every node click, so asking there
		re-fetched the subject's whole schema per click.
		"""
		if not self._CANVAS.exists():
			self.skipTest("frontend not mounted in this container")
		source = (self._CANVAS / "WorkflowCanvas.vue").read_text()
		# The canvas asks `context.graph_context`, which is `authoring_context` plus the graph's outputs —
		# one round trip for both. What this lock is about is the canvas ASKING rather than re-deciding.
		self.assertIn("workflow_engine.context.graph_context", source)
		# The resolver is debounced now (`resolveGraphContextSoon`) and awaited directly where an answer is
		# needed before a write; both take the live graph, which is what this asserts.
		self.assertIn("resolveGraphContext", source, "the backend is no longer asked about the live graph")
		self.assertIn("graphNodes.value", source, "the graph handed over is no longer the live one")


class TestBothPickersHangOnTheWIRE(FrappeTestCase):
	"""A1 — the wiring in the payload is what answers, so a stale wire empties BOTH pickers at once.

	The canvas used to post node rows carrying the edges the graph was LOADED with, and nothing ever wrote
	them again — so an edge the author had just drawn was invisible here. `Waiting on` and `Outcome` were
	the visible half and no event-driven journey could be authored at all; the VALUE picker was the half
	nobody would have looked for, because `variables` and `emitters` come off the same ancestor walk.

	Driven as the difference one edge makes, rather than as a fixed expectation: what matters is not the
	list, it is that the list is a function of the wire.
	"""

	def _unwired(self):
		"""The same nodes with NOTHING joined to the Wait — the wire the author has not drawn yet.

		Two upstream nodes, because the two pickers ask about different things: a send reports OUTCOMES and
		writes no journey value, a Call API writes VALUES. One node could only ever prove half of it.
		"""
		return [
			_node("trigger-1", "Trigger", {"subject_doctype": "CRM Lead", "event": "Created"}, {}),
			_node(_UP, "Send WhatsApp", {}, {}),
			_node("api-up", "Call API", {"webhook_endpoint": "x"}, {}),
			_node(_WAIT, "Wait", {"mode": "Until Event", "event_name": "delivered"}, {}),
		]

	def _wired(self):
		found = self._unwired()
		found[1]["edges"] = [{"from_output": "sent", "to_node": "api-up"}]
		found[2]["edges"] = [{"from_output": "succeeded", "to_node": _WAIT}]
		return found

	def test_the_outcome_pickers_are_empty_until_the_wire_exists(self):
		self.assertEqual(_offered_at(_WAIT, self._unwired()), [])
		self.assertIn(_UP, _offered_at(_WAIT, self._wired()))

	def test_the_value_picker_is_the_second_victim_of_the_same_wire(self):
		"""Same walk, same payload — so a stale wire cost the author their values too."""
		before = {v["ref"] for v in upstream.available_at(json.dumps(self._unwired()), _WAIT)}
		after = {v["ref"] for v in upstream.available_at(json.dumps(self._wired()), _WAIT)}
		gained = after - before
		self.assertTrue(gained, "wiring a node in offered no new value — the two questions have diverged")
		self.assertTrue(all(ref.startswith("api-up.") for ref in gained), sorted(gained))

	def test_one_payload_answers_both(self):
		"""They are shipped together, off one call, so a control cannot be scoped by what it never got."""
		payload = context.node_context(json.dumps(self._wired()), _WAIT)
		self.assertIn(_UP, [e["node_id"] for e in payload["emitters"]])
		self.assertTrue([v for v in payload["variables"] if v["ref"].startswith("api-up.")])


def _judging_graph():
	"""Trigger → assign → call → End. `call` is the one verb that judges its own result."""
	return [
		_node("trigger-1", "Trigger", {"subject_doctype": "CRM Lead", "event": "Created"}, {"next": "assign"}),
		_node("assign", "Assign to User", {"assignee_mode": "User"}, {"assigned": "call", "nobody": "call"}),
		_node("call", "Call API", {"capture": [{"path": "body.data.id", "variable": "patient_id"}]},
		      {"succeeded": "end", "failed": "end"}),
		_node("end", "Terminal"),
	]


def _values_at(node_id):
	return {v["ref"] for v in upstream.emitted_at(_judging_graph(), node_id)}


class TestAVerbThatJudgesItsOwnResult(FrappeTestCase):
	"""The VALUE picker's one exception, and why it is not a hole in the rule above.

	A node is offered what ran before it, never its own — offering a value it has not produced is the lie
	this module exists to stop. A Call API is the exception `actions.judges_own_result` declares: it acts,
	writes its response into state, and only then reads `success_when` to pick its edge. Withholding its
	own values there left that box offering exactly the references that raise at run time, and none of the
	ones that work. `available_map` has always allowed them; this is the picker catching up.
	"""

	def test_it_is_offered_the_response_it_will_judge(self):
		at_call = _values_at("call")
		self.assertIn("call.status", at_call)
		self.assertIn("call.ok", at_call)
		self.assertIn("call.patient_id", at_call, "a capture row is a value this node will really have")

	def test_it_is_still_offered_what_ran_before_it(self):
		"""The direction a blunt fix breaks: swapping ancestors for own values passes the test above."""
		self.assertIn("assign.assigned_to", _values_at("call"))

	def test_every_other_node_is_still_withheld_its_own(self):
		self.assertNotIn("assign.assigned_to", _values_at("assign"))

	def test_the_picker_never_offers_more_than_publish_accepts(self):
		"""The lock. The gate is the superset by design, and the offer must stay inside it — a picker wider
		than the gate is a control that builds workflows publish then refuses."""
		available, _opaque = upstream.available_map(_judging_graph())
		for node_id in ("call", "assign"):
			self.assertLessEqual(
				_values_at(node_id), available[node_id],
				f"{node_id} is offered a value the publish gate does not accept",
			)
