# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""THE CANVAS READS ANSWERS. IT NEVER INTERPRETS RULES.

`useNodeTypes.outputsFor` re-implemented `registry.outputs_for` wholesale — both resolution modes,
including W1.3's `rows_from` — under a comment reading "Mirrors registry.outputs_for exactly." Nothing
checked that claim. It is the function that rendered ZERO nodes for a day on 2026-07-22.

WHY THE FIRST LOCK MISSED IT. `test_upstream_offer` forbade `props.graph.filter/map/find/…` — ONE shape
of re-derivation. Re-implementing a backend function wholesale is a DIFFERENT shape and walked straight
past it, three files away. A lock narrow enough to miss the biggest offender in the same directory is
worse than none, because it reads as coverage.

So this file locks the CLASS, as a table of shapes rather than a list of functions:

  1. an offer composed by filtering the raw graph prop        (the W2.1b defect)
  2. a backend resolution RULE interpreted in JS              (this defect)

and shape 2's vocabulary is DERIVED FROM THE DECLARATIONS (B12) — `outputs_by` and its mode names are
read off `NODE_TYPES`, so a third resolution mode added later is forbidden in JS the day it is declared,
with nobody editing this file.

THE FIX IS THE WIRE, NOT A SYNCHRONISED TWIN. `graph_outputs(nodes)` resolves every node's outputs
server-side for the graph it actually sits in, and the canvas draws handles from that answer. The rule
(`outputs_by`) stops being shipped at all: a rule on the wire is an invitation to interpret it.
"""
import json
import re
from pathlib import Path

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.workflow_engine import registry

_SEND = "Send WhatsApp"
_BUTTONS = [{"id": "yes", "label": "Yes"}, {"id": "no", "label": "No"}]


def _node(node_id, node_type, config=None, edges=None):
	return {
		"node_id": node_id,
		"node_type": node_type,
		"config_json": json.dumps(config or {}),
		"edges": [{"from_output": o, "to_node": t} for o, t in (edges or {}).items()],
	}


def _graph_with_buttons():
	"""Trigger → send-1 (declaring two buttons) → wait-1 waiting on it.

	`rows_from` is the mode most likely to break, because it is the one whose answer depends on ANOTHER
	node's config. A fixture whose Wait keys only on `mode` would exercise the easy half.
	"""
	return [
		_node("trigger-1", "Trigger", {"subject_doctype": "CRM Lead", "event": "Created"}, {"next": "send-1"}),
		_node("send-1", _SEND, {"buttons": _BUTTONS}, {"sent": "wait-1", "failed": "wait-1"}),
		_node("wait-1", "Wait", {"mode": registry.UNTIL_EVENT, "source_node": "send-1"}, {}),
	]


class TestTheBackendResolvesOutputsForAWholeGraph(FrappeTestCase):
	"""THE red: there was no server-side answer the canvas could ask for at all."""

	def test_it_answers_every_node_in_the_graph(self):
		answer = registry.graph_outputs(json.dumps(_graph_with_buttons()))
		self.assertEqual(sorted(answer), ["send-1", "trigger-1", "wait-1"])

	def test_a_wait_on_a_send_with_buttons_draws_one_edge_per_button(self):
		"""The `rows_from` mode, end to end. This is what the JS twin had to re-implement by hand."""
		answer = registry.graph_outputs(json.dumps(_graph_with_buttons()))
		self.assertEqual(answer["wait-1"], ["yes", "no"])

	def test_a_wait_with_no_button_source_falls_back_to_its_mode(self):
		"""The other resolution mode, so a fix cannot be "always read the rows"."""
		nodes = _graph_with_buttons()
		nodes[2] = _node("wait-1", "Wait", {"mode": registry.UNTIL_EVENT})
		self.assertEqual(registry.graph_outputs(json.dumps(nodes))["wait-1"], ["event"])

	def test_a_fixed_output_node_answers_its_declared_outputs(self):
		answer = registry.graph_outputs(json.dumps(_graph_with_buttons()))
		self.assertEqual(answer["send-1"], list(registry.outputs_for(_SEND)))

	def test_it_never_becomes_a_third_answer(self):
		"""B3 — it must BE `outputs_for`, not agree with it today. Driven over the real graph."""
		nodes = _graph_with_buttons()
		graph_config = {n["node_id"]: registry.config_of(n) for n in nodes}
		answer = registry.graph_outputs(json.dumps(nodes))
		for node in nodes:
			with self.subTest(node=node["node_id"]):
				self.assertEqual(
					answer[node["node_id"]],
					list(registry.outputs_for(node["node_type"], graph_config[node["node_id"]], graph_config)),
				)

	def test_it_is_gated_like_every_other_authoring_endpoint(self):
		"""Restored through addCleanup, never a bare call: `set_user` is not a context manager here, and a
		test that leaves the session as Guest fails the NEXT test instead of itself."""
		self.addCleanup(frappe.set_user, frappe.session.user)
		frappe.set_user("Guest")
		with self.assertRaises(frappe.PermissionError):
			registry.graph_outputs(json.dumps(_graph_with_buttons()))


class TestTheRuleStaysServerSide(FrappeTestCase):
	"""G5 — the twin is DELETED, and the rule it fed on stops being shipped. A rule on the wire is an
	invitation to interpret it, and the invitation was accepted once already."""

	def test_node_types_does_not_ship_the_resolution_rule(self):
		offenders = [n["type"] for n in registry.node_types() if "outputs_by" in n]
		self.assertEqual(offenders, [], f"the canvas is still handed the rule for: {offenders}")

	def test_the_field_that_reshapes_a_node_says_so_itself(self):
		"""The inspector decided WHEN to re-draw handles by reading `declaration.outputs_by.field`. That is
		the same class of rule-reading, so the declaration answers it instead."""
		wait = next(n for n in registry.node_types() if n["type"] == "Wait")
		shaping = [f["name"] for f in wait["config"] if f.get("shapes_outputs")]
		self.assertEqual(shaping, ["mode"], "exactly the field the outputs key on must be marked")

	def test_a_node_whose_outputs_are_fixed_marks_nothing(self):
		send = next(n for n in registry.node_types() if n["type"] == _SEND)
		self.assertEqual([f["name"] for f in send["config"] if f.get("shapes_outputs")], [])


class TestTheCardSummaryReadsTheTable(FrappeTestCase):
	"""The SEVENTH type consumer W2.1 missed: `WorkflowNode.vue` named Predicate / Requirements / Mapping
	itself to summarise a card. Cosmetic, but it is the type vocabulary living in a seventh place."""

	def test_every_type_declares_how_it_is_summarised(self):
		for name, row in registry.FIELD_TYPES.items():
			with self.subTest(type=name):
				self.assertIn("summary", row, "None is a valid answer, absence is not")

	def test_every_non_scalar_type_is_named_rather_than_printed(self):
		"""Walks the TABLE, never a remembered list. Naming three of them by hand is exactly how `Value Map`
		and `Button List` were missed — both hold arrays and both would render `[object Object]` on a card."""
		unnamed = [
			name for name, row in registry.FIELD_TYPES.items()
			if not row["scalar"] and row["summary"] is None
		]
		self.assertEqual(unnamed, [], f"these hold a list or a tree and would print as objects: {unnamed}")

	def test_a_scalar_type_declares_how_it_reads(self):
		"""The other direction, and it was WRONG until 2026-08-10: a scalar declared nothing, on the premise
		that a plain value is printable. It is not. A Link holds a composite primary key, so a card printed
		`Goodflip-Care::Anaya::Tukavo::Tucatinib Order punch`; a Duration holds `add_to_date` kwargs, so it
		printed `{"days": 10}`; a Check holds 1. The intent stands — a scalar is SHOWN, never renamed — but
		the row must now say HOW it reads, and `raw` is the explicit answer for the ones that already read
		well. The frontend used to fill that silence with `String(value)`, which is the seventh consumer this
		whole class exists to prevent."""
		silent = [
			name for name, row in registry.FIELD_TYPES.items()
			if row["scalar"] and not (row["summary"] or {}).get("as")
		]
		self.assertEqual(silent, [], f"these leave the card to guess how to print them: {silent}")

	def test_a_summary_says_which_of_the_three_shapes_it_is(self):
		"""One of `count`, `phrase` or `as`, never two and never none — the card branches on exactly this."""
		for name, row in registry.FIELD_TYPES.items():
			with self.subTest(type=name):
				self.assertEqual(
					len(set(row["summary"] or {}) & {"count", "phrase", "as"}), 1, "one shape, declared"
				)

	def test_every_reading_a_row_names_is_one_the_card_can_render(self):
		"""A row naming a reading the card has no renderer for would print nothing at all — the silent
		blank that replaces the silent raw value. The vocabulary is closed and lives in ONE place."""
		for name, row in registry.FIELD_TYPES.items():
			reading = (row["summary"] or {}).get("as")
			if reading:
				with self.subTest(type=name):
					self.assertIn(reading, registry.CARD_READINGS)

	def test_the_summary_reaches_the_wire_on_the_field(self):
		"""C17.2 — the card reads `f.summary`; a table column the wire drops is half a change."""
		for node in registry.node_types():
			for field in node.get("config") or []:
				with self.subTest(node=node["type"], field=field["name"]):
					self.assertIn("summary", field, "the card reads f.summary and would get undefined")


class TestTheCanvasNeverComputesABackendAnswer(FrappeTestCase):
	"""THE STRUCTURAL GUARD, widened from one shape to the class.

	B12 — no function is named. Shape 2's vocabulary is derived from the declarations, so it follows a
	rename and covers a resolution mode nobody has written yet.
	"""

	_CANVAS = Path("/home/frappe/frappe-bench/apps/crm/frontend/src/tatva")

	@staticmethod
	def _rule_vocabulary():
		"""The declaration keys that exist to be RESOLVED server-side.

		Only the distinctive ones. `field`, `map` and `key` are also rule keys but are ordinary English in
		JS, and a lock that fires on `.map(` would be noise rather than a guard. Forbidding the CONTAINER
		is sufficient and complete: the rule cannot be interpreted without naming it first.
		"""
		found = set()
		for declared in registry.NODE_TYPES.values():
			rule = declared.get("outputs_by")
			if rule:
				found.add("outputs_by")
				found |= {k for k in rule if isinstance(rule[k], dict) and k != "map"}
		return found

	def _offenders(self, pattern):
		found = []
		for path in sorted(self._CANVAS.rglob("*")):
			if path.suffix not in (".js", ".vue"):
				continue
			for n, line in enumerate(path.read_text().splitlines(), 1):
				stripped = line.strip()
				# A comment may legitimately describe the rule; only interpretation is forbidden.
				if stripped.startswith(("//", "*", "/*", "<!--")):
					continue
				if pattern.search(line):
					found.append(f"{path.name}:{n}")
		return found

	def test_no_offer_is_composed_from_the_raw_graph_prop(self):
		"""Shape 1 — the W2.1b defect, kept because deleting a lock when its defect is fixed is how the
		defect comes back."""
		if not self._CANVAS.exists():
			self.skipTest("frontend not mounted in this container")
		offenders = self._offenders(re.compile(r"props\.graph\s*\.\s*(filter|map|find|some|every|reduce|flatMap)\b"))
		self.assertEqual(offenders, [], f"an offer is being composed from the raw graph at: {offenders}")

	def test_no_backend_resolution_rule_is_interpreted_in_js(self):
		"""Shape 2 — THE red for this chunk."""
		if not self._CANVAS.exists():
			self.skipTest("frontend not mounted in this container")
		vocabulary = self._rule_vocabulary()
		self.assertTrue(vocabulary, "nothing derived — the lock would pass by testing nothing")
		pattern = re.compile("|".join(re.escape(word) for word in sorted(vocabulary)))
		offenders = self._offenders(pattern)
		self.assertEqual(
			offenders, [],
			f"the canvas is interpreting a backend rule ({', '.join(sorted(vocabulary))}) at: {offenders}",
		)

	def test_the_vocabulary_really_covers_the_modes_that_exist(self):
		"""The lock's own coverage, asserted — otherwise a narrowed `_rule_vocabulary` would silently make
		every test above pass by forbidding nothing."""
		self.assertIn("outputs_by", self._rule_vocabulary())
		self.assertIn("rows_from", self._rule_vocabulary(), "the mode that broke is not covered")
