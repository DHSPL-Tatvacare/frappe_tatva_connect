# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A NODE'S WAITABLE EVENTS COME FROM THE ADAPTER'S DECLARATION, AND A RECEIPT WAKES THE RUN THAT SENT IT.

Two halves of one contract.

W1.1 — THE DECLARATION. `channels/event.OUTCOMES` names six things a WhatsApp provider may report, and
every adapter's `Declaration` states the subset it can TRUTHFULLY emit. WATI declares all six. The
workflow node ignored every word of it and offered two, so an author could not branch on `delivered`
even though the provider reports it 14 seconds later on live traffic.

They land on the verb's `outcomes` — the events a downstream Wait may name — and NOT on its `outputs`.
`sent`/`failed` are the synchronous answer to "did the send attempt happen"; `delivered`/`read`/
`replied`/`clicked` arrive seconds later down a completely different path. A synchronous `delivered`
handle would have told the canvas the send node knows immediately, which is false.

W1.2 — THE BRIDGE, and the identity problem at its heart. The engine mints `run::node`; the provider
mints `localMessageId`. **They are different values, and the run parks before the provider id exists** —
the send is deferred past the segment commit, so at park time there is nothing to park on but the token.
The two identities therefore meet on the `WhatsApp Message` row: the send job writes both, the status
ingest reads them back.

THE TEST THAT MATTERS IS TWO RUNS ON ONE LEAD. Correlating a receipt by lead alone would wake whichever
run answered first — which is a wrong clinical action, not a cosmetic bug, when one patient is in two
journeys. `TestAReceiptWakesOnlyTheRunThatSentThatMessage` is the whole point of this module; everything
else supports it.

