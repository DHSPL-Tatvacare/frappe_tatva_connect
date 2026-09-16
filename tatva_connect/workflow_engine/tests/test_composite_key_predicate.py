# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Publish refuses a predicate value the grain-scoped master holds no record of, and passes a key or a label.

A label is what the picker offers and what the evaluator reads as every key carrying it (`rules._read_as_keys`),
so only a value that is neither — a branch that matches nobody for ever — is refused, on Route rows and a
Trigger's tree alike.
"""
import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.taxonomy import labels
from tatva_connect.workflow_engine import graph, registry
from tatva_connect.workflow_engine.tests import fixtures

PICKED = "picked"
STAGE_REF = "crm_lead.custom_substage"
_NOWHERE = "zz-no-such-stage"


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
	def test_a_value_no_record_carries_is_refused(self):
		"""The defect, at the gate: a word that is neither a key nor a label matches nobody for ever."""
		found = _about_the_value(_problems(_graph(condition=_rule(_NOWHERE))))

		self.assertTrue(found, f"publish accepted {_NOWHERE!r}, which names no stage")
		self.assertEqual(found[0]["severity"], registry.BLOCKS)
		self.assertIn(_NOWHERE, found[0]["message"])

	def test_the_picked_key_passes(self):
		stage = _a_real_stage()

		self.assertEqual(_about_the_value(_problems(_graph(condition=_rule(stage.name)))), [])

	def test_the_label_the_picker_offers_passes(self):
		"""The picker offers labels and the evaluator reads them as keys, so publish must not refuse one."""
		label = labels.label(_a_real_stage().name, labels.LEAD_STAGE)

		self.assertEqual(_about_the_value(_problems(_graph(condition=_rule(label)))), [])

	def test_a_trigger_predicate_is_judged_by_the_same_rule(self):
		"""Route rows and a Trigger's single tree both declare `trees`, so neither escapes it."""
		found = _about_the_value(_problems(_graph(predicate=_rule(_NOWHERE))))

		self.assertTrue(found, "a Trigger predicate escaped the gate a Route row is held to")

	def test_a_membership_list_is_judged_item_by_item(self):
		"""`is one of` is where the Anaya stop-lists lived — one bad item is one refusal."""
		stage = _a_real_stage()
		mixed = f"{stage.name}\n{_NOWHERE}"

		found = _about_the_value(_problems(_graph(condition=_rule(mixed, operator="is one of"))))

		self.assertEqual(len(found), 1, "the list was judged as one string rather than item by item")
		self.assertIn(_NOWHERE, found[0]["message"])


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
		route = _graph(condition=_rule(_NOWHERE))[1]

		found = registry.validate_node(
			"Route", route["config"], list(route["edges"]), mode=registry.DRAFT,
			graph_context=registry.graph_context(_graph(condition=_rule(_NOWHERE))),
		)

		self.assertEqual(_about_the_value(found), [])
