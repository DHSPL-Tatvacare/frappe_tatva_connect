# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""DATA AND CONFIG FLOW TOP TO BOTTOM. What an upstream node emits is offerable; nothing else is.

`node_context` already answered that for VALUES — `variables` comes from `upstream.available_at`, which
walks ancestors. It did not answer it for NODES, so the inspector built its own offer:

    props.graph.filter((n) => n.node_id !== props.node.node_id && declarationFor(n.node_type)?.outcomes)

Not-self, and can-emit. No position at all. A Wait was therefore offered every emitting node in the
graph INCLUDING ITS OWN DESCENDANTS — measured in the browser on 2026-07-22, where `wait-1` offered
`send-whatsapp-1`, the node it blocks.

Publish already refuses that graph (`graph._wait_problems` — "does not always run before it, the run
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
			f"{_WAIT} is offered {_DOWN}, which runs AFTER it — the run would park for ever",
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

	def test_the_graph_prop_still_reaches_the_backend(self):
		"""The prop is not banned — it is the QUESTION. Deleting it would pass the test above by making the
		inspector answer nothing at all."""
		if not self._CANVAS.exists():
			self.skipTest("frontend not mounted in this container")
		source = (self._CANVAS / "NodeInspector.vue").read_text()
		self.assertIn("node_context", source)
		self.assertIn("JSON.stringify(props.graph)", source, "the backend is no longer asked about the graph")
