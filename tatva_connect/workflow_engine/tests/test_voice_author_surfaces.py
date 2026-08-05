# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""THE SURFACES AN AUTHOR AND AN OPERATOR READ ON THE VOICE NODE — each one was confidently wrong.

Four defects, one family: every one of them is a screen that stated something untrue and gave no sign of
it. None broke the engine; all of them make the person configuring a patient call distrust it.

  #5   The From-number picker labelled every row by `telephony_provider`, so the one control whose whole
       job is picking a number offered `plivo · plivo · plivo · plivo`.
  #7   The recipient was `Recipient` on the voice node and `Contact number` on the WhatsApp node — two
       author-facing words for one thing. The word is `Mobile Number`, ruled 2026-08-05.
  #18  Bolna can be told to skip the agent's calling hours. The batch path already sent the flag; the
       single path — the one the node uses — had no way to say it and no field to say it with.
  #10  The caller-id was stored exactly as typed. `to_number` is conformed by the provider's declared
       `number_format`; `from_phone` was `.strip()`ed, so `08035303509` reached the provider as typed.

NOTHING HERE ARMS ANYTHING. The bypass switch is asserted DORMANT and is never written. No call can leave:
`requests.post` is patched and the assertions are made against the body that would have been sent, so what
is proven is the payload itself and not that a mock was called. The probe account is deleted in
`addCleanup`, so a failing assertion cannot leave a voice account on the bench.
"""
import unittest
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.automation import actions, registry as auto_registry
from tatva_connect.voice import channel
from tatva_connect.voice.adapters import bolna

_ACCOUNT = "Voice-author-surface-probe"
_CONNECTION = {"api_key": "probe-key", "base_url": "https://api.example.invalid", "from_phone": ""}


def _voice_params():
	return {p["name"]: p for p in actions.VERBS["AI Voice Call"]["params"]}


class _Response:
	"""A real enough response for `place_call` to read — status, json, content."""

	status_code = 200
	content = b"{}"

	def json(self):
		return {"execution_id": "probe-execution"}

	def raise_for_status(self):
		return None


class TestTheFromNumberPickerShowsNumbers(FrappeTestCase):
	"""#5 — `name` is what the control renders, and it was the carrier."""

	_RAW = [
		{"phone_number": "+919240289225", "telephony_provider": "plivo"},
		{"phone_number": "+919240289226", "telephony_provider": "plivo"},
	]

	def test_every_option_is_labelled_by_its_number(self):
		with patch.object(bolna, "_get", return_value=self._RAW):
			rows = bolna.list_phone_numbers(_CONNECTION)

		self.assertEqual([r["name"] for r in rows], ["+919240289225", "+919240289226"],
		                 "the picker renders `name`; labelling it by the carrier offered no numbers at all")
		self.assertEqual([r["id"] for r in rows], ["+919240289225", "+919240289226"])

	def test_the_carrier_survives_as_the_hint_the_existing_joiner_already_renders(self):
		"""`voice.api._listing` joins `name · status`. Putting the carrier on `status` means the hint costs
		no second joiner — so this locks that the adapter feeds the seam that already exists."""
		with patch.object(bolna, "_get", return_value=self._RAW):
			rows = bolna.list_phone_numbers(_CONNECTION)

		self.assertEqual(rows[0]["status"], "plivo")
		label = " · ".join(p for p in (rows[0].get("name"), rows[0].get("status")) if p) or rows[0]["id"]
		self.assertEqual(label, "+919240289225 · plivo")


class TestOneWordForAPersonsNumber(unittest.TestCase):
	"""#7 — LABELS ONLY. The stored column and the wire key are named here so a rename cannot reach them."""

	def test_the_voice_node_asks_for_a_mobile_number(self):
		self.assertEqual(_voice_params()["contact_number"]["label"], "Mobile Number")

	def test_the_whatsapp_node_asks_for_the_same_thing_in_the_same_words(self):
		params = {p["name"]: p for p in actions.VERBS["Send WhatsApp"]["params"]}
		self.assertEqual(params["contact_number"]["label"], "Mobile Number")

	def test_an_email_address_is_not_a_mobile_number(self):
		"""The ruling is about a PERSON'S PHONE NUMBER. Send Email's recipient is an email address, and
		giving it this label would be the vocabulary error in the other direction."""
		params = {p["name"]: p for p in actions.VERBS["Send Email"]["params"]}
		self.assertEqual(params["email_recipient"]["label"], "Recipient")

	def test_no_label_change_moved_a_stored_column_or_a_wire_key(self):
		"""`CRM Workflow Step Log.contact` is a shipped column the contact cap counts on, and `to_number`
		is the wire. A label ruling must never reach either."""
		self.assertEqual(_voice_params()["contact_number"]["name"], "contact_number")
		self.assertTrue(frappe.db.has_column("CRM Workflow Step Log", "contact"))
		self.assertIn("to_number", bolna.place_call.__code__.co_varnames)


