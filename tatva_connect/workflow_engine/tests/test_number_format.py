# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""THE ADAPTER DECLARES ITS NUMBER FORMAT. The send asks it. A number that does not conform is REFUSED.

A WhatsApp message once reached the wrong subscriber. The lead's number was stored as a bare Indian
10-digit and `channel.normalize_number` claimed in its docstring to produce E.164 while actually only
stripping non-digits. An ambiguous number reached WATI and THE PROVIDER GUESSED THE COUNTRY, reading the
leading digits as a dialling code that was never declared.

The engine inferred a country nobody declared. This suite locks the deletion of that inference.

WHY THE FIX IS NOT ONE GLOBAL RULE
----------------------------------
"Demand a country code" as an engine-level rule is the WRONG SHAPE, and it was rejected before this was
built. WATI needs the digits with NO `+` (the number goes into a URL query — `?whatsappNumber={to}` — where
a `+` decodes as a space). Bolna, the AI-voice provider coming later, needs `+91`. A global resolver breaks
Bolna on day one. So the format is a property of the PROVIDER, it is DECLARED, and the send path asks.

BOTH DIRECTIONS ARE TESTED, AND THAT IS THE WHOLE POINT
-------------------------------------------------------
A suite that only proved "a bare number is refused" would have proved a global rule with extra steps. The
same bare number that WATI refuses is ACCEPTED by an adapter declaring `NATIONAL`, through the same code
path, with no engine change. That is what makes the design generic rather than a hardcoded opinion about
India.

NOTHING HERE IS REGISTERED, ARMED OR LEFT BEHIND
------------------------------------------------
The alternate-format adapters are in-process doubles reached by patching `resolve.adapter_for`;
`webhooks.registry.CHANNELS` is never touched, so no test double can leak into the real adapter registry.
`sends_enabled` and `channel.is_enabled` are patched in-process rather than switched on the bench — the
sends switch ships OFF (B10) and this suite never writes it. A config a test left on the bench once broke
the entire API suite; nothing here can. No message can leave: `frappe.enqueue` is patched and the provider
call is never reached.
"""
import hashlib
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.automation import sends
from tatva_connect.channels import contract, resolve
from tatva_connect.tests.authz.grains import assert_masters_exist
from tatva_connect.whatsapp import channel, routing, transport, wati
from tatva_connect.workflow_engine import interpreter
from tatva_connect.workflow_engine.tests import fixtures as fx

_ACCOUNT = "Number-format-probe-account"
_TEMPLATE = "number-format-probe"
_WORKFLOW = "number-format-probe"

# A bare 10-digit number and its correct canonical form. The bare spelling is the fixture on purpose: an ambiguous number is what the provider guesses a country for.
_BARE = "9876543210"
_CANONICAL = "+91-9876543210"
_WIRE_FOR_WATI = "919876543210"


def _declaration(provider, number_format):
	"""A declaration for a provider that does not exist, to prove the format is DECLARED and not decided
	here. Only the format varies — everything else is held constant so the format is the one variable."""
	return contract.declare(
		channel="whatsapp",
		provider=provider,
		account_doctype="WhatsApp Account",
		outcomes={"sent"},
		capabilities={"templates"},
		number_format=number_format,
	)


class _Adapter:
	"""The vendor boundary and ONLY the vendor boundary — a module-shaped double carrying one declaration.

	`template_variables` is the provider's own answer about a template and is faked here exactly as the
	existing send suite fakes it; what this suite is about — which number reaches the wire — runs for real.
	"""

	def __init__(self, declaration):
		self.DECLARATION = declaration

	def template_variables(self, account, template):
		return []


def _make_account():
	if frappe.db.exists("WhatsApp Account", _ACCOUNT):
		frappe.delete_doc("WhatsApp Account", _ACCOUNT, force=True, ignore_permissions=True)
	return frappe.get_doc({
		"doctype": "WhatsApp Account", "account_name": _ACCOUNT, "status": "Active",
		"url": "https://live-mt-server.wati.io/000003", "token": "number-format-probe-token",
		"custom_provider": "WATI",
		"custom_wati_channel_number": f"9190{int(hashlib.md5(_ACCOUNT.encode()).hexdigest(), 16) % 10**8:08d}",
	}).insert(ignore_permissions=True).name  # authz-ok: tier-c — test fixture, no user input


def _make_template(account):
	"""B11 DEVIATION, declared: `db_insert` bypasses the controller on purpose.

	`WhatsAppTemplates.after_insert` calls `make_post_request` against Meta's live API, so `doc.insert()`
	from a test would fire REAL provider traffic. The row is a WATI-mirrored read-only catalogue entry that
	only ever lands this way in production too (`templates_sync`), so no rule-shaping hook is being skipped
	— the hook being avoided is an outbound network call.
	"""
	full_name = f"{_TEMPLATE}-en"
	if frappe.db.exists("WhatsApp Templates", full_name):
		frappe.delete_doc("WhatsApp Templates", full_name, force=True, ignore_permissions=True)
	doc = frappe.new_doc("WhatsApp Templates")
	doc.update({
		"template_name": _TEMPLATE, "template": "<p>Hi</p>", "language_code": "en",
		"category": "UTILITY", "whatsapp_account": account, "actual_name": _TEMPLATE,
		"status": "APPROVED",
	})
	doc.name = full_name
	doc.db_insert()
	return doc.name


class _SendHarness(FrappeTestCase):
	"""One real lead, one real account, one real template, and the real `send_whatsapp`. The only things
	patched are the vendor boundary and the two operator switches."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		assert_masters_exist()
		cls.account = _make_account()
		cls.template = _make_template(cls.account)
		cls.lead = fx.make_lead()
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		frappe.delete_doc("CRM Lead", cls.lead.name, force=True, ignore_permissions=True)
		frappe.delete_doc("WhatsApp Templates", cls.template, force=True, ignore_permissions=True)
		frappe.delete_doc("WhatsApp Account", _ACCOUNT, force=True, ignore_permissions=True)
		frappe.db.commit()
		super().tearDownClass()

	def _send(self, number, adapter):
		"""Run the real send path and report `(output, marker_or_thunk, number_that_reached_the_wire)`.

		The wire number is read out of the deferred enqueue by RUNNING the thunk with `frappe.enqueue`
		patched — the outcome, not a call count. Nothing is enqueued and no provider is reached.
		"""
		frappe.db.set_value("CRM Lead", self.lead.name, "mobile_no", number)
		frappe.db.commit()
		enqueued = {}

		def _capture(_method, **kwargs):
			enqueued.update(kwargs)

		with patch.object(sends, "sends_enabled", return_value=True), \
		     patch.object(routing, "resolve_account_for_lead", return_value=self.account), \
		     patch.object(channel, "is_enabled", return_value=True), \
		     patch.object(resolve, "adapter_for", return_value=adapter), \
		     patch.object(frappe, "enqueue", _capture):
			output, deferred = sends.send_whatsapp(
				self.lead.name, "probe.number", self.template, {"probe.number": number},
			)
			if callable(deferred):
				deferred()
		return output, deferred, enqueued.get("to_number")


