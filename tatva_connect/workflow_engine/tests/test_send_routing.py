# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A send that did not reach the patient is DATA the author routes — and its template values are DECLARED.

Three defects, one suite, because they are the same defect wearing three coats: a declaration and a
runtime that disagreed, with publish siding with the wrong one.

1. `Send WhatsApp` and `Send Email` declared NO outputs, so they defaulted to `next` and every failure
   mode raised. A lead with no `mobile_no` — an ordinary state of an ordinary patient record — reached
   `interpreter.advance` as an exception and marked the whole run **Failed**. A real production flow
   (WhatsApp → wait → call → branch → WhatsApp) wires a failure edge on every messaging node, and ours
   could not express one.

2. A WhatsApp template's placeholders were an ENTIRELY UNDECLARED read surface. `sends.send_whatsapp`
   did `ctx.get(name)` for every placeholder the provider declared; the node's only declared param was a
   Link to the template, so `contract.reads_of` saw nothing and the publish gate checked nothing. A
   template with a `{{patient_name}}` slot that nothing upstream produces published GREEN and then sent
   "Hi ," to a real patient, silently, with nothing in the step log.

3. `email_recipient` is declared `Variable, free_text`, and `graph._reference_problems` ENFORCES that a
   bare name is produced upstream — while the handler passed the raw string to `frappe.sendmail` as the
   address. So publish actively CERTIFIED a node that queued mail to the literal string
   `"escalation_email"`.

Every assertion here is an OUTCOME — which edge the run really left by, what the publish gate really
returned, what address really reached the mail boundary. Never a call count: a mocked assertion on
`mock_send.called` is exactly what let three months of file bugs through this repo.

