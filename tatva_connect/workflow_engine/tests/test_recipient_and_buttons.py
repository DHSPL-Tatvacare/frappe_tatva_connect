# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""THE RECIPIENT IS DECLARED, AND A TAP LEAVES DOWN ITS OWN BRANCH.

W1.4 — THE RECIPIENT. `Send Email` has always declared `email_recipient`. `Send WhatsApp` declared
nothing and resolved the number implicitly from the lead behind the author's back, and that is why it was
the node that delivered a real patient's message to a stranger in Turkey: nobody ever CHOSE the recipient,
so there was no control on which to show a warning and no author who had looked at it.

It now declares `contact_number` — the SAME field, the same picker, in every trigger mode (§8b.1,
CONFIRMED). Not conditional on trigger mode: a node whose controls change shape according to something
else in the graph is an inconsistent authoring layer, and the inconsistency is the defect, not the extra
field. The engine resolves the declared ref against the run's subject; it never supplies one.

`contact_number` is `Variable` WITHOUT `free_text`, which is the one place it differs from
`email_recipient`. It is PICKED, never typed, so it resolves purely as a reference. Routing it through
`sends.resolve_recipient` would re-introduce that helper's literal path — which treats a phone-shaped
string as an address to send to — and a typed number is precisely how Turkey happened.

W1.3 — BUTTONS. `whatsapp.clicked` was DECLARED and selectable in the Wait picker from W1.1, and nothing
could ever deliver it: `_wake_workflow` was reachable only from `_update_status`, and a tap arrives as
`kind="inbound"`. A declaration nothing can deliver is the same lie as an outcome nothing reports.

