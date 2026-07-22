# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A ROWS RULE APPLIES ONLY WHEN THE FIELD IT READS APPLIES.

`_rows_from` reads `source_node` and never asked whether `source_node` is in play. So a Wait switched to
a pure timer still drew one edge per button of the node it used to wait on, no `next` handle existed to
wire the timer path to, and the workflow could not be built. The author got no explanation, because
nothing was wrong with their graph.

Measured on the bench before the fix, with a send declaring three buttons upstream:

    For Duration      -> ['yes', 'no', 'maybe']
    Until Time        -> ['yes', 'no', 'maybe']
    Until Event       -> ['yes', 'no', 'maybe']
    Event-or-Timeout  -> ['yes', 'no', 'maybe']

NOT a W2.1c regression: this is `outputs_for`, and the deleted JS twin copied it faithfully. Killing the
twin is what made it visible.

NO NEW VOCABULARY. `source_node` already declares when it applies —
`depends_on_value={"mode": [UNTIL_EVENT, EVENT_OR_TIMEOUT]}` — and `_applies` already reads exactly that.
A `rows_from` spec names its `node_field`; that field's own declaration says when it is in play. The gate
is on READ only: flipping the mode twice must not cost an author their wiring, so nothing is deleted.
"""
import json

from frappe.tests.utils import FrappeTestCase

from tatva_connect.workflow_engine import registry

_BUTTONS = [{"id": "yes"}, {"id": "no"}, {"id": "maybe"}]


def _graph(mode, source_node="send-1"):
	"""A send offering three buttons, and a Wait in `mode` still naming it. The lingering `source_node` is
	the point: an author who wired taps and then switched to a timer keeps the value on the node."""
	config = {"mode": mode}
	if source_node:
		config["source_node"] = source_node
	return [
		{"node_id": "send-1", "node_type": "Send WhatsApp",
		 "config_json": json.dumps({"buttons": _BUTTONS}), "edges": []},
		{"node_id": "wait-1", "node_type": "Wait", "config_json": json.dumps(config), "edges": []},
	]


def _outputs(mode, source_node="send-1"):
	return registry.graph_outputs(json.dumps(_graph(mode, source_node)))["wait-1"]


class TestATimerWaitDrawsNoTapBranches(FrappeTestCase):
	"""THE red."""

	def test_for_duration_offers_only_its_timer_edge(self):
		self.assertEqual(_outputs(registry.FOR_DURATION), ["next"])

	def test_until_time_offers_only_its_timer_edge(self):
		self.assertEqual(_outputs(registry.UNTIL_TIME), ["next"])

	def test_the_wiring_is_not_destroyed_to_achieve_it(self):
		"""Gated on READ. An author flipping mode timer → event → timer must find their taps intact, so the
		config keeps `source_node` and only the ANSWER changes."""
		nodes = _graph(registry.FOR_DURATION)
		registry.graph_outputs(json.dumps(nodes))
		self.assertEqual(registry.config_of(nodes[1])["source_node"], "send-1", "config was mutated")


class TestTheEventModesAreUnchanged(FrappeTestCase):
	"""The other direction. A gate that refuses everything would pass the class above."""

	def test_until_event_still_draws_one_edge_per_button(self):
		self.assertEqual(_outputs(registry.UNTIL_EVENT), ["yes", "no", "maybe"])

	def test_until_event_without_a_source_falls_back_to_its_mode(self):
		self.assertEqual(_outputs(registry.UNTIL_EVENT, source_node=None), ["event"])

	def test_a_timer_wait_that_never_named_a_source_is_unaffected(self):
		self.assertEqual(_outputs(registry.FOR_DURATION, source_node=None), ["next"])


class TestGraphOutputsAndOutputsForStayOneAnswer(FrappeTestCase):
	"""B3, driven across every mode rather than the one the fix was written against."""

	def test_the_two_readers_agree_in_every_mode(self):
		for mode in (registry.UNTIL_EVENT, registry.FOR_DURATION, registry.UNTIL_TIME, registry.EVENT_OR_TIMEOUT):
			with self.subTest(mode=mode):
				nodes = _graph(mode)
				graph_config = {n["node_id"]: registry.config_of(n) for n in nodes}
				self.assertEqual(
					registry.graph_outputs(json.dumps(nodes))["wait-1"],
					list(registry.outputs_for("Wait", graph_config["wait-1"], graph_config)),
				)


class TestRowsSpliceIntoTheLegTheyReplace(FrappeTestCase):
	"""`rows_from` says WHICH output its rows stand in for, so the other legs survive.

	Rows used to replace the whole map. `Event-or-Timeout` declares `["event", "timeout"]`, so a send
	offering buttons deleted the timeout edge — the timer leg could not be wired, in the one mode whose
	entire purpose is having both. Same unbuildable-node shape as the mode-gate bug above.

	`replaces: "event"` completes the key rather than adding a rival to it: `rows_from` already meant
	"these rows become edges" and simply never said which edge they stand in for.
	"""

	def test_event_or_timeout_keeps_its_timer_leg_beside_the_button_rows(self):
		self.assertEqual(_outputs(registry.EVENT_OR_TIMEOUT), ["yes", "no", "maybe", "timeout"])

	def test_until_event_answers_the_rows_alone(self):
		"""Its map is the event leg and nothing else, so splicing leaves exactly the rows."""
		self.assertEqual(_outputs(registry.UNTIL_EVENT), ["yes", "no", "maybe"])

	def test_the_rows_land_where_the_leg_they_replace_stood(self):
		"""Position is the contract the canvas draws handles from — the timer leg must stay last, not be
		re-ordered into the middle of the taps."""
		self.assertEqual(_outputs(registry.EVENT_OR_TIMEOUT)[-1], "timeout")

	def test_a_mode_that_never_declares_that_leg_is_untouched(self):
		"""The timer modes do not carry an `event` output, so there is nothing to splice into."""
		self.assertEqual(_outputs(registry.FOR_DURATION), ["next"])