class TestTheDeclarationIsTheVocabulary(FrappeTestCase):
	"""B3 — a declaration that can say anything says nothing."""

	def test_an_adapter_cannot_declare_a_number_format_the_vocabulary_does_not_have(self):
		"""It fails at IMPORT, like an undeclarable outcome — not on the one message that needed it."""
		with self.assertRaises(ValueError):
			_declaration("Imaginary", "whatever-the-vendor-felt-like")

	def test_every_declared_format_is_reachable_and_distinct(self):
		"""The three spellings are the axis of real variation, and no two render the same string."""
		rendered = {fmt: _declaration("P", fmt).conform_number(_CANONICAL) for fmt in contract.NUMBER_FORMATS}
		self.assertEqual(rendered[contract.E164_PLUS], "+919876543210")
		self.assertEqual(rendered[contract.E164_PLAIN], _WIRE_FOR_WATI)
		self.assertIsNone(
			rendered[contract.NATIONAL],
			"a national-only provider cannot be handed a country code it has no way to measure off",
		)

	def test_wati_declares_the_spelling_its_own_transport_needs(self):
		"""Not a preference — WATI puts the number in a URL query, where a `+` decodes as a space."""
		self.assertEqual(wati.DECLARATION.number_format, contract.E164_PLAIN)
		self.assertNotIn("+", wati.DECLARATION.conform_number(_CANONICAL))