NOTHING IS SENT. The sends switch stays OFF and every provider call is patched at the transport boundary.
The engine switch is armed in-process where a signal must actually be delivered, and restored — and
because `fx.arm_engine` is NOT abort-safe, the run that produced this file also checked both switches
directly afterwards rather than trusting teardown.
"""
import hashlib
import unittest
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.automation import actions, sends
from tatva_connect.channels import contract, resolve
from tatva_connect.tests.authz.grains import GRAINS, assert_masters_exist
from tatva_connect.whatsapp import ingest, transport, wati
from tatva_connect.workflow_engine import registry
from tatva_connect.workflow_engine.tests import fixtures as fx

_WORKFLOW = "channel-outcome-probe"
_ACCOUNT = "Channel-outcome-probe-account"
_TEMPLATE = "channel-outcome-probe"
_GRAIN = GRAINS[2]

_SEND_VERB = "Send WhatsApp"
_DELIVERED = "whatsapp.delivered"


def _status_payload(message_id, conversation="conv-probe"):
	"""A real `sentMessageDELIVERED_v2` shape. Only the `_v2` events carry `localMessageId` — verified
	12/12 on live traffic — which is why the bridge correlates on it and on nothing else."""
	return {
		"eventType": "sentMessageDELIVERED_v2",
		"localMessageId": message_id,
		"id": f"wati-{message_id}",
		"conversationId": conversation,
		"statusString": "DELIVERED",
		"timestamp": "1750000000",
	}


class TestOutcomesAreDerivedFromTheAdapterDeclaration(FrappeTestCase):
	"""W1.1, both directions. The node offers what the PROVIDER declared, never a list typed into a verb."""

	def test_wati_declaring_delivered_gives_the_node_a_waitable_delivered(self):
		"""THE red. The verb declared no outcomes at all, so a Wait could name nothing after a send."""
		self.assertIn("delivered", wati.DECLARATION.outcomes, "the premise: WATI really does declare it")
		self.assertIn(_DELIVERED, actions.outcomes_of(_SEND_VERB))

	def test_an_adapter_that_cannot_report_it_does_not_offer_it(self):
		"""The other direction, and the reason this is a derivation rather than a longer hardcoded list.
		Same code, same node, a poorer provider — and the outcome is gone."""
		poorer = contract.declare(
			channel="whatsapp", provider="WATI", account_doctype="WhatsApp Account",
			outcomes={"sent", "failed"}, capabilities={"templates"},
			number_format=contract.E164_PLAIN,
		)
		with patch.object(wati, "DECLARATION", poorer):
			offered = actions.outcomes_of(_SEND_VERB)

		self.assertNotIn(_DELIVERED, offered, "an outcome nothing can report must not be offerable")
		self.assertEqual(offered, [], "sent/failed are synchronous outputs, so nothing is left to wait on")

	def test_the_synchronous_outputs_are_never_offered_as_waitable(self):
		"""Not taxonomy — a race. The send path returned `sent` to the run before any provider was called,
		and a `sent` status can arrive before the row the bridge correlates through is even committed. A
		Wait on `sent` could never be woken reliably, so it must not be declarable."""
		offered = actions.outcomes_of(_SEND_VERB)
		self.assertNotIn("whatsapp.sent", offered)
		self.assertNotIn("whatsapp.failed", offered)
		self.assertEqual(registry.outputs_for(_SEND_VERB), [sends.SENT, sends.FAILED], "outputs untouched")

	def test_the_offered_set_is_the_union_across_the_channels_adapters(self):
		"""The resolution rule stated as a test: authoring has no lead, so no account and no adapter — the
		union is the honest static answer to what a send on this channel can ever report."""
		expected = {name for name in resolve.outcomes_for_channel("whatsapp")
		            if name.split(".", 1)[1] not in {"sent", "failed"}}
		self.assertEqual(set(actions.outcomes_of(_SEND_VERB)), expected)

	def test_the_outcomes_reach_the_wire_the_canvas_reads(self):
		"""The Wait's picker is built from the node-type payload, so a derivation that never shipped would
		be invisible to the only surface that consumes it."""
		payload = {n["type"]: n for n in registry.node_types()}
		self.assertIn(_DELIVERED, payload[_SEND_VERB]["outcomes"])


class _BridgeHarness(FrappeTestCase):
	"""Real account, real template, real graphs, the real ingest. Only the HTTP boundary is patched."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		assert_masters_exist()
		fx.purge(*frappe.get_all(fx.WORKFLOW_DT, filters={"name": ("like", f"{_WORKFLOW}%")}, pluck="name"))
		fx.arm_engine(True, cls)
		cls.account = cls._make_account()
		cls.template = cls._make_template()
		cls.number = f"+9198111{int(hashlib.md5(cls.__name__.encode()).hexdigest(), 16) % 10**5:05d}"
		for stale in frappe.get_all("CRM Lead", filters={"mobile_no": cls.number}, pluck="name"):
			frappe.delete_doc("CRM Lead", stale, force=True, ignore_permissions=True)
		cls.lead = fx.make_lead()
		cls.lead.mobile_no = cls.number
		cls.lead.save(ignore_permissions=True)  # authz-ok: tier-c — fixture, through the document API (B11)
		cls._made = []
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		fx.purge(*cls._made)
		frappe.db.delete("WhatsApp Message", {"reference_name": cls.lead.name})
		frappe.delete_doc("CRM Lead", cls.lead.name, force=True, ignore_permissions=True)
		frappe.delete_doc("WhatsApp Templates", cls.template, force=True, ignore_permissions=True)
		frappe.delete_doc("WhatsApp Account", _ACCOUNT, force=True, ignore_permissions=True)
		frappe.db.commit()
		super().tearDownClass()

	@classmethod
	def _make_account(cls):
		if frappe.db.exists("WhatsApp Account", _ACCOUNT):
			frappe.delete_doc("WhatsApp Account", _ACCOUNT, force=True, ignore_permissions=True)
		return frappe.get_doc({
			"doctype": "WhatsApp Account", "account_name": _ACCOUNT, "status": "Active",
			"url": "https://live-mt-server.wati.io/000003", "token": "channel-outcome-token",
			"custom_provider": "WATI", "custom_wati_channel_number": "919000000888",
		}).insert(ignore_permissions=True).name  # authz-ok: tier-c — test fixture, no user input

	@classmethod
	def _make_template(cls):
		"""B11 DEVIATION, declared: `db_insert` bypasses the controller on purpose.

		`WhatsAppTemplates.after_insert` calls `make_post_request` against Meta's live API, so `doc.insert()`
		from a test would fire REAL provider traffic. The row is a WATI-mirrored read-only catalogue entry
		that only ever lands this way in production too (`templates_sync`), so no rule-shaping hook is being
		skipped — the hook being avoided is an outbound network call.
		"""
		name = f"{_TEMPLATE}-en"
		if frappe.db.exists("WhatsApp Templates", name):
			frappe.delete_doc("WhatsApp Templates", name, force=True, ignore_permissions=True)
		doc = frappe.new_doc("WhatsApp Templates")
		doc.update({
			"template_name": _TEMPLATE, "template": "<p>Hi</p>", "language_code": "en",
			"category": "UTILITY", "whatsapp_account": cls.account, "actual_name": _TEMPLATE,
			"status": "APPROVED",
		})
		doc.name = name
		doc.db_insert()
		return doc.name

	def _graph(self, name):
		"""Send → Wait(until whatsapp.delivered, correlated on the send node) → Terminal."""
		self._made.append(name)
		wf = fx.make_workflow(name, [
			fx.trigger(to="wa"),
			fx.node("wa", _SEND_VERB, config={"contact_number": "crm_lead.mobile_no", "whatsapp_template": self.template},
			        edges={sends.SENT: "w1", sends.FAILED: "dead"}),
			fx.node("w1", "Wait",
			        config={"mode": "Until Event", "event_name": _DELIVERED, "source_node": "wa"},
			        edges={"event": "end"}),
			fx.node("end", "Terminal"),
			fx.node("dead", "Terminal"),
		])
		frappe.db.commit()
		return wf

	def _park_a_run(self, name):
		"""Walk a run until it parks on the Wait, and hand back the token it is waiting on."""
		from tatva_connect.workflow_engine import interpreter

		workflow = self._graph(name)
		run = fx.start_run(workflow, self.lead.name, "start")
		interpreter.advance(frappe.get_doc(fx.RUN_DT, run.name))
		row = frappe.get_doc(fx.RUN_DT, run.name)
		return row

	def _sent_message(self, message_id, token):
		"""The row the send job writes, created the way the job creates it — through the document API."""
		doc = frappe.get_doc({
			"doctype": "WhatsApp Message", "type": "Outgoing", "message_type": "Template",
			"use_template": 1, "template": self.template, "message": "probe",
			"content_type": "text", "to": self.number, "message_id": message_id,
			"status": "sent", "whatsapp_account": self.account,
			"reference_doctype": "CRM Lead", "reference_name": self.lead.name,
			"custom_workflow_correlation": token,
		})
		doc.flags.tatva_ingested = True  # already on the wire — the controller must not send it again
		doc.insert(ignore_permissions=True)  # authz-ok: tier-c — fixture, through the document API (B11)
		frappe.db.commit()
		return doc.name

	def _deliver(self, message_id):
		"""Feed a real WATI status payload through the real normalizer and the real ingest."""
		event = wati.normalize(_status_payload(message_id), account=self.account)
		ingest.apply(event)


