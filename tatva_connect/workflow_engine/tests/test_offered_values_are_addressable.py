# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A VALUE IS OFFERED ONLY WHEN THE RUNTIME CAN READ IT BACK.

A node id is a REFERENCE SOURCE: a later node reads this one's values as `<node_id>.<value>`, and
`refs._SOURCE` reads a source as a plain identifier. A canvas minting ids from a type name produced
`call-api-1`, which composes to `call-api-1.summary` — a string `refs.parse` refuses, so
`Values._lookup` never finds it.

THE RED THIS LOCKS. The write still landed: a handler writes a bare name into its own bucket without
parsing, so the AI's answer really was in journey state. Only the READ failed, and it failed by
returning nothing. The picker offered the value, `graph._reference_problems` blessed it because both
sides compared the same unreadable string, and the row resolved to None on a live record with every
step logged `ok` — a ticket raised with an empty description and nothing anywhere saying why.

`upstream._emitted_by` is the ONE function both sides ask — `available_at` for what an author may pick,
`available_map` for what the gate will accept — so the filter belongs there and nowhere else. Neither
can bless what the runtime cannot read.

LEGACY IDS STAY PUBLISHABLE. Nodes already stored carry hyphens in their ids, and re-saving a workflow
must not refuse them: an id is only judged where it is used AS A SOURCE. A hyphenated node still runs,
still writes its own bucket and still takes its edges — its values are simply not offered, because
nothing could ever address them.
"""
import json

from frappe.tests.utils import FrappeTestCase

from tatva_connect.workflow_engine import graph, refs, upstream

_SUBJECT = "CRM Lead"
_ENDPOINT_CONFIG = {
	"webhook_endpoint": "Any Curated Endpoint",
	"capture": [{"variable": "summary", "path": "body.choices.0.message.content"}],
}


def _node(node_id, node_type, config=None, edges=None):
	return {
		"node_id": node_id, "node_type": node_type,
		"config_json": json.dumps(config or {}),
		"edges": [{"from_output": o, "to_node": t} for o, t in (edges or {}).items()],
	}


def _graph(call_id, reads=None):
	"""Trigger → Call API → Update Field → End, with the write reading whatever `reads` names."""
	updates = [{"name": "description", "mode": refs.FROM_CONTEXT, "value": reads}] if reads else []
	return [
		_node("trigger_1", "Trigger", {"subject_doctype": _SUBJECT, "event": "Created"},
		      {"next": call_id}),
		_node(call_id, "Call API", _ENDPOINT_CONFIG, {"succeeded": "write_1", "failed": "end_1"}),
		_node("write_1", "Update Field", {"target_doctype": _SUBJECT, "updates": updates},
		      {"next": "end_1"}),
		_node("end_1", "Terminal"),
	]


def _offered(nodes, node_id):
	return {value["ref"] for value in upstream.available_at(nodes, node_id) if value.get("emitted")}


class TestAnUnaddressableNodeOffersNothing(FrappeTestCase):
	"""THE red: every one of these was offered, and none of them could be read."""

	def test_a_node_whose_id_is_not_a_source_offers_no_values(self):
		offered = _offered(_graph("call-api-1"), "write_1")
		self.assertEqual(offered, set())

	def test_the_same_node_named_as_a_source_offers_its_values(self):
		"""The other half: the filter must not swallow a node that IS addressable."""
		offered = _offered(_graph("call_api_1"), "write_1")
		self.assertIn("call_api_1.summary", offered)
		self.assertIn("call_api_1.status", offered)

	def test_everything_offered_anywhere_can_be_parsed_back(self):
		"""The invariant itself, driven over the graph rather than asserted about one id."""
		for call_id in ("call-api-1", "call_api_1"):
			nodes = _graph(call_id)
			for node in nodes:
				for ref in _offered(nodes, node["node_id"]):
					self.assertIsNotNone(refs.parse(ref), f"{ref} is offered but cannot be read")


class TestPublishRefusesWhatCannotBeRead(FrappeTestCase):
	"""The gate already said "nothing before it produces it" — it just never got to say it here."""

	def _codes(self, nodes):
		return [p.get("code") for p in graph.problems(nodes, entry_node="trigger_1")]

	def test_a_row_reading_an_unaddressable_value_is_refused(self):
		nodes = _graph("call-api-1", reads="call-api-1.summary")
		self.assertIn("ref.unresolved", self._codes(nodes))

	def test_the_same_row_publishes_when_the_node_is_addressable(self):
		nodes = _graph("call_api_1", reads="call_api_1.summary")
		self.assertNotIn("ref.unresolved", self._codes(nodes))

	def test_a_legacy_hyphenated_node_that_nothing_reads_still_publishes(self):
		"""Stored graphs keep their ids: an id is judged where it is used as a SOURCE, never on sight."""
		self.assertNotIn("ref.unresolved", self._codes(_graph("call-api-1")))
