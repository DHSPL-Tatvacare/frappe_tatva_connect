# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""W12 §2b — one patient is ONE contact identity, whichever channel reached them.

THE DEFECT THIS EXISTS TO CLOSE. A phone number has three jobs and they must never be confused
(`phone.py`): MATCH is digits, STORE is the canonical `+91…`, SEND is whatever THIS provider accepts.
WATI is handed `91…` and Bolna `+91…` — the same subscriber, two strings.

The obvious place to log who a journey reached is where the send path hands the provider its number, and
that is the CONFORMED string. Log that, and one patient becomes two rows: WhatsApp writes `91…`, voice
writes `+91…`, and a contact cap counting distinct numbers counts them as two people and SILENTLY NEVER
FIRES. Nothing goes red; the guardrail is simply decoration.

So the step log stores `to_e164` — the STORE form — and the adapters go on conforming at the provider
boundary as they already do. `test_one_patient_two_channels_one_identity` is that rule as an assertion,
and it is the row the whole guardrail stands on.

NOTHING HERE REACHES A PROVIDER. The send verbs return a deferred thunk and nothing calls it, so the
armed path is exercised without a message existing; the gate is patched rather than armed in the DB, so
no live switch is left behind either.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \\
        --module tatva_connect.workflow_engine.tests.test_who_we_reached
