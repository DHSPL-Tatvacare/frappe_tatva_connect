# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Publish refuses a predicate value the grain-scoped master holds no record of.

THE RED. A grain-scoped master's primary key is a composite `::` string — `Field-Sales::New Lead`,
`zone_assigned::Goodflip-Care::Anaya::::North` — and the Link column holds THAT, never the human word.
`taxonomy.picklist` is the one seam that knows this and every ingestion path resolves through it; a
predicate was the only consumer that never did. Nothing refused the bare word, so a branch comparing
Zone to `North` published green, matched nobody for ever, and said nothing about it. Four Anaya branches
were dead exactly this way — 506 zoned leads, 155 Lifetime-Free patients, and every stage exclusion on
the weekly and monthly reminder, which meant deceased and dropped-out patients stayed in scope for a
medicine message.

WHERE THE RULE LIVES. On the `reads` declaration, not on a node type: a Route carries its conditions as
ROWS and a Trigger carries one tree, so a per-type check would have had to be remembered twice and the
next predicate-holding type would have escaped it. Both declare a `trees` resolver and inherit the gate.
"""
import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.workflow_engine import graph, registry
from tatva_connect.workflow_engine.tests import fixtures

PICKED = "picked"
STAGE_REF = "crm_lead.custom_substage"


def _a_real_stage():
	"""A stage the fixture grain really has — its composite PK and the human word inside it."""
	row = frappe.db.get_value(
		"CRM Lead Stage", {"program": fixtures.GRAIN["program"], "selectable": 1},
		["name", "stage"], as_dict=True, order_by="position asc",
	)
	if not row:
		raise AssertionError(
			f"no CRM Lead Stage seeded for programme {fixtures.GRAIN['program']!r} — the fixture is wrong, "
			"not the code"
		)
	return row


def _rule(value, operator="is", field=STAGE_REF):
	return {"type": "rule", "field": field, "operator": operator, "value": value}


def _graph(condition=None, predicate=None):
	"""A two-node graph: the Trigger carrying `predicate`, a Route carrying `condition`."""
	trigger = fixtures.trigger(to="r1", predicate=predicate)
	route = fixtures.node(
		"r1", "Route",
		config={"routes": [{"id": PICKED, "condition": condition or _rule(_a_real_stage().name)}]},
		edges={PICKED: "end", "otherwise": "end"},
	)
	return [trigger, route, fixtures.node("end", "Terminal")]


def _problems(nodes):
	authored = [
		{"node_id": n["node_id"], "node_type": n["node_type"], "config": n["config"],
		 "edges": [{"from_output": o, "to_node": t} for o, t in n["edges"].items()]}
		for n in nodes
	]
	return graph.problems(authored, entry_node="start")


# This gate's own fix line — `_link_grain_problems` shares its `code`, so the fix is what tells them apart.
_FIX = "Pick the value from the list rather than typing it."


def _about_the_value(found):
	return [p for p in found if p.get("fix") == _FIX]


class TestPublishRefusesAValueThatCanNeverMatch(FrappeTestCase):
	def test_a_route_comparing_the_bare_word_is_refused(self):
		"""The defect, at the gate: `New Lead` is not what the column holds."""
		stage = _a_real_stage()

		found = _about_the_value(_problems(_graph(condition=_rule(stage.stage))))

		self.assertTrue(found, f"publish accepted {stage.stage!r}, which the column never holds")
		self.assertEqual(found[0]["severity"], registry.BLOCKS)

	def test_the_message_names_the_key_the_author_meant(self):
		"""A refusal an author cannot act on is a refusal that gets worked around."""
		stage = _a_real_stage()

		found = _about_the_value(_problems(_graph(condition=_rule(stage.stage))))

		self.assertIn(stage.name, found[0]["message"])

	def test_the_picked_key_passes(self):
		stage = _a_real_stage()

		self.assertEqual(_about_the_value(_problems(_graph(condition=_rule(stage.name)))), [])

	def test_a_trigger_predicate_is_judged_by_the_same_rule(self):
		"""Route rows and a Trigger's single tree both declare `trees`, so neither escapes it."""
		stage = _a_real_stage()

		found = _about_the_value(_problems(_graph(predicate=_rule(stage.stage))))

		self.assertTrue(found, "a Trigger predicate escaped the gate a Route row is held to")

	def test_a_membership_list_is_judged_item_by_item(self):
		"""`is one of` is where the Anaya stop-lists lived — one bad item is one refusal."""
		stage = _a_real_stage()
		mixed = f"{stage.name}\n{stage.stage}"

		found = _about_the_value(_problems(_graph(condition=_rule(mixed, operator="is one of"))))

		self.assertEqual(len(found), 1, "the list was judged as one string rather than item by item")
		self.assertIn(stage.stage, found[0]["message"])


class TestItRefusesOnlyWhatCannotMatch(FrappeTestCase):
	def test_contains_is_left_alone(self):
		"""Load-bearing: a substring of `<Programme>::<Stage>` tests one leaf across every programme."""
		stage = _a_real_stage()

		found = _about_the_value(_problems(_graph(condition=_rule(stage.stage, operator="contains"))))

		self.assertEqual(found, [], "`contains` was refused, which breaks testing one stage across grains")

	def test_a_presence_operator_carries_no_value_to_judge(self):
		found = _about_the_value(_problems(_graph(condition=_rule("", operator="is set"))))

		self.assertEqual(found, [])

	def test_a_link_at_a_plain_master_is_not_touched(self):
		"""Only a COMPOSITE-PK master is judged: whether a Webhook or a User exists is not this rule's
		question, and `_link_grain_problems` already owns the grain half."""
		found = _about_the_value(
			_problems(_graph(condition=_rule("nobody@example.com", field="crm_lead.lead_owner")))
		)

		self.assertEqual(found, [])

	def test_a_draft_save_is_not_refused(self):
		"""Timing, kept: context reaches a check at PUBLISH only, so an author mid-build is not blocked."""
		stage = _a_real_stage()
		route = _graph(condition=_rule(stage.stage))[1]

		found = registry.validate_node(
			"Route", route["config"], list(route["edges"]), mode=registry.DRAFT,
			graph_context=registry.graph_context(_graph(condition=_rule(stage.stage))),
		)

		self.assertEqual(_about_the_value(found), [])
