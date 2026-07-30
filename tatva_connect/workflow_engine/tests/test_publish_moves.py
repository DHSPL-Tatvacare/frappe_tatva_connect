# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""THREE CHECKS MOVE FROM RUNTIME TO PUBLISH — the author learns at publish, not from a live patient.

Each rule was already written and already fired, but at RUNTIME, on a real lead, days after the author
left the canvas. They are MOVES, not new logic: the publish gate now calls the SAME shared function the
runtime does, so a workflow that would have died mid-run is refused at publish instead.

  1. Every template placeholder has a declared mapping row  — was `sends._template_parameters` raising.
  2. The picked template belongs to the account the grain routes to — was `sends.send_whatsapp` raising.
     Publish can only see this when the workflow's grain pins ONE account; a blank/ambiguous grain still
     resolves per-lead at runtime, so the runtime check STAYS as the backstop and both call the ONE
     `sends.template_account_mismatch`.
  3. A Call API endpoint exists — was `_action_call_api` raising.

A check that cannot answer (grain pins no account, template not fetched) ABSTAINS — it never invents a
refusal the author has no way to fix.
"""
import json
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.automation import sends
from tatva_connect.tests.authz.grains import assert_masters_exist
from tatva_connect.workflow_engine import graph
from tatva_connect.workflow_engine.tests import fixtures as fx

_ACCOUNT_A = "publish-moves-account-a"


def _node(node_id, node_type, config=None, edges=None):
	return {
		"node_id": node_id, "node_type": node_type,
		"config_json": json.dumps(config or {}),
		"edges": [{"from_output": o, "to_node": t} for o, t in (edges or {}).items()],
	}


def _trigger(to="wa"):
	"""The authored shape graph.problems reads — config_json + list edges — carrying the fixture grain so
	the account resolver has one to route on."""
	return _node("start", "Trigger", {
		"subject_doctype": "CRM Lead", "event": "Created",
		"vertical": fx.GRAIN["vertical"], "group": fx.GRAIN["group"], "program": fx.GRAIN["program"],
	}, {"next": to})


def _messages(nodes):
	return " | ".join(p["message"] for p in graph.problems(nodes, entry_node="start"))


def _problems(nodes):
	return graph.problems(nodes, entry_node="start")


class TestPublishRefusesAnUnmappedPlaceholder(FrappeTestCase):
	"""Move 1. A template placeholder with no declared row — author error, wrong for every record — is
	refused at publish. It used to publish green and raise on the first real send."""

	def _send_graph(self, verb, template_field, values):
		config = {template_field: "t", "template_values": values}
		config["contact_number" if verb == "Send WhatsApp" else "email_recipient"] = "crm_lead.mobile_no"
		return [
			_trigger(to="wa"),
			_node("wa", verb, config, {sends.SENT: "end", sends.FAILED: "end"}),
			_node("end", "Terminal"),
		]

	def test_whatsapp_a_placeholder_with_no_row_is_refused_at_publish(self):
		with patch.object(sends, "whatsapp_template_slots", return_value=["patient_name"]):
			found = _messages(self._send_graph("Send WhatsApp", "whatsapp_template", []))
		self.assertIn("patient_name", found, "publish must name the unmapped placeholder")

	def test_email_a_slot_with_no_row_is_refused_at_publish(self):
		with patch.object(sends, "email_template_slots", return_value=["first_name"]):
			found = _messages(self._send_graph("Send Email", "email_template", []))
		self.assertIn("first_name", found, "publish must name the unmapped slot")

	def test_voice_an_agent_placeholder_with_no_row_is_refused_at_publish(self):
		"""VOICE-1. The gate kept its OWN map of which verbs have slots — `Send WhatsApp` and `Send Email`
		— while the verb registry declared `slots_from`/`slots_method` on AI Voice Call's `agent_values`
		too. A voice node with an unmapped agent placeholder therefore published green and raised in
		`sends._agent_variables` on the first real lead: a journey marked Failed on a live patient, which is
		the exact failure this move exists to prevent.

		Patched at `voice.api.agent_slots` because that is what the DECLARATION names — if the dispatch ever
		stops reading the declaration, this stops being patched and goes red for the right reason.
		"""
		from tatva_connect.voice import api as voice_api

		nodes = [
			_trigger(to="vc"),
			_node("vc", "AI Voice Call", {
				"connection": "acct-1", "agent_id": "agent-1", "agent_values": [],
				"contact_number": "crm_lead.mobile_no",
			}, {sends.PLACED: "end", sends.FAILED: "end"}),
			_node("end", "Terminal"),
		]
		with patch.object(voice_api, "agent_slots", return_value=["customer_name"]):
			found = _messages(nodes)
		self.assertIn("customer_name", found, "publish must name the unmapped agent placeholder")

	def test_voice_the_declared_sibling_args_really_reach_the_slots_method(self):
		"""`agent_values` is the only slots field declaring `slots_args` — the account its agent lives on.
		Dropping it would still find the slots for a mock and return nothing from the real provider, so the
		call itself is asserted, not just its effect."""
		from tatva_connect.voice import api as voice_api

		nodes = [
			_trigger(to="vc"),
			_node("vc", "AI Voice Call", {
				"connection": "acct-1", "agent_id": "agent-1", "agent_values": [],
				"contact_number": "crm_lead.mobile_no",
			}, {sends.PLACED: "end", sends.FAILED: "end"}),
			_node("end", "Terminal"),
		]
		with patch.object(voice_api, "agent_slots", return_value=[]) as slots:
			_messages(nodes)
		slots.assert_called_once_with("agent-1", account="acct-1")

	def test_a_fully_mapped_voice_call_publishes_clean(self):
		"""The negative half for voice, so the fix cannot be "refuse every voice node"."""
		nodes = [
			_trigger(to="vc"),
			_node("vc", "AI Voice Call", {
				"connection": "acct-1", "agent_id": "agent-1",
				"agent_values": [{"name": "customer_name", "mode": "From Context", "value": "crm_lead.lead_name"}],
				"contact_number": "crm_lead.mobile_no",
			}, {sends.PLACED: "end", sends.FAILED: "end"}),
			_node("end", "Terminal"),
		]
		from tatva_connect.voice import api as voice_api

		with patch.object(voice_api, "agent_slots", return_value=["customer_name"]):
			found = _messages(nodes)
		self.assertNotIn("customer_name", found, "a fully-mapped voice call must not be refused")

	def test_the_gate_keeps_no_list_of_which_verbs_have_slots(self):
		"""THE STRUCTURAL LOCK, and the reason VOICE-1 could happen at all.

		The gate held `{"Send WhatsApp": …, "Send Email": …}` — a second, shorter copy of a question the
		verb registry already answers, and AI Voice fell through the gap. So the lock forbids the SHAPE:
		this function may not name a node type at all. It derives the vocabulary from `NODE_TYPES`, so a
		verb added later cannot be hardcoded here either, with nobody editing this file.
		"""
		import inspect

		from tatva_connect.workflow_engine import graph as graph_module
		from tatva_connect.workflow_engine import registry

		source = inspect.getsource(graph_module._template_mapping_problems)
		body = "".join(
			line for line in source.splitlines(keepends=True)
			if not line.lstrip().startswith("#")
		)
		_, _, body = body.partition('"""')
		_, _, body = body.partition('"""')  # the docstring EXPLAINS the old map; only code is judged
		named = sorted(t for t in registry.NODE_TYPES if t in body)
		self.assertEqual(named, [], f"the gate is re-deciding which verbs have slots: {named}")

	def test_every_field_that_declares_slots_is_reachable_through_the_declaration(self):
		"""The other half: the lock above passes if the function does nothing at all. This asserts the
		declarations really carry what the dispatch reads, and that AI Voice is among them."""
		from tatva_connect.workflow_engine import registry

		declaring = [
			(node_type, f["name"], f.get("slots_method"))
			for node_type in registry.NODE_TYPES
			for f in registry.config_fields(node_type)
			if f.get("slots_from")
		]
		self.assertTrue(declaring, "nothing declares slots — this would pass by testing nothing")
		self.assertEqual(
			[(t, n) for t, n, m in declaring if not m], [],
			"a field naming a slots SOURCE with no method to enumerate them cannot be gated",
		)
		self.assertIn(
			("AI Voice Call", "agent_values", "tatva_connect.voice.api.agent_slots"), declaring,
			"the verb VOICE-1 was about",
		)

	def test_a_fully_mapped_send_publishes_clean(self):
		"""The negative half — a check authors learn to distrust is worse than no check."""
		nodes = [
			_trigger(to="sv"),
			_node("sv", "Set Variables", {"assign": '{"patient_name": "Asha"}'}, {"next": "wa"}),
			_node("wa", "Send WhatsApp", {
				"whatsapp_template": "t", "contact_number": "crm_lead.mobile_no",
				"template_values": [{"name": "patient_name", "mode": "From Context", "value": "sv.patient_name"}],
			}, {sends.SENT: "end", sends.FAILED: "end"}),
			_node("end", "Terminal"),
		]
		with patch.object(sends, "whatsapp_template_slots", return_value=["patient_name"]):
			found = _messages(nodes)
		self.assertNotIn("patient_name", found, "a fully-mapped send must not be refused")


