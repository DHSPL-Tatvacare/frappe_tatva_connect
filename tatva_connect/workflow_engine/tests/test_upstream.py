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
		_node("b1", "Route", {"routes": [{"id": "r1", "label": "r1", "condition": {"type": "rule", "field": "crm_lead.status", "operator": "is", "value": "New"}}]}, {"r1": "end", "otherwise": "end"}),
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
		"""A node is offered what ran BEFORE it, never what it has not produced yet.

		The Create Note on the failed leg is the probe rather than the Call API: a verb declaring
		`judges_own_result` picks its edge AFTER acting, so its own values really do exist by the time its
		`success_when` is read and it is the one declared exception — see
		`test_upstream_offer.TestAVerbThatJudgesItsOwnResult`. Probing the exception here would have locked
		in the opposite rule for every other verb.
		"""
		note_keys = _keys("note")
		self.assertFalse(
			[k for k in note_keys if k.startswith("note.")],
			"a Create Note has not run when it is configured, so it may offer nothing of its own",
		)
		self.assertIn("api.patient_id", note_keys, "what ran before it is still offered")

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
		always = {"ref", "label", "type", "source", "source_label", "emitted"}
		for value in upstream.available_at(_graph(), "b1"):
			# Two conditional keys, both carried only by a value that HAS choices: `options` is the list
			# itself, and `pick` is how the control should offer them. A node-emitted value declares
			# neither and must not claim to. Spelling the key set as exactly `always` made this red for
			# every Select the subject has.
			self.assertEqual(set(value) - {"options", "pick"}, always, value)
			self.assertTrue(value["label"], "a value must be nameable to a person")
			self.assertTrue(value["source_label"], "a value must say, in words, where it came from")

	def test_every_value_says_whether_a_node_produced_it(self):
		"""C17.1 — the canvas asked this by scanning the raw `graph` prop for the source id, which is a
		backend answer recomputed client-side. It is answered HERE because only this module knows: it is
		the difference between the two loops in `available_at`.

		Unconditional, never "present when true": a row that simply omitted the key would read as a subject
		field and be narrowed away by a working set it was never subject to.
		"""
		for value in upstream.available_at(_graph(), "b1"):
			with self.subTest(ref=value["ref"]):
				self.assertIsInstance(value["emitted"], bool, "absent or None would be read as False")

	def test_a_node_value_says_it_was_emitted_and_a_subject_field_says_it_was_not(self):
		"""Both directions over ONE graph. Answering True for everything passes the half above, and it is
		the half that would silently stop the working set narrowing anything at all."""
		found = {v["ref"]: v for v in upstream.available_at(_graph(), "b1")}
		self.assertTrue(found["api.patient_id"]["emitted"], "the Call API node produced this value")
		self.assertFalse(found["crm_lead.status"]["emitted"], "this is the subject's own field")

	def test_an_emitted_value_names_its_producing_node_in_source(self):
		"""What makes the flag sufficient: the canvas needs the node id too, and for anything emitted the
		`source` namespace IS that id — so nothing has to be parsed back out of the ref."""
		by_id = {n["node_id"] for n in _graph()}
		for value in upstream.available_at(_graph(), "b1"):
			if value["emitted"]:
				with self.subTest(ref=value["ref"]):
					self.assertIn(value["source"], by_id, "an emitted value must name a node in the graph")

	def test_an_unknown_node_resolves_to_nothing(self):
		self.assertEqual(upstream.available_at(_graph(), "no-such-node"), [])

	def test_it_answers_for_an_unsaved_graph(self):
		"""The picker has to work while the author is still building — requiring a save first would leave
		it empty at the only moment it matters. Nothing here is persisted."""
		self.assertEqual(frappe.db.count("CRM Workflow", {"workflow_name": "never-saved"}), 0)
		self.assertIn("api.status", _keys("b1"))