class TestAReceiptWakesOnlyTheRunThatSentThatMessage(_BridgeHarness):
	"""THE HEADLINE. One lead, two journeys, two messages. A receipt for message A must move run A and
	leave run B exactly where it was."""

	def test_two_runs_on_one_lead_are_woken_independently(self):
		run_a = self._park_a_run(f"{_WORKFLOW}-a")
		run_b = self._park_a_run(f"{_WORKFLOW}-b")
		self.assertEqual(run_a.status, "Parked", "the run must be waiting before a receipt can wake it")
		self.assertEqual(run_b.status, "Parked")
		self.assertNotEqual(
			run_a.awaiting_correlation, run_b.awaiting_correlation,
			"two sends must park on two different tokens or nothing downstream can tell them apart",
		)
		self._sent_message("MID-A", run_a.awaiting_correlation)
		self._sent_message("MID-B", run_b.awaiting_correlation)

		self._deliver("MID-A")

		after_a = frappe.get_doc(fx.RUN_DT, run_a.name)
		after_b = frappe.get_doc(fx.RUN_DT, run_b.name)
		self.assertEqual(after_a.status, "Done", "the run that sent MID-A must have been woken")
		self.assertEqual(after_a.current_node, "end")
		self.assertEqual(after_b.status, "Parked", "the OTHER run on the same lead must not have moved")

	def test_the_row_is_what_carries_the_engine_token(self):
		"""The join itself: the provider's id and the engine's token meet on one row, and the bridge reads
		the token back off it. Without this the receipt could only find the lead."""
		run = self._park_a_run(f"{_WORKFLOW}-carrier")
		name = self._sent_message("MID-CARRIER", run.awaiting_correlation)

		self.assertEqual(
			frappe.db.get_value("WhatsApp Message", name, "custom_workflow_correlation"),
			run.awaiting_correlation,
		)

	def test_a_message_no_workflow_sent_wakes_nothing_and_is_not_an_error(self):
		"""A rep's manual send carries no token. That is silence, not a loss — and it must not log."""
		run = self._park_a_run(f"{_WORKFLOW}-manual")
		self._sent_message("MID-MANUAL", None)

		with patch.object(frappe, "log_error") as logged:
			self._deliver("MID-MANUAL")

		self.assertEqual(frappe.get_doc(fx.RUN_DT, run.name).status, "Parked")
		self.assertFalse(logged.called, "a manual send is not an unmappable status")

	def test_a_status_matching_no_message_is_logged_never_dropped(self):
		"""The not-found path. A run may be parked waiting for exactly this receipt, and the operator's
		only other clue would be a journey that silently stopped."""
		with patch.object(frappe, "log_error") as logged:
			self._deliver("MID-NOTHING-MATCHES")

		self.assertTrue(logged.called, "an unmappable delivery status must be visible")
		self.assertIn("matches no message", logged.call_args.kwargs.get("title", ""))


