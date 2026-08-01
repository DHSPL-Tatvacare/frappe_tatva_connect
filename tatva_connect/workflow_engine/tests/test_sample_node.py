# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""SAMPLE — split by chance into arms of a declared size, with an automatic Remainder.

THE ONE RULE THAT MAKES THIS A SEPARATE NODE FROM ROUTE: a lead lands in the SAME arm every time it is
judged. A control group that reshuffles on a resume, a re-run or a second cohort is not a control group,
and the result of the trial it was measuring is worthless. That rule is meaningless inside Route — a
condition has nothing to be stable about — so merging the two would put a dead control on every
conditional node, which is why W7 §3 settled them as two nodes and NEVER one with a mode toggle.

The two questions a graph asks when it splits:
    what do I know about this person?  -> data decides   -> Route
    which arm did this person land in? -> chance decides -> Sample

NO NEW MECHANISM (G1). Outputs come from the SAME `outputs_by.rows_from` seam Route reads its own config
through — rows lead, the reserved `remainder` follows. Publish refuses an unhonourable split through the
SAME `FIELD_TYPES[...]["check"]` seam `Select`, `Link`, `Field` and `Predicate` already use, so `graph.py`
gains no code. Right-edge handles are count-keyed and never type-keyed, so Sample inherits them untouched.

SINGULAR: one node, one shape, always. No mode toggle, no `depends_on_value`, nothing that makes it look
like a Route.
"""
import json
import unittest
from typing import ClassVar

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.tests.authz.grains import assert_masters_exist
from tatva_connect.workflow_engine import graph, interpreter, registry
from tatva_connect.workflow_engine.tests import fixtures as fx

_WF = "sample-node-probe"


def _arm(rid, label, percent):
	return {"id": rid, "label": label, "percent": percent}


def _node(node_id, node_type, config=None, edges=None):
	return {
		"node_id": node_id, "node_type": node_type, "config_json": json.dumps(config or {}),
		"edges": [{"from_output": o, "to_node": t} for o, t in (edges or {}).items()],
	}


def _trigger(to="sp"):
	return _node("start", "Trigger", {
		"subject_doctype": "CRM Lead", "event": "Created",
		"vertical": fx.GRAIN["vertical"], "group": fx.GRAIN["group"], "program": fx.GRAIN["program"],
	}, {"next": to})


def _messages(nodes):
	return " | ".join(p["message"] for p in graph.problems(nodes, entry_node="start"))


class TestSampleOutputsAreItsOwnArmsPlusRemainder(unittest.TestCase):
	"""The SAME seam Route uses, reading own config. One handle per arm, then a reserved `remainder`."""

	def test_outputs_are_one_per_arm_then_remainder(self):
		outs = registry.outputs_for("Sample", {"arms": [{"id": "a"}, {"id": "b"}]})
		self.assertEqual(outs, ["a", "b", "remainder"])

	def test_no_arms_is_just_remainder_no_placeholder_leaks(self):
		self.assertEqual(registry.outputs_for("Sample", {"arms": []}), ["remainder"])
		self.assertEqual(registry.outputs_for("Sample", {}), ["remainder"])

	def test_an_arm_without_an_id_draws_no_handle(self):
		outs = registry.outputs_for("Sample", {"arms": [{"id": "a"}, {"label": "half-typed"}]})
		self.assertEqual(outs, ["a", "remainder"])

	def test_the_arms_field_shapes_outputs_so_the_inspector_re_resolves_handles(self):
		payload = next(n for n in registry.node_types() if n["type"] == "Sample")
		self.assertTrue(next(f for f in payload["config"] if f["name"] == "arms")["shapes_outputs"])


class TestSampleIsSingularAndIsNotRoute(unittest.TestCase):
	"""SINGULAR + the settled W7 §3 decision. Neither node may grow into the other."""

	def test_sample_declares_no_mode_and_no_field_that_morphs_it(self):
		declared = registry.declaration("Sample")
		for field in declared["config"]:
			self.assertNotIn("depends_on_value", field,
			                 "a Sample field gated on another makes the node change shape — SINGULAR forbids it")
		self.assertNotIn("field", declared["outputs_by"],
		                 "a mode-map on Sample would be the Route-with-a-toggle W7 rejected")

	def test_route_did_not_grow_a_sampling_arm(self):
		self.assertNotIn("arms", {f["name"] for f in registry.declaration("Route")["config"]})

	def test_sample_reads_no_run_state_because_an_arm_is_a_share_of_chance(self):
		field = next(f for f in registry.declaration("Sample")["config"] if f["name"] == "arms")
		self.assertIsNone(registry.read_kind_of(field),
		                  "an arm references nothing upstream; a read kind here would invent a reference")


class TestAssignmentIsAStableHashAndNeverADiceRoll(FrappeTestCase):
	"""G8 — the runtime is locked against the declaration, and against its own promise.

	`_arm_of` is driven directly rather than through a journey, because what is being asserted is the FUNCTION's
	stability: a test that walked the graph once could not tell a stable hash from a lucky draw.
	"""

	ARMS: ClassVar = [_arm("a", "Treatment", 50), _arm("b", "Control", 50)]

	def _node(self, node_id="sp"):
		return frappe._dict(node_id=node_id, node_type="Sample")

	def test_the_same_lead_lands_in_the_same_arm_every_time(self):
		first = interpreter._arm_of(self._node(), {"arms": self.ARMS}, "LEAD-0001")
		for _ in range(50):
			self.assertEqual(interpreter._arm_of(self._node(), {"arms": self.ARMS}, "LEAD-0001"), first)

	def test_it_does_not_use_a_process_salted_hash(self):
		"""`hash()` of a str is salted per process, so an assignment built on it re-randomises on every
		worker restart while looking perfectly deterministic inside one process. Pinned to the digest's own
		answer: swap in `hash()` or `random` and this goes red."""
		self.assertEqual(interpreter._arm_of(self._node(), {"arms": self.ARMS}, "LEAD-0001"),
		                 _expected_arm("LEAD-0001", "sp", self.ARMS))

	def test_two_samples_in_one_graph_split_independently(self):
		"""The node id is in the digest. Without it, every Sample below the first would send exactly the
		same people down the same side, and a two-stage trial would measure nothing."""
		one = [interpreter._arm_of(self._node("sp1"), {"arms": self.ARMS}, f"L{i}") for i in range(200)]
		two = [interpreter._arm_of(self._node("sp2"), {"arms": self.ARMS}, f"L{i}") for i in range(200)]
		self.assertNotEqual(one, two, "both Samples put the same leads in the same arms — a lockstep split")

	def test_the_arms_get_roughly_the_share_they_declare(self):
		arms = [_arm("a", "Ten", 10), _arm("b", "Ninety", 90)]
		landed = [interpreter._arm_of(self._node(), {"arms": arms}, f"L{i}") for i in range(2000)]
		self.assertAlmostEqual(landed.count("a") / 2000, 0.10, delta=0.03)
		self.assertAlmostEqual(landed.count("b") / 2000, 0.90, delta=0.03)

	def test_the_leftover_share_takes_remainder(self):
		arms = [_arm("a", "Tenth", 10)]
		landed = [interpreter._arm_of(self._node(), {"arms": arms}, f"L{i}") for i in range(2000)]
		self.assertAlmostEqual(landed.count("remainder") / 2000, 0.90, delta=0.03)

	def test_no_arms_sends_everyone_to_remainder(self):
		self.assertEqual(interpreter._arm_of(self._node(), {"arms": []}, "LEAD-0001"), "remainder")


def _expected_arm(subject, node_id, arms):
	"""The digest arithmetic, restated here ON PURPOSE so the lock has an independent second opinion —
	if the runtime's answer drifts from this, one of the two changed and the test says so."""
	import hashlib

	point = int(hashlib.sha256(f"{subject}::{node_id}".encode()).hexdigest()[:8], 16) % 10000 / 100.0
	ceiling = 0.0
	for row in arms:
		ceiling += float(row["percent"])
		if point < ceiling:
			return row["id"]
	return "remainder"


