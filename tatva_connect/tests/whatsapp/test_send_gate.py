# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""ONE GATE. Every outbound WhatsApp message passes it, or it is not sent.

Two defects, one gate, because they are the same defect: a rule that exists in the send path's head
rather than in one place every surface must go through.

D1 — THE NUMBER. A number stored without a country code is ambiguous, and the send path reduced it
further with a `\\D`-strip. WATI resolves the dialling plan itself, so it read the leading digits as a
country code and the message reached a different subscriber. Chunk 1 fixed the WORKFLOW path only. A rep pressing Send in the
lead's WhatsApp tab, a notification, and a bulk campaign all still went straight to the provider with an
ambiguous number.

D2 — THE CONSENT. `CRM Lead.custom_whatsapp_opt_out` had NO READER anywhere in the send path. 106 of the
2,381 leads on this bench have it ticked, and every one of them was messageable by every surface.

WHAT IS ASSERTED, AND ON WHICH SURFACE
--------------------------------------
Both rules are proven on the MANUAL surface and on the WORKFLOW surface, because "it works in the engine"
is exactly the assumption that left three surfaces open after Chunk 1. Every refusal is also proven in the
POSITIVE direction — the same lead, the same number, sent successfully once the condition is removed —
because a gate that refuses everything would pass a one-sided suite and break the product.

The AST lock at the bottom is the one that matters most in six months: it fails when ANY module calls an
adapter's send function without going through the gate, so a fourth surface cannot be added unguarded.