class TestTheCallingHoursBypassIsTwoDeliberateActs(FrappeTestCase):
	"""#18 — declared like every other config field, shipped OFF, and gated behind a dormant switch."""

	def test_it_is_declared_on_the_node_like_any_other_field(self):
		field = _voice_params()["bypass_call_guardrails"]
		self.assertEqual(field["type"], "Check")
		self.assertIn("calling hours", field["label"],
		              "the label is the only thing an author reads before ticking it")

	def test_it_ships_off_because_an_unset_check_is_falsy(self):
		"""No `default` is declared, so an author who never meets the field cannot have armed it."""
		self.assertNotIn("default", _voice_params()["bypass_call_guardrails"])

	def test_the_switch_is_declared_and_hangs_off_the_channel(self):
		row = next(a for a in auto_registry.AUTOMATIONS if a.key == "AI Voice::Channel::bypass-guardrails")
		self.assertEqual(row.requires, "AI Voice::Channel::calls",
		                 "skipping the calling window is meaningless when no call is placed at all")
		self.assertTrue(row.purpose.strip(), "an operator reads `purpose` on the switch form")

	def test_the_switch_is_dormant_on_this_bench_read_from_the_database(self):
		enabled = frappe.db.get_value("CRM Tatva Automation", "AI Voice::Channel::bypass-guardrails", "enabled")
		self.assertIn(enabled, (0, None), "the bypass switch must never ship armed")
		self.assertFalse(channel.bypass_guardrails_enabled())

	def test_a_call_that_did_not_ask_for_it_sends_no_such_key(self):
		"""An absent key and a `false` mean the same to Bolna, and the absent one cannot be misread off a
		captured request as a deliberate choice to skip a patient's calling window."""
		with patch.object(bolna.requests, "post", return_value=_Response()) as post:
			bolna.place_call(_CONNECTION, "+919876543210", "agent-1", None, "journey::node")

		self.assertNotIn("bypass_call_guardrails", post.call_args.kwargs["json"])

	def test_the_single_path_sends_a_json_BOOL_not_the_batch_paths_string(self):
		"""The two paths differ by Bolna's own contract: `/call` takes a JSON body, `/batches` takes
		multipart form fields where the same flag is the string "true". Getting this backwards sends a
		truthy string into a JSON bool, or a Python `True` into a form field."""
		with patch.object(bolna.requests, "post", return_value=_Response()) as post:
			bolna.place_call(_CONNECTION, "+919876543210", "agent-1", None, "journey::node",
			                 bypass_call_guardrails=True)

		self.assertIs(post.call_args.kwargs["json"]["bypass_call_guardrails"], True)


class TestTheCallerIdIsStoredCanonical(FrappeTestCase):
	"""#10 — the one number in the app that was kept exactly as typed."""

	def setUp(self):
		self.addCleanup(self._drop)

	def _drop(self):
		if frappe.db.exists("CRM AI Voice Account", _ACCOUNT):
			frappe.delete_doc("CRM AI Voice Account", _ACCOUNT, force=True, ignore_permissions=True)

	def _account(self, from_phone):
		self._drop()
		doc = frappe.get_doc({
			"doctype": "CRM AI Voice Account", "account_name": _ACCOUNT, "provider": "bolna",
			"api_key": "probe-key", "base_url": "https://api.example.invalid", "from_phone": from_phone,
		})
		doc.insert(ignore_permissions=True)
		return doc

	def test_a_trunk_prefixed_number_is_stored_in_the_form_the_provider_declared(self):
		"""Bolna declares E164_PLUS. `08035303509` is what an operator types and what reached the wire."""
		doc = self._account("08035303509")
		stored = frappe.db.get_value("CRM AI Voice Account", doc.name, "from_phone")
		self.assertEqual(stored, "+918035303509")

	def test_separators_do_not_survive_into_the_stored_value(self):
		self.assertEqual(self._account("+91 98765 43210").from_phone, "+919876543210")

	def test_blank_stays_blank_because_no_caller_id_is_not_a_bad_one(self):
		self.assertEqual(self._account("").from_phone, "")

	def test_a_number_that_is_not_one_is_refused_and_the_refusal_says_the_shape_wanted(self):
		with self.assertRaises(frappe.ValidationError) as caught:
			self._account("12345")
		message = str(caught.exception)
		self.assertIn("12345", message, "the refusal names the number the operator typed")
		self.assertIn("+91", message, "and the shape it wanted, which nothing on the form said")

	def test_the_form_itself_states_the_format(self):
		"""Conforming is half the fix; the field said nothing about the format it silently required."""
		description = frappe.get_meta("CRM AI Voice Account").get_field("from_phone").description or ""
		self.assertIn("country code", description)