class TestTheSendJobReallyWritesTheCorrelation(_BridgeHarness):
	"""The write half, driven through the real `_deliver_whatsapp` — the function the enqueue calls."""

	def test_the_send_job_stamps_the_token_on_the_row_it_files(self):
		def _capture(account, to_number, *args, **kwargs):
			return {"result": True, "local_message_id": "MID-JOB"}

		with patch.object(transport, "send_template_message", _capture):
			sends._deliver_whatsapp(
				account_name=self.account, to_number="919811100000", template=self.template,
				parameters=[], lead=self.lead.name, correlation="RUN-X::wa",
			)

		name = frappe.db.get_value("WhatsApp Message", {"message_id": "MID-JOB"}, "name")
		self.assertTrue(name, "the send job must file the message it sent")
		self.assertEqual(
			frappe.db.get_value("WhatsApp Message", name, "custom_workflow_correlation"), "RUN-X::wa",
			"a row written without the token can never be correlated back to its run",
		)


class TestTheRegistryCarriesEveryKeyItsDeclarationDefines(unittest.TestCase):
	"""B12, and the defect that has now happened TWICE in one file: `_verb_field` warns that "a whitelist
	of keys is a contract that quietly stops carrying whatever is added to it next" — written after
	`grain_scoped` was dropped that way, and the function above it then dropped `outcomes` the same way.

	This asserts the SHAPE rather than a name list: every key a node type's declaration defines must
	survive into the payload the frontend reads. A key added to a declaration and forgotten in the
	whitelist is red on the next run.
	"""

	# Declaration-internal keys that are deliberately not shipped — each is server-side machinery, not a contract the canvas reads.
	# `outputs_by` is a RESOLUTION RULE, and shipping it is what let the canvas re-implement `outputs_for` in JS; the canvas now asks `registry.graph_outputs` for the resolved answer instead (W2.1c).
	_INTERNAL = frozenset({"handler", "lane", "target", "params", "is_verb", "outcomes_channel", "config", "outputs_by"})

	def test_every_declared_key_survives_into_the_wire_payload(self):
		payload = {n["type"]: n for n in registry.node_types()}
		for node_type, declared in registry.NODE_TYPES.items():
			missing = {k for k in declared if k not in self._INTERNAL} - set(payload[node_type])
			self.assertEqual(
				missing, set(),
				f"{node_type} declares {sorted(missing)} and the wire payload drops it — the whitelist "
				"stopped carrying a key its declaration defines",
			)

	def test_the_send_node_ships_its_derived_outcomes(self):
		"""The concrete case this lock was built around, asserted directly so the general rule above
		cannot pass by coincidence."""
		payload = {n["type"]: n for n in registry.node_types()}
		self.assertEqual(payload[_SEND_VERB]["outcomes"], actions.outcomes_of(_SEND_VERB))