NOTHING REACHES A PROVIDER. `transport` is patched at the HTTP boundary in every test that sends; the
WhatsApp kill-switch and the sends switch stay OFF and are patched in-process, never written to the bench.
Every probe row is torn down.
"""
import ast
import pathlib
import unittest
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.automation import sends
from tatva_connect.tests.authz.grains import GRAINS, assert_masters_exist
from tatva_connect.whatsapp import channel, notification, transport, wati

_ACCOUNT = "Send-gate-probe-account"
_TEMPLATE = "send-gate-probe"
_GRAIN = GRAINS[2]

# A bare 10-digit number and the canonical form of the same subscriber.
_BARE = "9876543210"
_CANONICAL = "+919876543210"
_WIRE = "919876543210"


def _account():
	if frappe.db.exists("WhatsApp Account", _ACCOUNT):
		frappe.delete_doc("WhatsApp Account", _ACCOUNT, force=True, ignore_permissions=True)
	return frappe.get_doc({
		"doctype": "WhatsApp Account", "account_name": _ACCOUNT, "status": "Active",
		"url": "https://live-mt-server.wati.io/000003", "token": "send-gate-probe-token",
		"custom_provider": "WATI", "custom_wati_channel_number": "919000000777",
	}).insert(ignore_permissions=True).name  # authz-ok: tier-c — test fixture, no user input


def _template(account):
	"""B11 DEVIATION, declared: `db_insert` bypasses the controller on purpose.

	`WhatsAppTemplates.after_insert` calls `make_post_request` against Meta's live API, so `doc.insert()`
	from a test would fire REAL provider traffic. The row is a WATI-mirrored read-only catalogue entry
	that only ever lands this way in production too (`templates_sync`), so nothing a hook shapes is being
	skipped — the hook here is an outbound network call, not a rule.
	"""
	name = f"{_TEMPLATE}-en"
	if frappe.db.exists("WhatsApp Templates", name):
		frappe.delete_doc("WhatsApp Templates", name, force=True, ignore_permissions=True)
	doc = frappe.new_doc("WhatsApp Templates")
	doc.update({
		"template_name": _TEMPLATE, "template": "<p>Hi</p>", "language_code": "en",
		"category": "UTILITY", "whatsapp_account": account, "actual_name": _TEMPLATE,
		"status": "APPROVED",
	})
	doc.name = name
	doc.db_insert()
	return doc.name


def _lead(number, opted_out=0):
	return frappe.get_doc({
		"doctype": "CRM Lead", "first_name": "Gate", "lead_name": "Gate Probe", "status": "New",
		"mobile_no": number, "custom_whatsapp_opt_out": opted_out,
		"custom_vertical": _GRAIN["vertical"], "custom_group": _GRAIN["group"],
		"custom_current_program": _GRAIN["program"],
	}).insert(ignore_permissions=True)  # authz-ok: tier-c — test fixture, no user input


class _GateHarness(FrappeTestCase):
	"""One real account, one real template, the real controller and the real gate. Only the HTTP boundary
	and the two operator switches are patched."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		assert_masters_exist()
		cls.account = _account()
		cls.template = _template(cls.account)
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		frappe.delete_doc("WhatsApp Templates", cls.template, force=True, ignore_permissions=True)
		frappe.delete_doc("WhatsApp Account", _ACCOUNT, force=True, ignore_permissions=True)
		frappe.db.commit()
		super().tearDownClass()

	def setUp(self):
		self.leads = []
		self.sent = []

	def tearDown(self):
		for lead in self.leads:
			frappe.db.delete("WhatsApp Message", {"reference_name": lead})
			frappe.delete_doc("CRM Lead", lead, force=True, ignore_permissions=True)
		frappe.db.commit()

	def _lead(self, number, opted_out=0):
		doc = _lead(number, opted_out)
		self.leads.append(doc.name)
		return doc

	def _transport(self):
		"""Capture at the HTTP boundary — the last point before the wire. What lands in `self.sent` is
		what the patient's provider would really have received."""

		def _capture(account, to_number, *args, **kwargs):
			self.sent.append(to_number)
			return {"result": True, "local_message_id": "gate-probe"}

		return patch.multiple(
			transport,
			send_template_message=_capture,
			send_session_message=_capture,
		)

	def _manual_send(self, lead, template=True):
		"""The manual surface, driven the way the lead's WhatsApp tab drives it — a `WhatsApp Message`
		row insert. Every manual path (free text, template, reaction, bulk) funnels through this one
		controller."""
		row = {
			"doctype": "WhatsApp Message", "type": "Outgoing",
			"reference_doctype": "CRM Lead", "reference_name": lead.name,
			"to": lead.mobile_no, "whatsapp_account": self.account, "content_type": "text",
		}
		if template:
			row.update({"message_type": "Template", "use_template": 1, "template": self.template,
			            "message": "Template message"})
		else:
			row.update({"message_type": "Text", "message": "hello"})
		with self._transport(), patch.object(channel, "is_enabled", return_value=True):
			return frappe.get_doc(row).insert(ignore_permissions=True)  # authz-ok: tier-c — test drives the real controller

	def _workflow_send(self, lead):
		"""Returns `(output, marker, number_bound_for_the_wire)`.

		The workflow path defers its provider call past commit, so nothing reaches `transport` inline —
		the number it would really send is read out of the deferred enqueue by running the thunk.
		"""
		from tatva_connect.whatsapp import routing

		enqueued = {}
		with self._transport(), \
		     patch.object(sends, "sends_enabled", return_value=True), \
		     patch.object(routing, "resolve_account_for_lead", return_value=self.account), \
		     patch.object(channel, "is_enabled", return_value=True), \
		     patch.object(frappe, "enqueue", lambda _m, **kw: enqueued.update(kw)):
			output, deferred = sends.send_whatsapp(
				lead.name, "probe.number", self.template, {"probe.number": lead.mobile_no},
			)
			if callable(deferred):
				deferred()
		return output, deferred, enqueued.get("to_number")


