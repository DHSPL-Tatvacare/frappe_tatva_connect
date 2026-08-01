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

NOTHING HERE SENDS. The sends gate stays OFF, which is also what proves the recording happens BEFORE it:
a suppressed step still has to say who it was for, because "which number did this journey message?" is
asked of the failures more often than of the successes.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \\
        --module tatva_connect.workflow_engine.tests.test_who_we_reached
"""
import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.automation import sends
from tatva_connect.workflow_engine import refs
from tatva_connect.workflow_engine.tests import fixtures as fx

# The test subscriber this codebase already documents, in the two spellings the two providers take.
_SUBSCRIBER = "9876543210"
_AS_WATI_TAKES_IT = f"91{_SUBSCRIBER}"
_AS_BOLNA_TAKES_IT = f"+91{_SUBSCRIBER}"
_CANONICAL = f"+91{_SUBSCRIBER}"

_ACCOUNT = "ZZ Who We Reached Voice"


class TestOneContactIdentity(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.addClassCleanup(cls._drop_account)
		cls._drop_account()
		frappe.get_doc({
			"doctype": "CRM AI Voice Account", "account_name": _ACCOUNT,
			"api_key": "sk-test-never-real", "base_url": "https://api.bolna.ai", "enabled": 1,  # pragma: allowlist secret
		}).insert(ignore_permissions=True)

	@classmethod
	def _drop_account(cls):
		if frappe.db.exists("CRM AI Voice Account", _ACCOUNT):
			frappe.delete_doc("CRM AI Voice Account", _ACCOUNT, force=True, ignore_permissions=True)

	def setUp(self):
		self.lead = fx.make_lead(mobile_no=_CANONICAL)
		self.addCleanup(self._drop_lead, self.lead.name)

	def _drop_lead(self, name):
		if frappe.db.exists("CRM Lead", name):
			frappe.delete_doc("CRM Lead", name, force=True, ignore_permissions=True)
			frappe.db.commit()

	def _whatsapp(self, number):
		"""One WhatsApp send, dormant, returning what it recorded about who it reached."""
		context = {"crm_lead.mobile_no": number}
		sends.send_whatsapp(self.lead.name, "crm_lead.mobile_no", "any-template", context=context)
		return context

	def _voice(self, number):
		context = {"crm_lead.mobile_no": number}
		sends.send_voice(self.lead.name, "crm_lead.mobile_no", _ACCOUNT, "agent-1", context=context)
		return context

	# ---- THE GUARDRAIL ---------------------------------------------------------------------------

	def test_one_patient_two_channels_one_identity(self):
		"""THE row the cap stands on. Each channel is handed the number ITS provider takes, and both must
		record the same person. If these differ the cap counts one patient as two and never fires."""
		whatsapp = self._whatsapp(_AS_WATI_TAKES_IT)
		voice = self._voice(_AS_BOLNA_TAKES_IT)

		self.assertEqual(
			whatsapp[refs.CONTACT], voice[refs.CONTACT],
			"WhatsApp and voice wrote different identities for one patient — the cap would count two",
		)
		self.assertEqual(whatsapp[refs.CONTACT], _CANONICAL)

	def test_it_stores_the_canonical_form_and_not_what_wati_is_handed(self):
		"""The conformed string is what goes on the wire and must never be what is stored. WATI's own form
		carries no `+`, so recording it would be recording a second identity for the same subscriber."""
		recorded = self._whatsapp(_AS_WATI_TAKES_IT)[refs.CONTACT]

		self.assertEqual(recorded, _CANONICAL)
		self.assertNotEqual(recorded, _AS_WATI_TAKES_IT, "the conformed form was stored")
		self.assertTrue(recorded.startswith("+"), "the stored form is E.164 and carries its country")

	def test_each_channel_says_which_one_it_was(self):
		self.assertEqual(self._whatsapp(_AS_WATI_TAKES_IT)[refs.CHANNEL], sends.WHATSAPP)
		self.assertEqual(self._voice(_AS_BOLNA_TAKES_IT)[refs.CHANNEL], sends.VOICE)

	def test_a_number_that_is_real_nowhere_has_no_identity_rather_than_a_wrong_one(self):
		"""`to_e164` refuses what is not a real number anywhere. A raw fallback would be a SECOND identity
		for the same person — exactly the defect above — so the answer is blank: no cap identity, which is
		the ruled behaviour for a lead whose number cannot be canonicalised."""
		self.assertEqual(self._whatsapp("12")[refs.CONTACT], "")

	def test_a_suppressed_send_still_says_who_it_was_for(self):
		"""The sends gate is OFF throughout this suite, so every send above is suppressed — and every one
		of them recorded a recipient. That is the assertion: the recording happens before the gate."""
		self.assertFalse(sends.sends_enabled(), "the suite must run with sends dormant, or it proves nothing")

		self.assertEqual(self._whatsapp(_AS_WATI_TAKES_IT)[refs.CONTACT], _CANONICAL)
