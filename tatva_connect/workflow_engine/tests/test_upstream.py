# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""What a node may read is derived from the graph, not typed from memory.

Before this, the only way to name a value in a predicate was to type it. A misspelling produced a
condition that looked correct, never matched, and said nothing — the exact failure the engine already
fixed once for event names, and did not fix for anything else.

The two properties that make the resolver trustworthy are both NEGATIVE, so they are tested hardest: a
value written by a node that has not run yet must never be offered, and a node must not offer its own
output to itself. Offering either would invite the silent non-match this exists to prevent.
"""
import json

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.workflow_engine import upstream


def _node(node_id, node_type, config=None, edges=None):
	return {
		"node_id": node_id,
		"node_type": node_type,
		"config_json": json.dumps(config or {}),
		"edges": [{"from_output": o, "to_node": t} for o, t in (edges or {}).items()],
	}


def _graph():
	"""Trigger → Call API → Branch → End, with a Create Note hanging off the failed leg."""
	return [
		_node("start", "Trigger", {"subject_doctype": "CRM Lead", "event": "Created"}, {"next": "api"}),
		_node("api", "Call API", {
			"webhook_endpoint": "x",
			"capture": [{"path": "body.data.id", "variable": "patient_id"}],
		}, {"succeeded": "b1", "failed": "note"}),
		_node("b1", "Branch", {}, {"true": "end", "false": "end"}),
		_node("note", "Create Note", {"comment_mode": "Literal", "comment_text": "hi"}, {"next": "end"}),
		_node("end", "Terminal"),
	]


def _keys(node_id, graph=None):
	return [v["ref"] for v in upstream.available_at(graph or _graph(), node_id)]


class TestUpstream(FrappeTestCase):
	# --- what a node CAN see ----------------------------------------------------------------------------

	def test_a_branch_sees_what_the_call_before_it_wrote(self):
		"""The headline. A Branch had no field source at all, so its control rendered permanently
		disabled and the node could never be configured."""
		keys = _keys("b1")
		for expected in ("api.status", "api.ok", "api.error"):
			self.assertIn(expected, keys, f"the Call API's {expected} must be offered downstream")

	def test_a_variable_the_author_named_is_offered_by_that_name(self):
		"""`capture` rows are the author's own naming. Offering them back is what removes the typo."""
		self.assertIn("api.patient_id", _keys("b1"))

	def test_each_value_says_which_node_produced_it(self):
		found = upstream.available_at(_graph(), "b1")
		captured = next(v for v in found if v["ref"] == "api.patient_id")
		self.assertEqual(captured["source"], "api", "an author must be able to see where a value came from")

	# --- what a node must NOT see -----------------------------------------------------------------------

	def test_a_node_is_not_offered_its_own_output(self):
		"""A Call API cannot test its own response while configuring the call that produces it.

		Probed with `ok` and the author's own `patient_id`, never with `status`: `status` is ALSO a real
		CRM Lead column, so it is legitimately offered as a subject field and proves nothing here.
		"""
		self.assertNotIn("api.patient_id", _keys("api"))
		self.assertNotIn("api.ok", _keys("api"))

	def test_a_node_emit_does_not_shadow_a_subject_field_of_the_same_name(self):
		"""THE COLLISION, now impossible by construction.

		This test used to assert the opposite, and said so: Call API emits `status`, CRM Lead has a `status`
		column, `available_at` de-duplicated by bare key with node values FIRST, and downstream of a Call
		API the name `status` therefore meant the HTTP status while the lead's own became unpickable —
		silently, under one label, with the author told nothing. It closed with "the fix is to namespace what
		a node writes; until then this test states the behaviour rather than letting a future reader discover
		it on a live lead."

		That fix has shipped, so the lock is inverted rather than deleted: two values, two references, two
		sources, and the author picks the one they mean.
		"""
		offered = [
			v for v in upstream.available_at(_graph(), "b1")
			if v["ref"] in ("api.status", "crm_lead.status")
		]
		self.assertEqual(
			{v["ref"] for v in offered}, {"api.status", "crm_lead.status"},
			"the node's status and the lead's status must both be offered, each saying where it came from",
		)
		self.assertEqual(
			{v["source"] for v in offered}, {"api", "crm_lead"},
			"neither value may eat the other — that ambiguity is what the contract removes",
		)
		self.assertEqual(
			len({v["source_label"] for v in offered}), 2,
			"two sources must read as two DIFFERENT headings, or the picker shows one `Status` twice",
		)

	def test_a_source_says_in_words_where_it_came_from(self):
		"""The label a person reads, decided here and not in the picker.

		A node's group leads with the id the author themselves typed, because that is what they will scan
		for; the subject's group is the doctype. Deriving either in JS would be a second brain that knows
		`crm_lead` means the lead and cannot know what `api` means, since that name is the author's.
		"""
		found = {v["ref"]: v for v in upstream.available_at(_graph(), "b1")}
		self.assertEqual(found["crm_lead.status"]["source_label"], "CRM Lead")
		self.assertIn("api", found["api.status"]["source_label"])
		self.assertIn("Call API", found["api.status"]["source_label"])

	def test_a_value_from_another_branch_is_not_offered(self):
		"""`note` sits on the FAILED leg. Nothing it writes can be read by `b1` on the succeeded leg —
		and nothing downstream of a node is available to it either."""
		graph = _graph()
		graph.append(_node("late", "Call API", {
			"webhook_endpoint": "y", "capture": [{"path": "x", "variable": "from_the_future"}],
		}, {"succeeded": "end", "failed": "end"}))
		self.assertNotIn("late.from_the_future", _keys("b1", graph))

	def test_the_trigger_sees_no_node_variables(self):
		"""Nothing has run when the Trigger's own predicate is judged, so only the subject exists."""
		found = upstream.available_at(_graph(), "start")
		self.assertEqual(
			[v for v in found if v["source"] != "crm_lead"], [],
			"the Trigger has no ancestors, so no node can have written anything yet",
		)

	# --- shape ------------------------------------------------------------------------------------------

	def test_every_value_uses_the_builder_contract_shape(self):
		"""One shape whatever the source, so the control need not know where a value came from. No
		per-field `operators`: the contract resolves those by TYPE, and a per-field list here would be a
		second operator vocabulary — the existing one emits symbols the evaluator rejects outright."""
		for value in upstream.available_at(_graph(), "b1"):
			self.assertEqual(set(value), {"ref", "label", "type", "source", "source_label"}, value)
			self.assertTrue(value["label"], "a value must be nameable to a person")
			self.assertTrue(value["source_label"], "a value must say, in words, where it came from")

	def test_an_unknown_node_resolves_to_nothing(self):
		self.assertEqual(upstream.available_at(_graph(), "no-such-node"), [])

	def test_it_answers_for_an_unsaved_graph(self):
		"""The picker has to work while the author is still building — requiring a save first would leave
		it empty at the only moment it matters. Nothing here is persisted."""
		self.assertEqual(frappe.db.count("CRM Workflow", {"workflow_name": "never-saved"}), 0)
		self.assertIn("api.status", _keys("b1"))
