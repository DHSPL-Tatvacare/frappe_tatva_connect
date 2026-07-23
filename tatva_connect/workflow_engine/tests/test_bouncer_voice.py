# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""THE BOUNCER'S VOICE — a fault carries a severity, a stable code and a one-line fix, from ONE authority.

A fault used to be `{node_id, field, message}` with a single implicit severity: it blocked. So a true
statement that is NOT a reason to refuse a publish — "the engine switch is off, so this workflow will not
run yet" — had nowhere to live: it would have to block a publish the switches are MEANT to ship off, or
hide in a second gate. This adds `severity` (`blocks` | `warns`) so a warning can exist without a second
gate, plus a `code` (a stable lint-rule id that survives a reworded message) and a `fix` (one line).

THE ONE RULE, and the reason this is not a catalog: a fault's severity/code/fix come from the SAME
constructor every fault flows through, and each call site names its own. There is no lookup table of
rule-ids the gate consults — two copies would drift the moment one is changed and the other forgotten.

THE ACCEPTANCE BAR is the drift test at the bottom: the Bouncer (author time) and the engine (runtime)
both hit "an edge to a node that is not in the graph", and they must name it by the SAME code. If they
ever diverge, they have become two catalogs.
"""
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.workflow_engine import graph, interpreter, registry, versions
from tatva_connect.workflow_engine.tests import fixtures as fx

_WF = "bouncer-voice-probe"


def _graph(*nodes):
	return [
		{
			"node_id": n["node_id"],
			"node_type": n["node_type"],
			"config_json": frappe.as_json(n.get("config") or {}),
			"edges": [{"from_output": o, "to_node": t} for o, t in (n.get("edges") or {}).items()],
		}
		for n in nodes
	]


def _broken():
	"""A graph with an edge to a node that does not exist — the fault BOTH layers can produce."""
	return _graph(fx.trigger(to="ghost"), fx.node("end", "Terminal"))


class TestOneConstructorFiveKeys(FrappeTestCase):
	"""STEP 1+2: one shape, built in one place. `graph._at` adds `node_id` to `registry.problem` rather
	than hand-building a second dict — so the Bouncer and the node validator cannot drift in shape."""

	def test_every_problem_carries_the_full_shape(self):
		found = graph.problems(_broken(), entry_node="start")
		self.assertTrue(found)
		for p in found:
			self.assertEqual(
				set(p), {"node_id", "field", "message", "code", "severity", "fix"},
				f"a problem is not the one shape: {sorted(p)}",
			)
			self.assertTrue(p["message"], "a problem must always be explainable to a person")
			self.assertIn(p["severity"], (registry.BLOCKS, registry.WARNS))

	def test_a_node_validator_problem_and_a_graph_problem_share_one_shape(self):
		"""The two constructors collapsed to one: a `validate_node` problem and a `graph` problem carry the
		same keys, because `_at` wraps `problem` rather than building its own dict."""
		node_level = registry.validate_node("Trigger", {"event": "Exploded"}, [])
		self.assertTrue(node_level)
		graph_level = graph.problems(_broken(), entry_node="start")
		self.assertEqual(
			set(node_level[0]) | {"node_id"}, set(graph_level[0]),
			"the node validator and the graph gate build differently-shaped problems",
		)

	def test_every_whole_graph_fault_names_a_code(self):
		"""A code is what makes the rule list its own documentation and survives a message reword."""
		for p in graph.problems(_broken(), entry_node="start"):
			self.assertTrue(p["code"], f"a fault carries no code: {p['message']}")

	def test_a_blocking_fault_offers_a_fix(self):
		found = [p for p in graph.problems(_broken(), entry_node="start") if p["severity"] == registry.BLOCKS]
		self.assertTrue(found)
		self.assertTrue(all(p["fix"] for p in found), "a blocking fault must say what to do about it")


class TestPublishRefusesOnBlocksOnly(FrappeTestCase):
	"""STEP 4: publish counts BLOCKS, not len(problems). A warns reaches the author and does not stop the
	save; the switches are meant to ship off, so publishing ahead of the operator is legitimate."""

	@classmethod
	def tearDownClass(cls):
		fx.purge(_WF)
		super().tearDownClass()

	def _sound_workflow(self):
		fx.purge(_WF)
		return fx.make_workflow(_WF, [
			fx.trigger(to="end"),
			fx.node("end", "Terminal"),
		], lifecycle_state="Draft")

	def test_a_sound_graph_with_the_engine_off_still_publishes(self):
		"""The engine ships OFF (dormant). A sound graph must publish anyway — the mute is a WARNING."""
		doc = self._sound_workflow()
		found = doc.publish_problems()
		self.assertTrue(any(p["code"] == "engine.muted" and p["severity"] == registry.WARNS for p in found),
		                f"the engine-off warning is missing: {found}")
		self.assertFalse([p for p in found if p["severity"] == registry.BLOCKS],
		                 f"a sound graph must carry no blockers: {found}")
		# The one that matters: assert_publishable does NOT throw on a warns-only graph.
		doc.assert_publishable()

	def test_the_mute_warning_is_driven_by_the_switch_not_hardcoded(self):
		"""Flip the switch (via its ONE reader) and the warning is gone — proving it is a true statement
		about the bench, not decoration."""
		doc = self._sound_workflow()
		with patch("tatva_connect.automation.settings.is_enabled", return_value=True):
			found = doc.publish_problems()
		self.assertFalse(any(p["code"] == "engine.muted" for p in found),
		                 f"the mute warning survived the engine being on: {found}")

	def test_a_real_block_still_refuses(self):
		"""A graph with a genuine blocker (an edge to a missing node) is still refused."""
		fx.purge(_WF)
		doc = fx.make_workflow(_WF, [fx.trigger(to="ghost")], lifecycle_state="Draft")
		with self.assertRaises(frappe.ValidationError):
			doc.assert_publishable()


class TestTheBouncerAndTheEngineAreNotTwoCatalogs(FrappeTestCase):
	"""THE ACCEPTANCE BAR. One fault — an edge to a node not in the graph — is caught by the Bouncer at
	publish (a problem) and by the engine at runtime (a `_Permanent`). They must NAME it by the same code,
	or they are two languages for one fault that will drift."""

	@classmethod
	def tearDownClass(cls):
		fx.purge(_WF)
		super().tearDownClass()

	def test_the_bouncer_names_the_missing_node_fault_by_the_shared_code(self):
		found = graph.problems(_broken(), entry_node="start")
		dangling = [p for p in found if p["code"] == registry.CODE_NODE_NOT_IN_GRAPH]
		self.assertTrue(dangling, f"the Bouncer does not code the dangling-edge fault: {found}")

	def test_the_engine_permanent_carries_the_same_code(self):
		"""Freeze a graph whose entry edge points at a ghost, run it inline, and read the code off the
		`_Permanent` it raises. Same string as the Bouncer's, from ONE constant both layers import."""
		fx.purge(_WF)
		lead = fx.make_lead()
		wf = fx.make_workflow(_WF, [fx.trigger(to="ghost")], lifecycle_state="Draft")
		# No commit: `run_inline` reads the frozen version in this same transaction, and committing here
		# would leak the lead past the per-test rollback (Azure/DB parity is the file layer's rule, but a
		# committed lead is the same class of leak this bench was bitten by before).
		version_name = versions.ensure_version(frappe.get_doc("CRM Workflow", wf.name))
		with self.assertRaises(interpreter._Permanent) as caught:
			interpreter.run_inline(version_name, lead.name, None, {})
		self.assertEqual(caught.exception.code, registry.CODE_NODE_NOT_IN_GRAPH)

	def test_both_layers_agree(self):
		"""Stated as one assertion so the drift is impossible to miss."""
		fx.purge(_WF)
		lead = fx.make_lead()
		wf = fx.make_workflow(_WF, [fx.trigger(to="ghost")], lifecycle_state="Draft")
		version_name = versions.ensure_version(frappe.get_doc("CRM Workflow", wf.name))
		bouncer = next(p["code"] for p in graph.problems(_broken(), entry_node="start")
		               if p["code"] == registry.CODE_NODE_NOT_IN_GRAPH)
		try:
			interpreter.run_inline(version_name, lead.name, None, {})
			engine = None
		except interpreter._Permanent as e:
			engine = e.code
		self.assertEqual(bouncer, engine, "the Bouncer and the engine name one fault by two codes")
