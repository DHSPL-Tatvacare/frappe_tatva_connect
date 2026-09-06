# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A Call API node captures its response and routes on it.

The point of W7 is that a workflow can ACT on what an endpoint said back. That needs three things to be
true, and each is tested here against a real graph rather than by calling the helper directly:

  * the response lands in named journey variables, so downstream nodes read them like any other value;
  * the node leaves by `succeeded` or `failed`, so the author routes by wiring rather than by writing
    a condition;
  * a failure is DATA, not an exception — a refused connection must take the `failed` edge, not kill
    the journey. A node that dies on a 500 is a workflow that cannot handle the case it exists to handle.

The HTTP call itself is the one thing stubbed: this suite is about what the engine does with an answer,
and reaching a real endpoint would make it a network test. `_call_endpoint` is the seam — everything
above it (capture, predicate, routing, state) runs for real.
"""
import json
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.tests.authz.grains import assert_masters_exist
from tatva_connect.workflow_engine import interpreter, refs
from tatva_connect.workflow_engine.tests import fixtures as fx

_WORKFLOW = "call-api-probe"
_ENDPOINT = "Call API Probe Endpoint"

_HANDLER = "tatva_connect.automation.actions._call_endpoint"


def _response(status=200, ok=True, body=None, error=None):
	return {"status": status, "ok": ok, "body": body if body is not None else {}, "error": error}


class TestCallApi(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		assert_masters_exist()
		fx.purge(_WORKFLOW)
		fx.arm_engine(True, cls)
		cls.lead = fx.make_lead()
		_ensure_endpoint()
		cls.workflow = fx.make_workflow(_WORKFLOW, [
			fx.trigger(to="call"),
			fx.node("call", "Call API", config={
				"webhook_endpoint": _ENDPOINT,
				"capture": [
					{"path": "status", "variable": "http_status"},
					{"path": "body.data.id", "variable": "patient_id"},
				],
				"success_when": {"type": "rule", "field": "call.status", "operator": "is", "value": 201},
			}, edges={"succeeded": "won", "failed": "lost"}),
			fx.node("won", "Terminal"),
			fx.node("lost", "Terminal"),
		])
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		fx.purge(_WORKFLOW)
		if frappe.db.exists("Webhook", _ENDPOINT):
			frappe.delete_doc("Webhook", _ENDPOINT, force=True, ignore_permissions=True)
		frappe.delete_doc("CRM Lead", cls.lead.name, force=True, ignore_permissions=True)
		frappe.db.commit()

	def setUp(self):
		self._clear()

	def tearDown(self):
		self._clear()

	def _clear(self):
		for run in frappe.get_all(fx.JOURNEY_DT, filters={"workflow": self.workflow.name}, pluck="name"):
			frappe.db.delete(fx.STEP_LOG_DT, {"journey": run})
		frappe.db.delete(fx.JOURNEY_DT, {"workflow": self.workflow.name})
		frappe.db.commit()

	def _run_with(self, response):
		with patch(_HANDLER, return_value=response):
			run = fx.start_journey(self.workflow, self.lead.name, "start")
			interpreter.advance(frappe.get_doc(fx.JOURNEY_DT, run.name))
		frappe.db.commit()
		row = frappe.db.get_value(
			fx.JOURNEY_DT, run.name, ["status", "current_node", "state_json"], as_dict=True
		)
		# The node's own bucket: `state_json` nests by writer, so what THIS Call API captured is under its
		# node id and can never be confused with a lead column of the same name.
		return row, frappe.parse_json(row.state_json or "{}").get("call", {})

	# --- the request actually leaves ----------------------------------------------------------------------

	def test_the_payload_survives_serialisation_and_the_call_reaches_the_network(self):
		"""THE ONE TEST THAT IS NOT MOCKED, and the reason this bug lived.

		Every other test here patches `_call_endpoint` — the exact function that builds the request — so
		the suite was green while the node could not send anything at all. `payload_doc.as_dict()` hands
		back real `datetime` objects (`creation`, `modified`, every Date field), `requests`' `json=` cannot
		serialise them, and the raise was caught by the transport guard and turned into the `failed` edge.
		Every Frappe document has those fields, so EVERY Call API call died before it reached the network
		and the graph simply took its failure branch, silently and for ever.

		The endpoint is `example.invalid`, so this test resolves no DNS and sends nothing. That is the
		point: it must fail at the NETWORK, not at serialisation, and the two are told apart by the error.
		"""
		from tatva_connect.automation import actions

		result = actions._call_endpoint(_ENDPOINT, frappe.get_doc("CRM Lead", self.lead.name))

		self.assertEqual(result["status"], 0, "an unroutable host cannot answer")
		self.assertNotIn(
			"JSON serializable", result["error"] or "",
			"the payload must serialise: this failing means the request never left the process",
		)

	def test_the_call_does_not_commit_the_transaction_it_runs_in(self):
		"""A Call API runs INSIDE the journey's segment — it has to, because the journey routes on the reply.

		So it may not commit. `create_request_log` ends in `frappe.db.commit()`, which published the
		caller's pending writes and dropped the savepoints `_run_verb` and `run_inline` roll back to; the
		Integration Request is built here instead. Proven by writing a row, calling out, then rolling
		back: if the call committed, the row survives.
		"""
		probe = frappe.get_doc({"doctype": "CRM Lead", "first_name": "CommitProbe",
		                        "lead_name": "Commit Probe"}).insert(ignore_permissions=True)
		from tatva_connect.automation import actions

		actions._call_endpoint(_ENDPOINT, frappe.get_doc("CRM Lead", self.lead.name))
		frappe.db.rollback()

		self.assertFalse(frappe.db.exists("CRM Lead", probe.name),
		                 "the outbound call committed the caller's pending write")

	# --- the author writes the body ---------------------------------------------------------------------

	_BODY = json.dumps({
		"model": "gpt-4o",
		"metadata": {"lead": "$ctx.crm_lead.name"},
		"messages": [{"role": "user", "content": "$ctx.crm_lead.first_name"}],
	})

	def test_an_authored_body_resolves_references_at_every_depth(self):
		"""An API body is NESTED — `messages` is a list of objects — and the reference an author needs is
		usually inside it. A flat resolver would send the literal string `$ctx.crm_lead.first_name` to the
		provider, which is a wrong request that still gets a 200 from some of them."""
		from tatva_connect.automation import actions

		state = refs.Values(buckets={"crm_lead": {"name": "LEAD-1", "first_name": "Ramesh"}})
		built = actions.build_request_body(self._BODY, state)

		self.assertEqual(built["model"], "gpt-4o", "a literal is left alone")
		self.assertEqual(built["metadata"]["lead"], "LEAD-1", "a reference nested in an object resolves")
		self.assertEqual(
			built["messages"][0]["content"], "Ramesh",
			"a reference nested inside a LIST of objects resolves — this is where every real API body puts it",
		)

	def test_the_gate_sees_every_reference_the_runtime_will_resolve(self):
		"""THE DIVERGENCE LOCK. The publish gate reads the body to refuse a reference nothing produces, and
		the runtime reads it again to fill them in. Two walks over one structure is the exact shape that
		drifts: a gate that sees fewer references than the runtime resolves blesses a workflow that then
		sends a literal `$ctx.…` to a real provider.

		`_ctx_json_keys` used to walk only `data.values()` — one level — so every reference inside
		`messages` was invisible to it while the runtime resolved them happily.
		"""
		from tatva_connect.automation import actions
		from tatva_connect.workflow_engine import contract

		seen_by_gate = contract.reads_of("Call API", {
			"webhook_endpoint": _ENDPOINT, "webhook_payload_source": "Custom", "request_body": self._BODY,
		})
		self.assertEqual(
			{r["ref"] for r in seen_by_gate},
			set(actions.body_references(self._BODY)),
			"what publish checks and what the journey resolves must be the same set, or the gate is decorative",
		)

	def test_a_test_call_makes_no_request_while_the_engine_is_dormant(self):
		"""An authoring screen is still the product. A site whose automation is switched off must not make
		an outbound request because someone opened a node and pressed a button — 'the flag is off so X did
		not happen' is correct behaviour, and the control is told WHY rather than left to guess."""
		from tatva_connect.workflow_engine import context as node_context

		was = fx.arm_engine(False)
		try:
			answer = node_context.test_call(_ENDPOINT)
		finally:
			fx.arm_engine(bool(was))

		self.assertFalse(answer["armed"])
		self.assertNotIn("status", answer, "a dormant engine answers about itself, it does not call out")

	def test_a_test_call_says_which_lead_it_built_the_request_from(self):
		"""A response is shaped by the record behind it. An author mapping a tree built from a lead they
		did not choose would capture paths that do not exist for the next one."""
		from tatva_connect.workflow_engine import context as node_context

		answer = node_context.test_call(_ENDPOINT, lead=self.lead.name)

		self.assertTrue(answer["armed"])
		self.assertEqual(answer["lead"], self.lead.name)
		self.assertEqual(answer["status"], 0, "example.invalid cannot answer, which is the point")

	# --- capture ----------------------------------------------------------------------------------------

	def test_the_response_lands_in_named_variables(self):
		"""Both a top-level value and one dug out of the body — the whole point of the mapping."""
		_row, state = self._run_with(_response(201, True, {"data": {"id": "P-42"}}))
		self.assertEqual(state.get("http_status"), 201)
		self.assertEqual(state.get("patient_id"), "P-42")

	def test_a_path_that_resolves_to_nothing_captures_empty_not_missing(self):
		"""A downstream predicate naming that variable must see it as empty rather than raise as though
		the author had misspelt it — the variable was declared, the endpoint just did not send it."""
		_row, state = self._run_with(_response(201, True, {"data": {}}))
		self.assertIn("patient_id", state)
		self.assertIsNone(state["patient_id"])

	# --- routing ----------------------------------------------------------------------------------------

	def test_it_leaves_by_succeeded_when_the_predicate_holds(self):
		row, _state = self._run_with(_response(201, True, {"data": {"id": "P-1"}}))
		self.assertEqual(row.current_node, "won")
		self.assertEqual(row.status, "Done")

	def test_it_leaves_by_failed_when_the_predicate_does_not(self):
		"""200 is a fine HTTP status, and this author declared that only 201 counts. The AUTHOR'S rule
		decides, not the transport — which is why `success_when` exists at all."""
		row, _state = self._run_with(_response(200, True, {"data": {"id": "P-1"}}))
		self.assertEqual(row.current_node, "lost")

	def test_a_transport_failure_routes_to_failed_rather_than_killing_the_run(self):
		"""The case the node exists to handle. A refused connection is the `failed` edge, not an error."""
		row, state = self._run_with(_response(0, False, None, error="Connection refused"))
		self.assertEqual(row.current_node, "lost")
		self.assertEqual(row.status, "Done", "a failed call must not fail the journey")
		self.assertEqual(state.get("http_status"), 0)

	def test_the_step_log_records_what_the_call_answered(self):
		"""An outbound call that left no trace is unauditable — the journey must say what came back."""
		self._run_with(_response(500, False, {"msg": "boom"}))
		detail = {entry.node_id: entry.detail for entry in fx.logs(
			frappe.get_all(fx.JOURNEY_DT, filters={"workflow": self.workflow.name}, pluck="name")[0]
		)}
		self.assertIn("500", detail["call"])