class TestPublishRefusesATemplateAccountMismatch(FrappeTestCase):
	"""Move 2. When the workflow's grain pins ONE account, a template belonging to a DIFFERENT account is a
	static author error for a whole grain — refused at publish. When the grain pins no account, publish
	ABSTAINS and the runtime backstop remains."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		assert_masters_exist()
		if frappe.db.exists("WhatsApp Account", _ACCOUNT_A):
			frappe.delete_doc("WhatsApp Account", _ACCOUNT_A, force=True, ignore_permissions=True)
		cls.account = frappe.get_doc({
			"doctype": "WhatsApp Account", "account_name": _ACCOUNT_A, "status": "Active",
			"url": "https://live-mt-server.wati.io/000009", "token": "publish-moves-token",
			"custom_provider": "WATI", "custom_wati_channel_number": "919099000123",
		}).insert(ignore_permissions=True).name
		name = "publish-moves-template-en"
		if frappe.db.exists("WhatsApp Templates", name):
			frappe.delete_doc("WhatsApp Templates", name, force=True, ignore_permissions=True)
		doc = frappe.new_doc("WhatsApp Templates")
		doc.update({
			"template_name": "publish-moves-template", "template": "<p>Hi</p>", "language_code": "en",
			"category": "UTILITY", "whatsapp_account": cls.account, "actual_name": "publish-moves-template",
			"status": "APPROVED",
		})
		doc.name = name
		doc.db_insert()
		cls.template = doc.name
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		frappe.delete_doc("WhatsApp Templates", cls.template, force=True, ignore_permissions=True)
		frappe.delete_doc("WhatsApp Account", cls.account, force=True, ignore_permissions=True)
		frappe.db.commit()
		super().tearDownClass()

	def _graph(self):
		return [
			_trigger(to="wa"),
			_node("wa", "Send WhatsApp", {
				"whatsapp_template": self.template, "contact_number": "crm_lead.mobile_no",
				"template_values": [],
			}, {sends.SENT: "end", sends.FAILED: "end"}),
			_node("end", "Terminal"),
		]

	def test_a_template_for_another_account_is_refused_when_the_grain_pins_one(self):
		from tatva_connect.whatsapp import routing

		with patch.object(routing, "resolve_account_for_grain", return_value="some-other-account"), \
		     patch.object(sends, "whatsapp_template_slots", return_value=[]):
			found = _messages(self._graph())
		self.assertIn("some-other-account", found, "publish must name the account the grain routes to")
		self.assertIn(self.account, found, "publish must name the account the template belongs to")

	def test_a_matching_account_publishes_clean(self):
		from tatva_connect.whatsapp import routing

		with patch.object(routing, "resolve_account_for_grain", return_value=self.account), \
		     patch.object(sends, "whatsapp_template_slots", return_value=[]):
			found = _messages(self._graph())
		self.assertNotIn("belongs to account", found, "a matching template/account must not be refused")

	def test_an_unresolvable_grain_abstains(self):
		"""The line that must NOT move: a grain that pins no account is a per-lead runtime question. Publish
		abstains rather than inventing a refusal the author cannot act on."""
		from tatva_connect.whatsapp import routing

		with patch.object(routing, "resolve_account_for_grain", return_value=None), \
		     patch.object(sends, "whatsapp_template_slots", return_value=[]):
			found = _messages(self._graph())
		self.assertNotIn("belongs to account", found, "publish must not refuse when it cannot resolve the account")


class TestPublishRefusesAMissingEndpoint(FrappeTestCase):
	"""Move 3. A Call API node whose curated Webhook was deleted is refused at publish, not at the first run
	that tries to call it."""

	def _graph(self, endpoint):
		return [
			_trigger(to="api"),
			_node("api", "Call API", {"webhook_endpoint": endpoint}, {"succeeded": "end", "failed": "end"}),
			_node("end", "Terminal"),
		]

	def test_a_missing_endpoint_is_refused_at_publish(self):
		found = _messages(self._graph("no-such-webhook-exists"))
		self.assertIn("no-such-webhook-exists", found, "publish must name the endpoint that does not exist")

	def test_a_blank_endpoint_is_a_required_field_not_a_missing_one(self):
		"""The blank case belongs to the `reqd` rule (W2.2), not this move — so this move must ABSTAIN on a
		blank endpoint rather than claim it 'does not exist'."""
		problems = _problems(self._graph(""))
		self.assertFalse(
			any(p.get("code") == "endpoint.missing" for p in problems),
			"a blank endpoint must not be reported as a missing one",
		)
