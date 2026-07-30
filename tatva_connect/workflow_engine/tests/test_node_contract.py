# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A node declares what it reads, and publish refuses a reference nothing produces.

Verbs have always declared `emits`, and `upstream.available_at` has always turned those declarations into
the list of values a node may read — but only the authoring picker ever asked. The gate never did. So a
workflow naming a variable no upstream node writes published green and failed on a live record: a
predicate raises and kills the journey, while every other kind of reference fails SILENTLY (the assignee
becomes None and the node leaves by `nobody`; the due date becomes None and the task takes its default;
the written value becomes None). Nothing in a log distinguishes any of that from correct behaviour.

The negative case is tested as hard as the positive one. A check that cannot be trusted is worse than no
check: an author who sees one false rejection stops believing the true ones.
"""
import json

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.workflow_engine import contract, graph


def _node(node_id, node_type, config=None, edges=None):
	return {
		"node_id": node_id,
		"node_type": node_type,
		"config_json": json.dumps(config or {}),
		"edges": [{"from_output": o, "to_node": t} for o, t in (edges or {}).items()],
	}


def _trigger(to="n1", **config):
	return _node("start", "Trigger", {"subject_doctype": "CRM Lead", "event": "Created", **config}, {"next": to})


def _graph(*nodes):
	return [*nodes]


def _messages(nodes):
	return " | ".join(p["message"] for p in graph.problems(nodes, entry_node="start"))


class TestReadsDeclaration(FrappeTestCase):
	"""`contract.reads_of` — every way a node can name a value, derived from the DECLARED field."""

	def test_a_variable_field_is_a_reference(self):
		found = contract.reads_of("Update Field", {"value_mode": "From Context", "context_field": "patient_id"})
		self.assertEqual([r["ref"] for r in found], ["patient_id"])

	def test_a_predicate_names_every_field_it_tests(self):
		tree = {"type": "all", "children": [
			{"type": "rule", "field": "status", "operator": "is", "value": "New"},
			{"type": "rule", "field": "ok", "operator": "is", "value": 1},
		]}
		found = {r["ref"] for r in contract.reads_of("Route", {"routes": [{"id": "r1", "condition": tree}]})}
		self.assertEqual(found, {"status", "ok"})

	def test_an_expression_names_the_context_keys_it_reads(self):
		found = {r["ref"] for r in contract.reads_of(
			"Create Task", {"due_mode": "Expression", "due_expression": 'add_days(ctx["report_date"], 3)'}
		)}
		self.assertEqual(found, {"report_date"})

	def test_a_ctx_json_map_names_its_dollar_ctx_values(self):
		found = {r["ref"] for r in contract.reads_of("Append Child Row", {
			"child_table": "labs", "set_json": json.dumps({"value": "$ctx.result", "unit": "mg"}),
		})}
		self.assertEqual(found, {"result"}, "a literal must not be read as a reference")

	def test_a_typed_address_is_now_a_reference_the_gate_will_refuse(self):
		"""`free_text` is DELETED, so a Variable field holds a reference and nothing else. A typed address
		is therefore read as a reference nothing upstream produces, and publish refuses it — which is the
		whole point: an address is picked, never typed."""
		found = contract.reads_of("Send Email", {"email_recipient": "ops@tatvacare.in"})
		self.assertEqual([r["ref"] for r in found], ["ops@tatvacare.in"])

	def test_a_namespaced_recipient_is_a_reference(self):
		found = contract.reads_of("Send Email", {"email_recipient": "sv.escalation_email"})
		self.assertEqual([r["ref"] for r in found], ["sv.escalation_email"])

	def test_a_bare_name_is_a_reference_too_now_that_nothing_is_free_text(self):
		"""The mirror image. While `free_text` existed a bare name was a LITERAL, and that split is exactly
		what let the gate and the runtime disagree about one string."""
		found = contract.reads_of("Send Email", {"email_recipient": "escalation_email"})
		self.assertEqual([r["ref"] for r in found], ["escalation_email"])

	def test_a_reference_says_which_control_carries_it(self):
		found = contract.reads_of("Update Field", {"value_mode": "From Context", "context_field": "nope"})
		self.assertEqual(found[0]["field"], "context_field", "the author must be told which box to fix")


class TestPublishRefusesADanglingReference(FrappeTestCase):
	"""The gate itself — the half that did not exist."""

	def test_a_variable_nothing_produces_is_refused(self):
		"""The headline. `patient_id` is captured by NO node here."""
		nodes = _graph(
			_trigger(to="n1"),
			_node("n1", "Update Field", {
				"target_doctype": "CRM Lead", "fieldname": "status",
				"value_mode": "From Context", "context_field": "patient_id",
			}, {"next": "end"}),
			_node("end", "Terminal"),
		)
		self.assertIn("patient_id", _messages(nodes))

	def test_the_same_variable_is_accepted_when_an_upstream_node_captures_it(self):
		"""The negative half. A Call API capturing `patient_id` makes the very same graph legal."""
		nodes = _graph(
			_trigger(to="api"),
			_node("api", "Call API", {
				"webhook_endpoint": "x",
				"capture": [{"path": "body.id", "variable": "patient_id"}],
			}, {"succeeded": "n1", "failed": "end"}),
			_node("n1", "Update Field", {
				"target_doctype": "CRM Lead", "fieldname": "status",
				"value_mode": "From Context", "context_field": "api.patient_id",
			}, {"next": "end"}),
			_node("end", "Terminal"),
		)
		self.assertNotIn("api.patient_id", _messages(nodes))

	def test_a_value_produced_on_another_branch_is_refused(self):
		"""`patient_id` is captured on the FAILED leg, so it has not been written on the succeeded leg."""
		nodes = _graph(
			_trigger(to="api"),
			_node("api", "Call API", {"webhook_endpoint": "x"}, {"succeeded": "n1", "failed": "other"}),
			_node("other", "Call API", {
				"webhook_endpoint": "y", "capture": [{"path": "body.id", "variable": "patient_id"}],
			}, {"succeeded": "end", "failed": "end"}),
			_node("n1", "Update Field", {
				"target_doctype": "CRM Lead", "fieldname": "status",
				"value_mode": "From Context", "context_field": "patient_id",
			}, {"next": "end"}),
			_node("end", "Terminal"),
		)
		self.assertIn("patient_id", _messages(nodes))

	def test_a_subject_field_is_always_available(self):
		"""The lead's own fields need no upstream producer — they are on the document."""
		nodes = _graph(
			_trigger(to="n1"),
			_node("n1", "Route", {"routes": [{"id": "r1", "label": "New", "condition": {
				"type": "rule", "field": "crm_lead.status", "operator": "is", "value": "New",
			}}]}, {"r1": "end", "otherwise": "end"}),
			_node("end", "Terminal"),
		)
		self.assertNotIn("crm_lead.status", _messages(nodes))

	def test_set_variables_contributes_the_keys_of_its_dict(self):
		"""An author writes `{"stage": ...}`; those keys are knowable without running it."""
		nodes = _graph(
			_trigger(to="sv"),
			_node("sv", "Set Variables", {"assign": '{"stage": "Qualified"}'}, {"next": "n1"}),
			_node("n1", "Update Field", {
				"target_doctype": "CRM Lead", "fieldname": "status",
				"value_mode": "From Context", "context_field": "sv.stage",
			}, {"next": "end"}),
			_node("end", "Terminal"),
		)
		self.assertNotIn("sv.stage", _messages(nodes))

	def test_an_unenumerable_set_variables_suspends_the_check_downstream(self):
		"""A computed key cannot be named, so absence cannot be PROVEN. A false rejection here would block
		a correct workflow, which is worse than the gap it would close."""
		nodes = _graph(
			_trigger(to="sv"),
			_node("sv", "Set Variables", {"assign": 'dict(other="x")'}, {"next": "n1"}),
			_node("n1", "Update Field", {
				"target_doctype": "CRM Lead", "fieldname": "status",
				"value_mode": "From Context", "context_field": "whatever",
			}, {"next": "end"}),
			_node("end", "Terminal"),
		)
		self.assertNotIn("whatever", _messages(nodes))