The join was the missing half. A tap carries `replyContextId` — the OUTBOUND wamid — while the outbound
row stores `message_id = localMessageId`, deliberately (a row stored under a wamid never receives status
updates; the reason is in `_classify`'s own comment). So the wamid is now captured as a SECOND id
alongside it, never instead of it, and the bridge joins tap → outbound row → run.

WHICH BUTTON is routed on comes from the AUTHOR'S DECLARATION on the send, never from reading whatever
arrived and inventing an edge for it.
"""
import hashlib
import json
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.automation import actions, sends
from tatva_connect.channels import contract
from tatva_connect.tests.authz.grains import assert_masters_exist
from tatva_connect.whatsapp import ingest, transport, wati
from tatva_connect.workflow_engine import graph, interpreter, refs, registry
from tatva_connect.workflow_engine.tests import fixtures as fx

_WORKFLOW = "recipient-button-probe"
_ACCOUNT = "Recipient-button-probe-account"
_TEMPLATE = "recipient-button-probe"
_SEND_VERB = "Send WhatsApp"

_LEAD_NUMBER = "+919812300001"
_DECLARED_NUMBER = "+919812300002"
_WIRE_DECLARED = "919812300002"
_WAMID = "wamid.HBgMOTE5ODEyMzAwMDAy86AA=="


def _tap_payload(reply_context, button_id="yes", number="919812300002"):
	"""A real button-tap payload. `type: interactive` + `interactiveButtonReply`, and `replyContextId`
	pointing at the message that offered the buttons — the field guide's verified sample shape."""
	return {
		"eventType": "message", "type": "interactive", "waId": number, "owner": False,
		"text": "Yes", "id": f"wati-inbound-{button_id}",
		"interactiveButtonReply": {"id": button_id, "title": "Yes"},
		"replyContextId": reply_context,
		"conversationId": "conv-tap-probe",
	}


class TestTheRecipientIsDeclared(FrappeTestCase):
	"""W1.4 at the declaration level — the shape §8b.1 confirms, asserted rather than described."""

	def test_send_whatsapp_declares_a_contact_number(self):
		"""THE red. It declared a template and template values and nothing that says who it goes to."""
		params = {p["name"]: p for p in actions.VERBS[_SEND_VERB]["params"]}
		self.assertIn("contact_number", params)

	def test_the_recipient_is_picked_never_typed(self):
		"""`Variable` without `free_text`. Send Email allows a typed literal address; a typed PHONE NUMBER
		is what reached Turkey, so this control offers the picker and nothing else."""
		field = {p["name"]: p for p in actions.VERBS[_SEND_VERB]["params"]}["contact_number"]
		self.assertEqual(field["type"], "Variable")
		self.assertTrue(field["reqd"])
		self.assertFalse(field.get("free_text"), "a phone number must never be typed into this control")

	def test_the_same_field_is_offered_in_every_trigger_mode(self):
		"""H1. The declaration carries no `depends_on_value`, so the author opens the same node and sees
		the same controls whatever fired the run. §8d.2 proposed the conditional shape and it is REJECTED."""
		field = {p["name"]: p for p in actions.VERBS[_SEND_VERB]["params"]}["contact_number"]
		self.assertIsNone(
			field.get("depends_on_value"),
			"a node whose controls change shape by trigger mode is an inconsistent authoring layer",
		)

	def test_publish_refuses_a_recipient_nothing_upstream_produces(self):
		"""The whole point of declaring it: a misspelt recipient becomes an AUTHORING error caught at
		publish, instead of a live misdelivery nobody sees until a patient complains."""
		nodes = [
			{"node_id": "start", "node_type": "Trigger",
			 "config_json": json.dumps({"subject_doctype": "CRM Lead", "event": "Created"}),
			 "edges": [{"from_output": "next", "to_node": "wa"}]},
			{"node_id": "wa", "node_type": _SEND_VERB,
			 "config_json": json.dumps({"whatsapp_template": "t", "contact_number": "nobody_makes_this"}),
			 "edges": [{"from_output": "sent", "to_node": "end"}, {"from_output": "failed", "to_node": "end"}]},
			{"node_id": "end", "node_type": "Terminal", "config_json": "{}", "edges": []},
		]
		messages = " | ".join(p["message"] for p in graph.problems(nodes, entry_node="start"))
		self.assertIn("nobody_makes_this", messages)


class _SendHarness(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		assert_masters_exist()
		fx.purge(*frappe.get_all(fx.WORKFLOW_DT, filters={"name": ("like", f"{_WORKFLOW}%")}, pluck="name"))
		fx.arm_engine(True, cls)
		cls.account = cls._make_account()
		cls.template = cls._make_template()
		for stale in frappe.get_all("CRM Lead", filters={"mobile_no": _LEAD_NUMBER}, pluck="name"):
			frappe.delete_doc("CRM Lead", stale, force=True, ignore_permissions=True)
		cls.lead = fx.make_lead()
		cls.lead.mobile_no = _LEAD_NUMBER
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
			"url": "https://live-mt-server.wati.io/000003", "token": "recipient-probe-token",
			"custom_provider": "WATI",
			"custom_wati_channel_number": f"9190{int(hashlib.md5(_ACCOUNT.encode()).hexdigest(), 16) % 10**8:08d}",
		}).insert(ignore_permissions=True).name  # authz-ok: tier-c — test fixture, no user input

	@classmethod
	def _make_template(cls):
		"""B11 DEVIATION, declared: `db_insert` bypasses the controller on purpose.

		`WhatsAppTemplates.after_insert` calls `make_post_request` against Meta's live API, so `doc.insert()`
		from a test would fire REAL provider traffic. The row is a WATI-mirrored read-only catalogue entry
		that only ever lands this way in production too, so no rule-shaping hook is skipped.
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

	def _send(self, contact_ref, context):
		"""Drive the real verb handler, which is what reads the declared field."""
		from tatva_connect.whatsapp import routing

		enqueued = {}
		params = frappe._dict({
			"action_type": _SEND_VERB, "whatsapp_template": self.template,
			"contact_number": contact_ref, "template_values": [],
		})
		with patch.object(sends, "sends_enabled", return_value=True), \
		     patch.object(routing, "resolve_account_for_lead", return_value=self.account), \
		     patch.object(wati, "template_variables", return_value=[]), \
		     patch("tatva_connect.whatsapp.channel.is_enabled", return_value=True), \
		     patch.object(frappe, "enqueue", lambda _m, **kw: enqueued.update(kw)):
			ctx = dict(context)
			result = actions._action_send_whatsapp(params, self.lead.name, ctx, None, None)
			if callable(result):
				result()
		return ctx.get(refs.OUTPUT), enqueued.get("to_number")


class TestTheDeclaredRecipientIsWhatIsDialled(_SendHarness):
	"""W1.4 at runtime, both directions."""

	def test_the_number_sent_is_the_one_the_declaration_names(self):
		"""THE headline red. The lead's own `mobile_no` is DIFFERENT from the declared ref's value, so a
		path still reading the lead implicitly cannot pass this."""
		output, wire = self._send("sv.number", {"sv.number": _DECLARED_NUMBER})

		self.assertEqual(output, sends.SENT)
		self.assertEqual(wire, _WIRE_DECLARED, "the declared recipient is what reaches the provider")

	def test_the_lead_mobile_no_is_no_longer_a_fallback(self):
		"""H4 — the implicit read is DELETED, not left beside the declaration. The lead HAS a good number;
		if the declared ref resolves to nothing the send must refuse rather than quietly use it."""
		output, wire = self._send("sv.number", {})

		self.assertEqual(output, sends.FAILED, "a resolved-to-nothing recipient must not fall back")
		self.assertIsNone(wire)
		self.assertNotEqual(wire, _LEAD_NUMBER.lstrip("+"))

	def test_the_refusal_names_the_reference_that_resolved_to_nothing(self):
		"""An author reading the step log must know WHICH control to fix."""
		params = frappe._dict({
			"action_type": _SEND_VERB, "whatsapp_template": self.template,
			"contact_number": "sv.number", "template_values": [],
		})
		with patch.object(sends, "sends_enabled", return_value=True):
			ctx = {}
			marker = actions._action_send_whatsapp(params, self.lead.name, ctx, None, None)
			self.assertEqual(ctx.get(refs.OUTPUT), sends.FAILED)

		self.assertIn("sv.number", str(marker))


class TestATapWakesTheRunThatOfferedTheButtons(_SendHarness):
	"""W1.3. `whatsapp.clicked` was declared from W1.1 and undeliverable until now."""

	def _graph(self, name):
		self._made.append(name)
		wf = fx.make_workflow(name, [
			fx.trigger(to="sv"),
			# The recipient is DECLARED, so something upstream must really produce it or the send takes its `failed` edge and a tap test passes for the wrong reason.
			fx.node("sv", "Set Variables",
			        config={"assign": json.dumps({"number": _DECLARED_NUMBER})}, edges={"next": "wa"}),
			fx.node("wa", _SEND_VERB, config={
				"whatsapp_template": self.template, "contact_number": "sv.number",
				"buttons": [{"id": "yes"}, {"id": "no"}],
			}, edges={sends.SENT: "w1", sends.FAILED: "dead"}),
			fx.node("w1", "Wait", config={
				"mode": "Until Event", "event_name": "whatsapp.clicked", "source_node": "wa",
			}, edges={"yes": "said_yes", "no": "said_no"}),
			fx.node("said_yes", "Terminal"),
			fx.node("said_no", "Terminal"),
			fx.node("dead", "Terminal"),
		])
		frappe.db.commit()
		return wf

	def _park(self, name):
		run = fx.start_run(self._graph(name), self.lead.name, "start")
		interpreter.advance(frappe.get_doc(fx.RUN_DT, run.name))
		return frappe.get_doc(fx.RUN_DT, run.name)

	def _sent_row(self, wamid, token, message_id):
		doc = frappe.get_doc({
			"doctype": "WhatsApp Message", "type": "Outgoing", "message_type": "Template",
			"use_template": 1, "template": self.template, "message": "probe", "content_type": "text",
			"to": _WIRE_DECLARED, "message_id": message_id, "status": "sent",
			"whatsapp_account": self.account, "reference_doctype": "CRM Lead",
			"reference_name": self.lead.name, "custom_workflow_correlation": token,
			"custom_outbound_wamid": wamid,
		})
		doc.flags.tatva_ingested = True
		doc.insert(ignore_permissions=True)  # authz-ok: tier-c — fixture, through the document API (B11)
		frappe.db.commit()
		return doc.name

	def test_a_tap_wakes_the_run_that_sent_that_message(self):
		"""THE headline red. Before this the tap was ingested as an inbound message and the run stayed
		parked for ever, because the bridge was reachable only from the status path."""
		run = self._park(f"{_WORKFLOW}-tap")
		self.assertEqual(run.status, "Parked", "the run must really be waiting before a tap can wake it")
		self._sent_row(_WAMID, run.awaiting_correlation, "MID-TAP")

		ingest.apply(wati.normalize(_tap_payload(_WAMID, "yes"), account=self.account))

		after = frappe.get_doc(fx.RUN_DT, run.name)
		self.assertEqual(after.status, "Done", "the tap must wake the run that offered the buttons")
		self.assertEqual(
			after.current_node, "said_yes",
			"the run must leave by the branch declared for the button that was actually tapped",
		)

	def test_a_tap_on_another_message_does_not_wake_this_run(self):
		"""The other direction, and the one that matters clinically: one lead, two journeys."""
		# Its OWN wamid: two sent messages can never share one, and sharing it would let another test's tap wake this run.
		run = self._park(f"{_WORKFLOW}-other")
		self.assertEqual(run.status, "Parked")
		self._sent_row("wamid.OTHER-MESSAGE-ENTIRELY", run.awaiting_correlation, "MID-OTHER")

		ingest.apply(wati.normalize(_tap_payload("wamid.SOMETHING-ELSE", "yes"), account=self.account))

		self.assertEqual(frappe.get_doc(fx.RUN_DT, run.name).status, "Parked")

	def test_the_send_job_stores_the_wamid_alongside_the_local_message_id(self):
		"""The join column. `message_id` must STILL be the localMessageId — a row stored under a wamid
		never receives status updates, which is why `_classify` discards it — so the wamid is a SECOND id."""
		def _capture(account, to_number, *args, **kwargs):
			return {"result": True, "local_message_id": "MID-BOTH",
			        "message": {"whatsappMessageId": _WAMID}}

		with patch.object(transport, "send_template_message", _capture):
			sends._deliver_whatsapp(
				account_name=self.account, to_number=_WIRE_DECLARED, template=self.template,
				parameters=[], lead=self.lead.name, correlation="RUN-Y::wa",
			)

		row = frappe.db.get_value(
			"WhatsApp Message", {"message_id": "MID-BOTH"},
			["message_id", "custom_outbound_wamid"], as_dict=True,
		)
		self.assertEqual(row.message_id, "MID-BOTH", "the status join key must stay the localMessageId")
		self.assertEqual(row.custom_outbound_wamid, _WAMID, "the tap join key must be captured too")


class TestTheBranchTargetsComeFromTheDeclaration(FrappeTestCase):
	"""B3/H2. An author wires a branch per button the SEND declared — never per button that happened to
	arrive, which would let the provider invent edges on our canvas."""

	def test_a_wait_on_a_send_offering_buttons_gets_one_output_per_button(self):
		outputs = registry.outputs_for("Wait", {
			"mode": "Until Event", "event_name": "whatsapp.clicked", "source_node": "wa",
		}, graph_config={"wa": {"buttons": [{"id": "yes"}, {"id": "no"}]}})
		self.assertEqual(outputs, ["yes", "no"])

	def test_a_wait_on_a_send_offering_no_buttons_keeps_its_plain_event_edge(self):
		"""The other direction — a send with no declared buttons must not grow phantom handles."""
		outputs = registry.outputs_for("Wait", {
			"mode": "Until Event", "event_name": "whatsapp.clicked", "source_node": "wa",
		}, graph_config={"wa": {}})
		self.assertEqual(outputs, ["event"])

	def test_the_classifier_captures_the_wamid_without_losing_the_correlation_id(self):
		"""`_classify` is the ONE place a send outcome is decided. It must now return both ids."""
		result = wati._classify({"result": True, "local_message_id": "LMID-1",
		                         "message": {"whatsappMessageId": _WAMID}})

		self.assertEqual(result.correlation_id, "LMID-1", "status updates still join on this")
		self.assertEqual(result.wamid, _WAMID, "and a tap joins on this")

	def test_a_send_response_with_no_wamid_is_not_a_failure(self):
		"""Not every endpoint returns one, and a missing tap-join key must not break a send."""
		result = wati._classify({"result": True, "local_message_id": "LMID-2"})

		self.assertTrue(result.accepted)
		self.assertIsNone(result.wamid)


class TestTheDeclarationIsWhatTheEngineReads(FrappeTestCase):
	"""B12 — the lock matches the declared surface and asserts the CALL, so renaming the resolution mode
	or bypassing `outputs_for` is red rather than silently green."""

	def test_outputs_for_is_the_one_answer_for_the_rows_mode_too(self):
		declared = registry.NODE_TYPES["Wait"]["outputs_by"]
		self.assertIn("rows_from", declared, "the rows resolution must live INSIDE outputs_by (B7)")

	def test_a_button_id_the_send_never_declared_cannot_be_wired(self):
		"""The enforcement half: publish must refuse an edge on a button nobody offered."""
		outputs = registry.outputs_for("Wait", {
			"mode": "Until Event", "event_name": "whatsapp.clicked", "source_node": "wa",
		}, graph_config={"wa": {"buttons": [{"id": "yes"}]}})
		self.assertNotIn("maybe", outputs)