class TestTheHeadlineLock(_SendHarness):
	"""A number without a country code is REFUSED for WATI, and the SAME number is ACCEPTED once an
	adapter declares it acceptable. Both directions, through one unchanged code path."""

	def test_a_bare_number_is_refused_for_wati_and_nothing_is_queued(self):
		"""THE red. A bare number went to WATI, which resolved a dialling plan nobody had declared."""
		output, marker, wire = self._send(_BARE, _Adapter(wati.DECLARATION))

		self.assertEqual(output, sends.FAILED, "an ambiguous number must never reach a provider")
		self.assertIsNone(wire, "nothing may be queued for a number the provider would have to guess at")
		self.assertIn(_BARE, marker, "the step log must name the number that was refused")

	def test_the_same_bare_number_is_accepted_by_an_adapter_that_declares_it_acceptable(self):
		"""The other direction, and the reason this is a declaration rather than a rule. Same lead, same
		number, same send path — a provider that declares it takes national numbers gets it."""
		adapter = _Adapter(_declaration("Nationaler", contract.NATIONAL))

		output, _thunk, wire = self._send(_BARE, adapter)

		self.assertEqual(output, sends.SENT)
		self.assertEqual(wire, _BARE, "a national-format provider takes the subscriber digits as stored")

	def test_a_canonical_number_reaches_wati_as_digits_and_never_the_bare_form(self):
		"""The positive path, asserted as the value on the wire."""
		output, _thunk, wire = self._send(_CANONICAL, _Adapter(wati.DECLARATION))

		self.assertEqual(output, sends.SENT)
		self.assertEqual(wire, _WIRE_FOR_WATI)
		self.assertNotEqual(wire, _BARE, "the country code must survive the trip to the wire")

	def test_the_same_canonical_number_reaches_a_plus_provider_with_its_plus(self):
		"""Bolna's shape, proven today so it cannot be broken later. One number, two providers, two
		spellings, and the engine has no opinion about either."""
		adapter = _Adapter(_declaration("Voicer", contract.E164_PLUS))

		output, _thunk, wire = self._send(_CANONICAL, adapter)

		self.assertEqual(output, sends.SENT)
		self.assertEqual(wire, "+919876543210")

	def test_a_country_code_is_refused_for_a_national_only_provider(self):
		"""The symmetry that keeps the engine out of it: we cannot strip a country code we cannot measure,
		so we refuse rather than guess how many leading digits to drop."""
		adapter = _Adapter(_declaration("Nationaler", contract.NATIONAL))

		output, marker, wire = self._send(_CANONICAL, adapter)

		self.assertEqual(output, sends.FAILED)
		self.assertIsNone(wire)
		self.assertIn("Nationaler", marker, "the refusal must name the provider whose format was not met")

	def test_a_lead_with_no_number_at_all_is_a_different_refusal(self):
		"""Two data states, two markers. An author reading the step log must be able to tell "this patient
		has no phone number" from "this patient's number is not dialable by this provider"."""
		_output, missing, _wire = self._send("", _Adapter(wati.DECLARATION))
		_output, malformed, _wire = self._send(_BARE, _Adapter(wati.DECLARATION))

		self.assertIn("resolved to no number", missing)
		self.assertNotIn("resolved to no number", malformed)


class TestTheAdapterCannotAlterWhatItDeclared(FrappeTestCase):
	"""B7 divergence lock. `wati.send_template` still reduces the number with `channel.normalize_number`
	before it hits the transport. For a conformed number that MUST be a no-op — if it ever stops being
	one, the declaration and the wire have drifted and this goes red."""

	def test_the_declaration_is_exactly_what_the_transport_receives(self):
		seen = {}

		def _capture(account, to_number, template_name, broadcast_name, parameters=None, **kwargs):
			seen["to_number"] = to_number
			return {"result": True, "local_message_id": "probe"}

		for given in (_CANONICAL, "+919876543210", "+91 9876543210", _WIRE_FOR_WATI):
			with self.subTest(given=given):
				seen.clear()
				declared = wati.DECLARATION.conform_number(given)
				with patch.object(transport, "send_template_message", _capture):
					wati.send_template(None, declared or given, frappe._dict({
						"actual_name": _TEMPLATE, "template_name": _TEMPLATE, "name": _TEMPLATE,
					}))
				if declared is None:
					continue
				self.assertEqual(
					seen["to_number"], declared,
					"the adapter changed the number its own declaration produced — declaration and wire have drifted",
				)


class TestTheRunRoutesInsteadOfDying(_SendHarness):
	"""The engine half: a refusal is DATA the author routes on, exactly like `no mobile_no`. Not an
	exception, and never a journey marked Failed — a patient with a badly stored number must not kill a
	journey that is still correct for every other patient."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		fx.purge(_WORKFLOW)
		fx.arm_engine(True, cls)
		cls.workflow = fx.make_workflow(_WORKFLOW, [
			fx.trigger(to="wa"),
			fx.node("wa", "Send WhatsApp",
			        config={"contact_number": "crm_lead.mobile_no", "whatsapp_template": cls.template},
			        edges={sends.SENT: "sent_end", sends.FAILED: "failed_end"}),
			fx.node("sent_end", "Terminal"),
			fx.node("failed_end", "Terminal"),
		])
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		fx.purge(_WORKFLOW)
		super().tearDownClass()

	def test_a_number_the_provider_cannot_dial_leaves_by_the_failed_edge(self):
		frappe.db.set_value("CRM Lead", self.lead.name, "mobile_no", _BARE)
		frappe.db.commit()

		with patch.object(sends, "sends_enabled", return_value=True), \
		     patch.object(routing, "resolve_account_for_lead", return_value=self.account), \
		     patch.object(channel, "is_enabled", return_value=True), \
		     patch.object(resolve, "adapter_for", return_value=_Adapter(wati.DECLARATION)):
			run = fx.start_journey(self.workflow, self.lead.name, "start")
			interpreter.advance(frappe.get_doc(fx.JOURNEY_DT, run.name))

		run = frappe.get_doc(fx.JOURNEY_DT, run.name)
		self.assertEqual(run.status, "Done", "a badly stored number is a data state, not a system fault")
		self.assertEqual(run.current_node, "failed_end")
		details = " ".join(log["detail"] or "" for log in fx.logs(run.name))
		self.assertIn(_BARE, details, "the step log must say WHICH number could not be dialled")