Nothing here sends anything. The sends switch is dormant by default and this suite never writes it; the
one test that needs a live path patches `sends_enabled` and the mail boundary in-process, so no bench
config is touched and no message can leave.
"""
import json
from typing import ClassVar
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.automation import actions, sends
from tatva_connect.tests.authz.grains import assert_masters_exist
from tatva_connect.workflow_engine import contract, graph, interpreter, refs, registry
from tatva_connect.workflow_engine.tests import fixtures as fx

_WORKFLOW = "send-routing-probe"


def _node(node_id, node_type, config=None, edges=None):
	return {
		"node_id": node_id,
		"node_type": node_type,
		"config_json": json.dumps(config or {}),
		"edges": [{"from_output": o, "to_node": t} for o, t in (edges or {}).items()],
	}


def _trigger(to="n1"):
	return _node("start", "Trigger", {"subject_doctype": "CRM Lead", "event": "Created"}, {"next": to})


def _messages(nodes):
	return " | ".join(p["message"] for p in graph.problems(nodes, entry_node="start"))


class _FakeAdapter:
	"""The vendor boundary, and ONLY the vendor boundary. `template_variables` is a live WATI/mirror read;
	everything this suite is about — what the declared rows resolve to, which slots are missing, what goes
	on the wire — runs for real against it."""

	def __init__(self, names):
		self._names = names

	def template_variables(self, account, template):
		return list(self._names)


class TestASendFailureRoutesInsteadOfKillingTheRun(FrappeTestCase):
	"""Defect 1, end to end through the real interpreter. The outcome asserted is the node the run
	finished on — not that a function was called."""

	@classmethod
	def setUpClass(cls):
		assert_masters_exist()
		fx.purge(_WORKFLOW)
		cls._was_armed = fx.arm_engine(True)
		cls.lead = fx.make_lead()
		cls.workflow = fx.make_workflow(_WORKFLOW, [
			fx.trigger(to="wa"),
			fx.node("wa", "Send WhatsApp", config={"whatsapp_template": "probe"},
			        edges={sends.SENT: "sent_end", sends.FAILED: "failed_end"}),
			fx.node("sent_end", "Terminal"),
			fx.node("failed_end", "Terminal"),
		])
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		fx.arm_engine(bool(cls._was_armed))
		fx.purge(_WORKFLOW)
		frappe.delete_doc("CRM Lead", cls.lead.name, force=True, ignore_permissions=True)
		frappe.db.commit()

	def setUp(self):
		self._clear_runs()

	def tearDown(self):
		self._clear_runs()

	def _clear_runs(self):
		for run in frappe.get_all(fx.RUN_DT, filters={"workflow": self.workflow.name}, pluck="name"):
			frappe.db.delete(fx.STEP_LOG_DT, {"workflow_run": run})
		frappe.db.delete(fx.RUN_DT, {"workflow": self.workflow.name})
		frappe.db.commit()

	def _walk(self):
		run = fx.start_run(self.workflow, self.lead.name, "start")
		interpreter.advance(frappe.get_doc(fx.RUN_DT, run.name))
		return frappe.get_doc(fx.RUN_DT, run.name)

	def test_the_sends_switch_is_dormant_so_this_suite_sends_nothing(self):
		"""A control. If the switch were armed on this bench the two tests below would mean something
		different, and one of them would try to reach WATI."""
		self.assertFalse(sends.sends_enabled(), "the sends switch is armed on this bench — it ships OFF")

	def test_a_lead_with_no_phone_number_leaves_by_the_failed_edge(self):
		"""THE headline. "This patient has no phone number" is an ordinary data state; before this it
		raised, reached `advance`, and marked the run Failed — killing the journey."""
		frappe.db.set_value("CRM Lead", self.lead.name, "mobile_no", "")
		frappe.db.commit()

		run = self._walk()

		self.assertEqual(run.status, "Done", "an ordinary data state must not fail the run")
		self.assertEqual(run.current_node, "failed_end", "the run must leave the send by its `failed` edge")
		details = " ".join(log["detail"] or "" for log in fx.logs(run.name))
		self.assertIn("mobile_no", details, "the step log must say WHY the send did not happen")

	def test_a_dormant_send_leaves_by_the_sent_edge(self):
		"""The judgement call, locked. A suppressed send is not a failed send: routing dormant to `failed`
		would make every bench walk its escalation branch for patients nothing was attempted for, so the
		shipped-OFF switch would change the SHAPE of the journey rather than just stop a message."""
		frappe.db.set_value("CRM Lead", self.lead.name, "mobile_no", "9800000001")
		frappe.db.commit()

		run = self._walk()

		self.assertEqual(run.status, "Done")
		self.assertEqual(run.current_node, "sent_end", "a dormant send is suppressed, not failed")
		details = " ".join(log["detail"] or "" for log in fx.logs(run.name))
		self.assertIn(sends.DORMANT_MARKER, details, "the audit must prove the send was suppressed")


class TestTheSendVerbsRouteThroughTheOneMechanism(FrappeTestCase):
	"""B3: a declaration nothing checks is a lie waiting to happen.

	`interpreter._verb_output` falls back to `next` when a handler names no output — and neither send verb
	declares `next` any more. So a handler that forgot to set `_output` would route the run to a node that
	does not exist, silently. This locks the declaration against the runtime for every verb that declares
	custom outputs, iterated from `VERBS` so a verb added tomorrow is covered tomorrow.
	"""

	@classmethod
	def setUpClass(cls):
		assert_masters_exist()
		cls.lead = fx.make_lead()
		frappe.db.set_value("CRM Lead", cls.lead.name, "mobile_no", "9800000001")
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		frappe.delete_doc("CRM Lead", cls.lead.name, force=True, ignore_permissions=True)
		frappe.db.commit()

	_CONFIGS: ClassVar[dict] = {
		"Send WhatsApp": {"whatsapp_template": "probe"},
		"Send Email": {"email_recipient": "ops@tatvacare.invalid", "email_subject": "s", "email_body": "b"},
	}

	def test_a_verb_that_does_not_declare_next_always_names_its_output(self):
		for verb, config in self._CONFIGS.items():
			with self.subTest(verb=verb):
				declared = registry.outputs_for(verb, config)
				self.assertNotIn(
					"next", declared, f"{verb} declares `next`, so this lock no longer applies to it"
				)
				state = refs.Values()
				node = frappe._dict({
					"node_id": "w", "node_type": verb,
					"config_json": frappe.as_json(config), "edges": [],
				})
				interpreter._run_verb(node, self.lead.name, self.lead, state, fx.AXES)
				self.assertIn(
					state.get(refs.OUTPUT), declared,
					f"{verb} left `{refs.OUTPUT}` at {state.get(refs.OUTPUT)!r}, which it does not declare — "
					"the interpreter would fall back to `next` and route the run nowhere",
				)

	def test_every_send_verb_declares_both_a_sent_and_a_failed_edge(self):
		"""The production flow this work exists to make buildable wires BOTH on every messaging node."""
		for verb in self._CONFIGS:
			with self.subTest(verb=verb):
				self.assertEqual(actions.VERBS[verb]["outputs"], [sends.SENT, sends.FAILED])


class TestTemplateVariablesAreDeclared(FrappeTestCase):
	"""Defect 2 — the worst of the three, because nothing anywhere could see it."""

	def test_a_from_context_row_is_a_reference(self):
		found = contract.reads_of("Send WhatsApp", {
			"whatsapp_template": "t",
			"template_values": [{"name": "1", "mode": "From Context", "value": "patient_name"}],
		})
		self.assertEqual([r["name"] for r in found], ["patient_name"])

	def test_a_literal_row_is_not_a_reference(self):
		"""The negative half. An author filling a slot with typed text names no variable, and demanding an
		upstream producer for it would reject every correctly-authored literal."""
		found = contract.reads_of("Send WhatsApp", {
			"whatsapp_template": "t",
			"template_values": [{"name": "1", "mode": "Literal", "value": "Namaste"}],
		})
		self.assertEqual(found, [])

	def test_the_reference_says_which_control_carries_it(self):
		found = contract.reads_of("Send WhatsApp", {
			"whatsapp_template": "t",
			"template_values": [{"name": "1", "mode": "From Context", "value": "nope"}],
		})
		self.assertEqual(found[0]["field"], "template_values")

	def test_publish_refuses_a_template_value_nothing_produces(self):
		"""THE red. Before the declaration existed this graph published clean, and the patient got "Hi ,"."""
		nodes = [
			_trigger(to="wa"),
			_node("wa", "Send WhatsApp", {
				"whatsapp_template": "t",
				"template_values": [{"name": "1", "mode": "From Context", "value": "patient_name"}],
			}, {sends.SENT: "end", sends.FAILED: "end"}),
			_node("end", "Terminal"),
		]
		self.assertIn("patient_name", _messages(nodes))

	def test_the_same_mapping_is_accepted_when_an_upstream_node_produces_it(self):
		"""The negative half, and the one that matters most: a check authors learn to distrust is worse
		than no check."""
		nodes = [
			_trigger(to="sv"),
			_node("sv", "Set Variables", {"assign": '{"patient_name": "Asha"}'}, {"next": "wa"}),
			_node("wa", "Send WhatsApp", {
				"whatsapp_template": "t",
				"template_values": [{"name": "1", "mode": "From Context", "value": "sv.patient_name"}],
			}, {sends.SENT: "end", sends.FAILED: "end"}),
			_node("end", "Terminal"),
		]
		self.assertNotIn("sv.patient_name", _messages(nodes))


class TestTemplateParametersComeOnlyFromTheDeclaredRows(FrappeTestCase):
	"""What actually goes on the wire. Driven at `sends._template_parameters` rather than through a live
	send: a live send needs a WhatsApp Account, grain routing and an armed switch — operator config this
	suite is forbidden to leave on the bench. The adapter is the one thing faked."""

	def _build(self, names, rows, ctx):
		return sends._template_parameters(
			_FakeAdapter(names), None, frappe._dict({"name": "t"}), rows, ctx
		)

	def test_a_from_context_row_is_filled_from_run_state(self):
		parameters, blank = self._build(
			["patient_name"],
			[{"name": "patient_name", "mode": "From Context", "value": "who"}],
			{"who": "Asha"},
		)
		self.assertEqual(parameters, [{"name": "patient_name", "value": "Asha"}])
		self.assertEqual(blank, [])

	def test_a_literal_row_is_sent_as_typed(self):
		parameters, blank = self._build(
			["greeting"], [{"name": "greeting", "mode": "Literal", "value": "Namaste"}], {},
		)
		self.assertEqual(parameters, [{"name": "greeting", "value": "Namaste"}])
		self.assertEqual(blank, [])

	def test_run_state_is_no_longer_read_by_placeholder_name(self):
		"""The defect itself, stated as a rule. `ctx["patient_name"]` must NOT reach the wire just because
		the placeholder happens to share its name — that implicit read is what nothing could see."""
		with self.assertRaises(ValueError) as caught:
			self._build(["patient_name"], [], {"patient_name": "Asha"})
		self.assertIn("no value declared", str(caught.exception))

	def test_a_placeholder_with_no_declared_row_raises(self):
		"""AUTHOR ERROR, not data: no patient's record can produce it and it is wrong for every record
		equally. Routing it to `failed` would hide a workflow that could never have sent at all."""
		with self.assertRaises(ValueError) as caught:
			self._build(
				["patient_name", "next_visit"],
				[{"name": "patient_name", "mode": "Literal", "value": "Asha"}],
				{},
			)
		self.assertIn("next_visit", str(caught.exception))

	def test_a_declared_row_that_resolves_blank_is_reported_not_sent(self):
		"""DATA, not author error: this patient simply has no diagnosis recorded yet. It routes to `failed`
		and — the whole point — nothing goes out with a blank in it."""
		parameters, blank = self._build(
			["diagnosis"], [{"name": "diagnosis", "mode": "From Context", "value": "dx"}], {"dx": None},
		)
		self.assertEqual(blank, ["diagnosis"])
		self.assertEqual(parameters, [], "a message with a blank slot must not be assembled at all")


class TestRecipientDeclarationMatchesRuntime(FrappeTestCase):
	"""Defect 3 — the declaration said Variable, the runtime meant literal, and publish blessed the gap."""

	def test_a_variable_recipient_resolves_to_the_address_it_names(self):
		"""THE red. `escalation_email` used to be handed to `frappe.sendmail` verbatim."""
		self.assertEqual(
			sends.resolve_recipient("sv.escalation_email", {"sv.escalation_email": "asm@tatvacare.invalid"}),
			"asm@tatvacare.invalid",
		)

	def test_a_literal_address_still_works(self):
		"""Most authors type one, and an address cannot be a variable name."""
		self.assertEqual(sends.resolve_recipient("ops@tatvacare.invalid", {}), "ops@tatvacare.invalid")

	def test_the_gate_and_the_runtime_use_the_ONE_predicate(self):
		"""The divergence lock (B3/B7). For every shape an author can type, "does publish demand an
		upstream producer" and "does the runtime resolve it from run state" must be the same answer. They
		were opposite answers for exactly one shape — the bare name — and that is the whole defect."""
		for value in ("sv.escalation_email", "escalation_email", "ops@tatvacare.invalid",
		              "asm.north@x.co.in", "_x", "9812345678"):
			with self.subTest(value=value):
				gate_treats_as_reference = bool(
					contract.reads_of("Send Email", {"email_recipient": value, "email_subject": "s"})
				)
				runtime_resolved = sends.resolve_recipient(value, {}) is None
				self.assertEqual(
					gate_treats_as_reference, runtime_resolved,
					f"publish and the runtime disagree about {value!r} — the gate would certify a node "
					"the sender then gets wrong",
				)

	def test_the_resolved_address_is_what_reaches_the_mail_boundary(self):
		"""Not "sendmail was called" — WHICH ADDRESS it was handed. `sends_enabled` is patched in-process
		rather than switched on the bench, so nothing is left behind and no mail can leave."""
		seen = {}

		def _capture(**kwargs):
			seen.update(kwargs)

		with patch.object(sends, "sends_enabled", return_value=True), \
		     patch.object(frappe, "sendmail", _capture):
			output, _marker = sends.send_email(
				"LEAD-PROBE", "sv.escalation_email", "s", "b", {"sv.escalation_email": "asm@tatvacare.invalid"},
			)

		self.assertEqual(output, sends.SENT)
		self.assertEqual(
			seen.get("recipients"), ["asm@tatvacare.invalid"],
			"the variable's VALUE must be the address — not the variable's name",
		)

	def test_a_recipient_variable_that_resolves_to_nothing_routes_to_failed(self):
		"""DATA: the upstream node produced no address for this record. The run must not die of it."""
		output, marker = sends.send_email("LEAD-PROBE", "sv.escalation_email", "s", "b", {})
		self.assertEqual(output, sends.FAILED)
		self.assertIn("sv.escalation_email", marker)

	def test_a_blank_recipient_on_the_node_still_raises(self):
		"""AUTHOR ERROR stays loud. The author left the box empty; no data state can produce that."""
		with self.assertRaises(ValueError):
			sends.send_email("LEAD-PROBE", "", "s", "b", {})