class TestSampleTakesItsArmThroughTheRealInterpreter(FrappeTestCase):
	"""Runtime, end to end. The outcome asserted is the node the journey finished on."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		assert_masters_exist()
		fx.purge(_WF)
		fx.arm_engine(True, cls)
		cls.lead = fx.make_lead()
		cls.workflow = fx.make_workflow(_WF, [
			fx.trigger(to="sp"),
			fx.node("sp", "Sample", config={"arms": [_arm("a", "Everyone", 100)]},
			        edges={"a": "end_a", "remainder": "end_rest"}),
			fx.node("end_a", "Terminal"),
			fx.node("end_rest", "Terminal"),
		])
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		fx.purge(_WF)
		frappe.delete_doc("CRM Lead", cls.lead.name, force=True, ignore_permissions=True)
		frappe.db.commit()
		super().tearDownClass()

	def test_a_hundred_percent_arm_takes_every_lead(self):
		"""A share of 100 is the one split whose outcome is knowable without asserting the digest, so the
		WIRING is what this proves: the arm's id really is an edge and the interpreter really leaves by it."""
		run = fx.start_journey(self.workflow, self.lead.name, "start")
		interpreter.advance(frappe.get_doc(fx.JOURNEY_DT, run.name))
		result = frappe.get_doc(fx.JOURNEY_DT, run.name)
		frappe.db.delete(fx.STEP_LOG_DT, {"journey": run.name})
		frappe.db.delete(fx.JOURNEY_DT, {"name": run.name})
		frappe.db.commit()
		self.assertEqual(result.current_node, "end_a")


class TestPublishRefusesASplitThatCannotBeHonoured(FrappeTestCase):
	"""The rule lives on the field type's `check`, so no new validator code exists in `graph.py`."""

	def _sample(self, arms, edges=None):
		return [
			_trigger(to="sp"),
			_node("sp", "Sample", {"arms": arms}, edges or {a["id"]: "end" for a in arms} | {"remainder": "end"}),
			_node("end", "Terminal"),
		]

	def test_arms_adding_up_past_the_whole_are_refused(self):
		found = _messages(self._sample([_arm("a", "A", 60), _arm("b", "B", 60)]))
		self.assertIn("120", found)
		self.assertIn("more than the whole", found)

	def test_arms_adding_up_to_less_than_the_whole_are_fine(self):
		"""The leftover IS the Remainder edge — demanding exactly 100 would make the reserved edge dead."""
		self.assertNotIn("more than the whole", _messages(self._sample([_arm("a", "A", 30)])))

	def test_exactly_a_hundred_is_fine(self):
		self.assertNotIn("more than the whole",
		                 _messages(self._sample([_arm("a", "A", 40), _arm("b", "B", 60)])))

	def test_an_arm_with_no_percentage_is_refused(self):
		self.assertIn("needs a percentage", _messages(self._sample([_arm("a", "A", None)])))

	def test_an_arm_nobody_can_land_in_is_refused(self):
		self.assertIn("never runs", _messages(self._sample([_arm("a", "A", 0)])))

	def test_an_unwired_arm_is_refused_by_the_rule_that_already_existed(self):
		"""Validation is FREE: the same edge rule that fires on Route fires here, with no new code."""
		nodes = self._sample([_arm("a", "A", 50), _arm("b", "B", 50)], edges={"a": "end", "remainder": "end"})
		self.assertIn("b", _messages(nodes))