def _ensure_endpoint():
	"""A curated Webhook row — the endpoint an author PICKS. Its URL is never typed on the node."""
	if frappe.db.exists("Webhook", _ENDPOINT):
		return
	frappe.get_doc({
		"doctype": "Webhook",
		"name": _ENDPOINT,
		"webhook_doctype": "CRM Lead",
		"request_url": "https://example.invalid/probe",
		"request_method": "POST",
		"webhook_docevent": "on_update",
	}).insert(ignore_permissions=True)  # authz-ok: tier-c — test fixture, no user input
	frappe.db.commit()



class TestCallApiSuccessWhen(FrappeTestCase):
	"""`success_when` speaks the same vocabulary as every other predicate control.

	It did not: the picker offered what ran before the node plus the subject's fields, while the box was
	judged against the response alone. The two sets never overlapped, so every reference an author could
	pick killed the journey and the two that worked were not offered. Driven through real graphs — the
	defect lived in the gap between the halves, and a direct call reproduces neither.
	"""

	@classmethod
	def setUpClass(cls):
		assert_masters_exist()
		fx.arm_engine(True, cls)
		cls.lead = fx.make_lead(status="New")
		_ensure_endpoint()
		cls.built = []

	@classmethod
	def tearDownClass(cls):
		fx.purge(*cls.built)
		frappe.delete_doc("CRM Lead", cls.lead.name, force=True, ignore_permissions=True)
		frappe.db.commit()

	def _land(self, name, success_when, response):
		"""Where a journey ended and how. `status` matters as much as the node: the defect killed runs."""
		fx.purge(name)
		workflow = fx.make_workflow(name, [
			fx.trigger(to="call"),
			fx.node("call", "Call API", config={
				"webhook_endpoint": _ENDPOINT,
				"capture": [{"path": "body.data.id", "variable": "patient_id"}],
				"success_when": success_when,
			}, edges={"succeeded": "won", "failed": "lost"}),
			fx.node("won", "Terminal"),
			fx.node("lost", "Terminal"),
		])
		self.built.append(name)
		frappe.db.commit()
		with patch(_HANDLER, return_value=response):
			run = fx.start_journey(workflow, self.lead.name, "start")
			interpreter.advance(frappe.get_doc(fx.JOURNEY_DT, run.name))
		frappe.db.commit()
		return frappe.db.get_value(fx.JOURNEY_DT, run.name, ["status", "current_node"], as_dict=True)

	def test_a_lead_field_decides_success_instead_of_killing_the_journey(self):
		"""The likeliest pick, and the one that used to be fatal — the lead is the subject."""
		row = self._land(
			"call-api-when-lead", {"type": "rule", "field": "crm_lead.status", "operator": "is", "value": "New"},
			_response(200, True, {"data": {"id": "P-1"}}),
		)
		self.assertEqual(row.status, "Done", "a predicate on the subject must not fail the journey")
		self.assertEqual(row.current_node, "won")

	def test_a_captured_variable_decides_success(self):
		"""`capture` is the declared way to read a body, and its names are offered here — now judged too."""
		row = self._land(
			"call-api-when-capture", {"type": "rule", "field": "call.patient_id", "operator": "is set"},
			_response(200, True, {"data": {"id": "P-1"}}),
		)
		self.assertEqual(row.status, "Done")
		self.assertEqual(row.current_node, "won")

	def test_a_captured_variable_that_did_not_arrive_takes_the_failed_edge(self):
		"""A 200 carrying nothing useful is the failure the author declared, still not an exception."""
		row = self._land(
			"call-api-when-capture-empty", {"type": "rule", "field": "call.patient_id", "operator": "is set"},
			_response(200, True, {"data": {}}),
		)
		self.assertEqual(row.status, "Done", "a declared failure is data, not a dead journey")
		self.assertEqual(row.current_node, "lost")

	def test_the_response_itself_still_decides(self):
		"""The one shape that always worked, kept working."""
		row = self._land(
			"call-api-when-status", {"type": "rule", "field": "call.status", "operator": "is", "value": 201},
			_response(201, True, {"data": {"id": "P-1"}}),
		)
		self.assertEqual(row.current_node, "won")