class TestPublishRefusesANodeIdThatCollidesWithAReachableRecord(FrappeTestCase):
	"""A node id may not be the slug of a record the journey can reach.

	The namespaced contract distinguishes two values by making the SOURCE unique. A node called `crm_lead`
	breaks exactly that: `crm_lead.status` would name both the lead's column and whatever the node emitted,
	and `Values._lookup` asks the node bucket FIRST — so the node silently shadows the subject, which is
	the collision the whole contract exists to remove. It cannot be resolved at runtime, so it must not be
	publishable.
	"""

	def test_a_node_named_after_the_subject_is_refused(self):
		nodes = _graph(
			_trigger(to="crm_lead"),
			_node("crm_lead", "Call API", {"webhook_endpoint": "x"}, {"succeeded": "end", "failed": "end"}),
			_node("end", "Terminal"),
		)
		self.assertIn("crm_lead", _messages(nodes))

	def test_an_ordinary_node_id_is_fine(self):
		"""The negative half: only a REACHABLE record's slug is taken, not every plausible name."""
		nodes = _graph(
			_trigger(to="api"),
			_node("api", "Call API", {"webhook_endpoint": "x"}, {"succeeded": "end", "failed": "end"}),
			_node("end", "Terminal"),
		)
		self.assertNotIn("Rename the node", _messages(nodes))