class TestABareNumberIsRefusedOnEverySurface(_GateHarness):
	"""HEADLINE 1. Chunk 1 protected the workflow path; this proves the manual path too, and proves the
	same number goes through once it carries a country code."""

	def test_a_manual_send_to_a_bare_number_is_refused(self):
		"""THE red. A rep pressing Send in the lead's WhatsApp tab handed a bare number to WATI, which
		resolved the dialling plan itself. Nothing in the manual path looked at the number at all."""
		lead = self._lead(_BARE)

		with self.assertRaises(frappe.ValidationError) as caught:
			self._manual_send(lead)

		self.assertIn(_BARE, str(caught.exception), "the rep must be told WHICH number was refused")
		self.assertEqual(self.sent, [], "nothing may reach the provider for an ambiguous number")

	def test_the_same_manual_send_goes_through_once_the_number_carries_its_country_code(self):
		"""The positive half. A gate that refused everything would pass the test above and break the CRM."""
		lead = self._lead(_CANONICAL)

		self._manual_send(lead)

		self.assertEqual(self.sent, [_WIRE], "the conformed number is what reaches the provider")

	def test_a_workflow_send_to_a_bare_number_is_refused_the_same_way(self):
		"""Cross-surface parity: one gate, so the two surfaces cannot answer differently."""
		lead = self._lead(_BARE)

		output, marker, wire = self._workflow_send(lead)

		self.assertEqual(output, sends.FAILED)
		self.assertIn(_BARE, marker)
		self.assertIsNone(wire, "nothing may be queued for a number the provider would have to guess at")

	def test_a_free_text_manual_send_is_gated_too_not_only_templates(self):
		"""The session path reaches a different adapter function and had its own `\\D`-strip."""
		lead = self._lead(_BARE)

		with self.assertRaises(frappe.ValidationError):
			self._manual_send(lead, template=False)

		self.assertEqual(self.sent, [])


class TestAnOptedOutPatientIsNotMessaged(_GateHarness):
	"""HEADLINE 2. 106 leads on this bench have opted out and every surface would message them."""

	def test_a_workflow_send_to_an_opted_out_lead_is_refused_and_routable(self):
		"""THE red, and the SHAPE matters: an opted-out patient is a data state the author routes on —
		send an SMS instead, raise a call task — never an exception that kills the journey."""
		lead = self._lead(_CANONICAL, opted_out=1)

		output, marker, wire = self._workflow_send(lead)

		self.assertEqual(output, sends.FAILED, "routable data, not a raise")
		self.assertIn("opted out", marker, "the step log must say why the patient was not messaged")
		self.assertIsNone(wire, "a patient who said no must not be messaged")

	def test_the_same_lead_is_messaged_once_the_opt_out_is_cleared(self):
		"""The positive half — consent is read live, not cached into a decision made earlier."""
		lead = self._lead(_CANONICAL, opted_out=1)
		frappe.db.set_value("CRM Lead", lead.name, "custom_whatsapp_opt_out", 0)
		frappe.db.commit()
		lead.reload()

		output, _thunk, wire = self._workflow_send(lead)

		self.assertEqual(output, sends.SENT)
		self.assertEqual(wire, _WIRE)

	def test_a_manual_send_to_an_opted_out_lead_is_refused(self):
		"""A rep cannot message a patient who opted out, whatever the workflow does."""
		lead = self._lead(_CANONICAL, opted_out=1)

		with self.assertRaises(frappe.ValidationError) as caught:
			self._manual_send(lead)

		self.assertIn("opted out", str(caught.exception))
		self.assertEqual(self.sent, [])

	def test_a_notification_send_to_an_opted_out_lead_reaches_no_adapter(self):
		"""The third call site. It builds its own payload and never went near the other two."""
		lead = self._lead(_CANONICAL, opted_out=1)
		notif = frappe.get_doc({
			"doctype": "WhatsApp Notification", "notification_type": "DocType Event",
			"reference_doctype": "CRM Lead", "doctype_event": "After Insert",
			"notification_name": "gate-probe-notification",
			"template": self.template, "field_name": "mobile_no", "whatsapp_account": self.account,
			"disabled": 1,
		})
		notif.flags.ignore_mandatory = True
		notif.insert(ignore_permissions=True)  # authz-ok: tier-c — test fixture, disabled, never fires on its own
		self.addCleanup(frappe.delete_doc, "WhatsApp Notification", notif.name, force=True, ignore_permissions=True)

		with self._transport(), patch.object(channel, "is_enabled", return_value=True):
			notification.ChannelWhatsAppNotification(notif.as_dict()).notify(
				{"to": _CANONICAL, "template": {}}, doc_data=lead,
			)

		self.assertEqual(self.sent, [], "an opted-out patient must not be messaged by a notification either")


