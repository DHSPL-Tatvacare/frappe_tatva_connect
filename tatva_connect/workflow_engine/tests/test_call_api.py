# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A Call API node captures its response and routes on it.

The point of W7 is that a workflow can ACT on what an endpoint said back. That needs three things to be
true, and each is tested here against a real graph rather than by calling the helper directly:

  * the response lands in named run variables, so downstream nodes read them like any other value;
  * the node leaves by `succeeded` or `failed`, so the author routes by wiring rather than by writing
    a condition;
  * a failure is DATA, not an exception — a refused connection must take the `failed` edge, not kill
    the run. A node that dies on a 500 is a workflow that cannot handle the case it exists to handle.

The HTTP call itself is the one thing stubbed: this suite is about what the engine does with an answer,
and reaching a real endpoint would make it a network test. `_call_endpoint` is the seam — everything
above it (capture, predicate, routing, state) runs for real.
"""
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.tests.authz.grains import assert_masters_exist
from tatva_connect.workflow_engine import interpreter
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
		cls._was_armed = fx.arm_engine(True)
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
		fx.arm_engine(bool(cls._was_armed))
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
		for run in frappe.get_all(fx.RUN_DT, filters={"workflow": self.workflow.name}, pluck="name"):
			frappe.db.delete(fx.STEP_LOG_DT, {"workflow_run": run})
		frappe.db.delete(fx.RUN_DT, {"workflow": self.workflow.name})
		frappe.db.commit()

	def _run_with(self, response):
		with patch(_HANDLER, return_value=response):
			run = fx.start_run(self.workflow, self.lead.name, "start")
			interpreter.advance(frappe.get_doc(fx.RUN_DT, run.name))
		frappe.db.commit()
		row = frappe.db.get_value(
			fx.RUN_DT, run.name, ["status", "current_node", "state_json"], as_dict=True
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
		self.assertEqual(row.status, "Done", "a failed call must not fail the run")
		self.assertEqual(state.get("http_status"), 0)

	def test_the_step_log_records_what_the_call_answered(self):
		"""An outbound call that left no trace is unauditable — the run must say what came back."""
		self._run_with(_response(500, False, {"msg": "boom"}))
		detail = {entry.node_id: entry.detail for entry in fx.logs(
			frappe.get_all(fx.RUN_DT, filters={"workflow": self.workflow.name}, pluck="name")[0]
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