class TestPublishRefusesAnUnwakeableWait(FrappeTestCase):
	"""A Wait that can never be woken — the failure that leaves a journey Parked for ever with no error."""

	def _wait(self, **config):
		return _graph(
			_trigger(to="task"),
			_node("task", "Create Task", {"task_type": "x"}, {"next": "w1"}),
			_node("w1", "Wait", {"mode": "Until Event", **config}, {"event": "end"}),
			_node("end", "Terminal"),
		)

	def test_a_wait_naming_no_node_is_allowed(self):
		"""Not every signal comes from the graph. A Wait naming no node answers to a signal delivered from
		outside for this subject, which carries no node token — refusing it would delete a real pattern."""
		found = _messages(self._wait(event_name="task.completed"))
		self.assertNotIn("wait on", found)
		self.assertNotIn("never reports", found)

	def test_a_wait_that_does_not_name_an_outcome_is_refused(self):
		"""Whatever it waits on, it must say WHAT it waits for — a blank event name matches nothing."""
		self.assertIn("does not say which one", _messages(self._wait(source_node="task")))

	def test_a_wait_naming_a_node_that_does_not_exist_is_refused(self):
		self.assertIn("not in this workflow", _messages(self._wait(source_node="ghost", event_name="task.completed")))

	def test_a_wait_naming_a_node_that_does_not_run_first_is_refused(self):
		"""The quiet one: a node on another leg mints no token for this journey, so nothing can ever wake it."""
		nodes = _graph(
			_trigger(to="b1"),
			_node("b1", "Route", {"routes": [{"id": "r1", "label": "New", "condition": {"type": "rule", "field": "status", "operator": "is", "value": "New"}}]},
			      {"r1": "w1", "otherwise": "task"}),
			_node("task", "Create Task", {"task_type": "x"}, {"next": "end"}),
			_node("w1", "Wait", {"mode": "Until Event", "source_node": "task", "event_name": "task.completed"},
			      {"event": "end"}),
			_node("end", "Terminal"),
		)
		self.assertIn("does not always run before it", _messages(nodes))

	def test_a_wait_on_an_outcome_the_node_never_reports_is_refused(self):
		found = _messages(self._wait(source_node="task", event_name="task.exploded"))
		self.assertIn("never reports", found)

	def test_a_correctly_wired_wait_is_accepted(self):
		"""The negative half — this is the ordinary shape and it must publish cleanly."""
		found = _messages(self._wait(source_node="task", event_name="task.completed"))
		for phrase in ("which node to wait on", "not in this workflow", "does not always run", "never reports"):
			self.assertNotIn(phrase, found)