"""
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.automation import sends
from tatva_connect.channels import resolve
from tatva_connect.whatsapp import channel, routing, wati
from tatva_connect.workflow_engine import refs
from tatva_connect.workflow_engine.tests import fixtures as fx

# The test subscriber this codebase already documents. A lead STORES the canonical form and that is what
# every send is fed; each provider's own spelling is produced INSIDE the send, at the adapter boundary.
_SUBSCRIBER = "9876543210"
_CANONICAL = f"+91{_SUBSCRIBER}"
_AS_WATI_TAKES_IT = f"91{_SUBSCRIBER}"      # E164_PLAIN — WATI puts the number in a URL, where `+` is a space
_AS_BOLNA_TAKES_IT = f"+91{_SUBSCRIBER}"    # E164_PLUS

class _WatiDouble:
	"""The VENDOR BOUNDARY, and the only thing about WhatsApp this suite fakes.

	It carries WATI's REAL declaration, so the number that reaches the wire is conformed by the same
	`E164_PLAIN` rule production uses — which is the whole point of the comparison. What it replaces is
	`template_variables`, whose live implementation asks WATI over HTTP: a test must reach no provider.
	"""

	DECLARATION = wati.DECLARATION

	def template_variables(self, account, template):
		return []


_WATI_DOUBLE = _WatiDouble()

_VOICE_ACCOUNT = "ZZ Who We Reached Voice"
_WA_ACCOUNT = "ZZ Who We Reached WhatsApp"
_TEMPLATE = "zz-who-we-reached"


class TestOneContactIdentity(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.addClassCleanup(cls._drop_fixtures)
		cls._drop_fixtures()
		frappe.get_doc({
			"doctype": "CRM AI Voice Account", "account_name": _VOICE_ACCOUNT,
			"api_key": "sk-test-never-real", "base_url": "https://api.bolna.ai", "enabled": 1,  # pragma: allowlist secret
		}).insert(ignore_permissions=True)
		cls.account = fx.whatsapp_account(_WA_ACCOUNT)
		cls.template = fx.whatsapp_template(_TEMPLATE, cls.account)
		frappe.db.commit()

	@classmethod
	def _drop_fixtures(cls):
		for doctype, name in (
			("CRM AI Voice Account", _VOICE_ACCOUNT),
			("WhatsApp Templates", f"{_TEMPLATE}-en"),
			("WhatsApp Account", _WA_ACCOUNT),
		):
			if frappe.db.exists(doctype, name):
				frappe.delete_doc(doctype, name, force=True, ignore_permissions=True)
		frappe.db.commit()

	def setUp(self):
		self.lead = fx.make_lead(mobile_no=_CANONICAL)
		self.addCleanup(self._drop_lead, self.lead.name)

	def _drop_lead(self, name):
		if frappe.db.exists("CRM Lead", name):
			frappe.delete_doc("CRM Lead", name, force=True, ignore_permissions=True)
			frappe.db.commit()

	def _whatsapp(self, number):
		"""The REAL WhatsApp send path, to the point where it queues. Returns `(context, wire_number)`.

		Only the vendor boundary and the two operator switches are patched — the same shape
		`test_number_format._SendHarness` proved. `frappe.enqueue` is captured rather than mocked away, so
		the number that would have gone ON THE WIRE is an outcome this test can read, not an assumption.
		Nothing is enqueued and no provider is reached.

		It has to run ARMED at all because a suppressed send contacts nobody and therefore records nobody,
		which is what `test_a_suppressed_send_records_nobody` locks.
		"""
		context = {"crm_lead.mobile_no": number}
		enqueued = {}
		with patch.object(sends, "sends_enabled", return_value=True), \
		     patch.object(routing, "resolve_account_for_lead", return_value=self.account), \
		     patch.object(channel, "is_enabled", return_value=True), \
		     patch.object(resolve, "adapter_for", return_value=_WATI_DOUBLE), \
		     patch.object(frappe, "enqueue", lambda _m, **kw: enqueued.update(kw)):
			output, deferred = sends.send_whatsapp(
				self.lead.name, "crm_lead.mobile_no", self.template, context=context,
			)
			if callable(deferred):
				deferred()
		return context, enqueued.get("to_number"), output, deferred

	def _queued(self, number):
		"""A send that really reached the queue, or a failure that SAYS WHY.

		Without this, a harness whose fixture stops the send early reports `wire is None` and sends the
		reader hunting for a bug in the code under test rather than in the setup.
		"""
		context, wire, output, detail = self._whatsapp(number)
		if not callable(detail):
			raise AssertionError(f"the send never queued — {output}: {detail}")
		return context, wire

	def _voice(self, number):
		context = {"crm_lead.mobile_no": number}
		with patch("tatva_connect.automation.sends.sends_enabled", return_value=True), \
		     patch("tatva_connect.voice.channel.is_enabled", return_value=True), \
		     patch("tatva_connect.automation.sends._agent_variables", return_value=({}, [])):
			sends.send_voice(self.lead.name, "crm_lead.mobile_no", _VOICE_ACCOUNT, "agent-1", context=context)
		return context

	# ---- THE GUARDRAIL ---------------------------------------------------------------------------

	def test_one_patient_two_channels_one_identity(self):
		"""THE row the cap stands on. Each channel is handed the number ITS provider takes, and both must
		record the same person. If these differ the cap counts one patient as two and never fires."""
		whatsapp, _wire = self._queued(_CANONICAL)
		voice = self._voice(_CANONICAL)

		self.assertEqual(
			whatsapp[refs.CONTACT], voice[refs.CONTACT],
			"WhatsApp and voice wrote different identities for one patient — the cap would count two",
		)
		self.assertEqual(whatsapp[refs.CONTACT], _CANONICAL)

	def test_what_goes_on_the_wire_is_not_what_is_recorded(self):
		"""The strongest form of the rule, in ONE send: WATI is really handed `91…` while the step really
		records `+91…`. A single implementation could satisfy either assertion alone by accident; nothing
		satisfies both except logging the STORE form and conforming only at the provider boundary."""
		context, wire = self._queued(_CANONICAL)

		self.assertEqual(wire, _AS_WATI_TAKES_IT, "the wire number is not WATI's declared format")
		self.assertEqual(context[refs.CONTACT], _CANONICAL)
		self.assertNotEqual(context[refs.CONTACT], wire, "the CONFORMED number was recorded")

	def test_each_channel_says_which_one_it_was(self):
		self.assertEqual(self._queued(_CANONICAL)[0][refs.CHANNEL], sends.WHATSAPP)
		self.assertEqual(self._voice(_CANONICAL)[refs.CHANNEL], sends.VOICE)

	def test_a_number_that_is_real_nowhere_never_reaches_a_provider_at_all(self):
		"""The gate refuses it before the queue, so there is no wire number AND no identity to record.

		This is why a blank is the right answer for an uncanonicalisable number rather than a raw
		fallback: such a number never becomes a contact at all, so a raw one could only ever be a SECOND
		identity for somebody the engine could not message anyway."""
		context, wire, output, detail = self._whatsapp("12")

		self.assertEqual(output, sends.FAILED)
		self.assertIsNone(wire, "an unusable number was handed to a provider")
		self.assertEqual(context.get(refs.CONTACT, ""), "", f"a refused send recorded a contact: {detail}")

	def test_a_suppressed_send_records_nobody(self):
		"""A dormant bench contacts nobody, so it must write no contact — the ceiling counts this column,
		and a suppressed send that left a row behind would spend a patient's allowance on a message that
		never existed. The moment an operator armed sends, the cap would already be counting phantoms."""
		self.assertFalse(sends.sends_enabled(), "the bench must be dormant here, or this proves nothing")
		context = {"crm_lead.mobile_no": _CANONICAL}

		output, marker = sends.send_whatsapp(
			self.lead.name, "crm_lead.mobile_no", "any-template", context=context,
		)

		self.assertEqual(output, sends.SENT, "a dormant send must keep its `sent` edge, never `failed`")
		self.assertEqual(marker, sends.DORMANT_MARKER)
		self.assertNotIn(refs.CONTACT, context, "a suppressed send recorded a contact it never made")