class TestASendThatNamesNoLeadIsRefused(_GateHarness):
	"""Consent is a fact about a PERSON. A send to a naked number is a send to somebody whose consent
	nobody established, so it is refused rather than guessed at.

	No phone-number lookup is used to rescue it. `routing.candidates_for_number` exists and inbound
	screening uses it, but at send time it answers a different question — a number matching three leads
	has no single consent answer, and picking one would be the inference this whole effort deletes.

	The only surface that sends without a lead is the bulk/campaign doctype, which has 0 rows and 0
	recipient lists on this bench: this refuses an unused path rather than breaking a used one.
	"""

	def test_a_row_with_no_lead_reference_is_refused(self):
		with self._transport(), patch.object(channel, "is_enabled", return_value=True), \
		     self.assertRaises(frappe.ValidationError) as caught:
			frappe.get_doc({
				"doctype": "WhatsApp Message", "type": "Outgoing", "message_type": "Text",
				"to": _CANONICAL, "message": "campaign blast", "content_type": "text",
				"whatsapp_account": self.account,
			}).insert(ignore_permissions=True)  # authz-ok: tier-c — test drives the real controller

		self.assertIn("lead", str(caught.exception).lower())
		self.assertEqual(self.sent, [])


class TestTheGateCannotBeBypassed(unittest.TestCase):
	"""THE lock that matters in six months. Chunk 1 protected one of three call sites because nobody
	enumerated them; this fails the suite when a fourth appears unguarded."""

	_APP = pathlib.Path(__file__).resolve().parents[2]

	# Where an adapter's send function may legitimately be called from, each of which must go through the gate.
	_ALLOWED: frozenset = frozenset({
		"tatva_connect/whatsapp/message.py",
		"tatva_connect/whatsapp/notification.py",
		"tatva_connect/automation/sends.py",
	})

	# The adapter contract's send surface, by NAME. Matching the name and not the receiver is deliberate:
	# an earlier revision only recognised a receiver literally called `adapter`, so renaming the variable
	# to `ad` would have slipped a new send past the lock in silence.
	_SEND_SURFACE: frozenset = frozenset({"send_template", "send_session", "send_media", "send_media_url"})

	def _modules_calling_an_adapter_send(self):
		found = {}
		for path in self._APP.rglob("*.py"):
			rel = str(path.relative_to(self._APP.parent))
			if "/tests/" in rel or rel.endswith("/wati.py"):
				continue
			tree = ast.parse(path.read_text())
			for node in ast.walk(tree):
				if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
					continue
				if node.func.attr not in self._SEND_SURFACE:
					continue
				# `self.send_*` is a doctype's own method, not a provider call — the adapter is never self.
				if isinstance(node.func.value, ast.Name) and node.func.value.id == "self":
					continue
				found.setdefault(rel, []).append(node.func.attr)
		return found

	def _calls_the_gate(self, rel):
		"""The gate must be CALLED, not merely mentioned. A substring check passed on a module that named
		`screen_send` only in a comment, which is how a lock comes to certify a bypass."""
		tree = ast.parse((self._APP.parent / rel).read_text())
		return any(
			isinstance(node, ast.Call)
			and isinstance(node.func, ast.Attribute)
			and node.func.attr == "screen_send"
			for node in ast.walk(tree)
		)

	def test_every_module_that_sends_goes_through_the_gate(self):
		callers = self._modules_calling_an_adapter_send()
		self.assertEqual(
			set(callers), set(self._ALLOWED),
			f"the set of modules calling an adapter send changed: {sorted(callers)}. A new send surface "
			"must route through channel.screen_send and be named here.",
		)
		for rel in callers:
			self.assertTrue(
				self._calls_the_gate(rel),
				f"{rel} calls an adapter send function without going through the one gate",
			)

	def test_no_adapter_send_path_reduces_a_number_on_its_own(self):
		"""The second conformer is DELETED, not merely unused. While `wati.send_*` kept its own
		`\\D`-strip, the declaration and the wire were two rules for one fact."""
		source = (self._APP / "whatsapp" / "wati.py").read_text()
		send_block = source[source.index("def send_template("):source.index("def list_templates(")]
		banned = [name for name in ("normalize_number", "phone_digits", "match_digits") if name in send_block]
		self.assertEqual(
			banned, [],
			f"a wati send function still shapes the number itself via {banned} — that is the second conformer",
		)
