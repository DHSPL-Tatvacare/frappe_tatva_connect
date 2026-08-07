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
       single path — the one the node uses — had no way to say it and no field to say it with. The first
       fix then dressed a PROVIDER REQUEST FIELD as a guardrail of ours: a second automation switch that
       had to be armed for the author's tick to count, wired to nothing, so both acts were inert. It is
       now what it always was — a capability a provider declares, offered to the author because Bolna
       declares it, hidden for one that does not, and carried through untouched. Placing a call at all is
       already gated by `AI Voice::Channel::calls`; that is the guardrail, and one is enough.
  #10  The caller-id was stored exactly as typed. `to_number` is conformed by the provider's declared
       `number_format`; `from_phone` was `.strip()`ed, so `08035303509` reached the provider as typed.

NOTHING HERE ARMS ANYTHING. No call can leave: `requests.post` is patched and the assertions are made
against the body that would have been sent, so what is proven is the payload itself and not that a mock
was called. The probe account is deleted in `addCleanup`, so a failing assertion cannot leave a voice
account on the bench.
"""
import unittest
from typing import ClassVar
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.automation import actions
from tatva_connect.automation import registry as auto_registry
from tatva_connect.channels import resolve
from tatva_connect.voice.adapters import bolna

_ACCOUNT = "Voice-author-surface-probe"
_CONNECTION = {"api_key": "probe-key", "base_url": "https://api.example.invalid", "from_phone": ""}


def _voice_params():
	"""What an author really gets — `params_of`, the one reader, not the raw declaration."""
	return {p["name"]: p for p in actions.params_of("AI Voice Call")}


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

	_RAW: ClassVar[list] = [
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


class TestTheCallingHoursBypassIsAProviderCapability(FrappeTestCase):
	"""#18 — a request field a provider either takes or does not, offered on that basis and nothing else."""

	def test_it_is_declared_on_the_node_like_any_other_field(self):
		field = _voice_params()["bypass_guardrails"]
		self.assertEqual(field["type"], "Check")
		self.assertIn("calling hours", field["label"],
		              "the label is the only thing an author reads before ticking it")

	def test_it_ships_off_because_an_unset_check_is_falsy(self):
		"""No `default` is declared, so an author who never meets the field cannot have armed it."""
		self.assertNotIn("default", _voice_params()["bypass_guardrails"])

	def test_the_field_is_offered_because_an_adapter_DECLARES_the_capability(self):
		"""The one reason it appears. Not a hardcoded field on a voice node — Bolna says it takes the
		instruction, so the author is offered it."""
		self.assertIn("bypass_guardrails", bolna.DECLARATION.capabilities)
		self.assertEqual(_voice_params()["bypass_guardrails"]["capability"], "bypass_guardrails")

	def test_a_provider_that_does_not_declare_it_never_shows_the_field(self):
		"""The half that makes the capability real. Without this the declaration is decoration and the
		field is hardcoded with extra steps."""
		with patch.object(resolve, "capabilities_for_channel", return_value={"recording"}):
			offered = [p["name"] for p in actions.params_of("AI Voice Call")]

		self.assertNotIn("bypass_guardrails", offered)
		self.assertIn("connection", offered, "only the capability-gated field is dropped, not the node")

	def test_the_palette_hides_it_too_so_the_two_surfaces_cannot_disagree(self):
		"""`node_types` feeds the canvas and `params_of` feeds the builder. One filter, both callers — a
		field advertised by one and hidden by the other is a support ticket nobody can reproduce."""
		from tatva_connect.workflow_engine import registry as wf_registry

		with patch.object(resolve, "capabilities_for_channel", return_value={"recording"}):
			palette = {t["type"]: t for t in wf_registry.node_types()}

		self.assertNotIn("bypass_guardrails",
		                 [f["name"] for f in palette["AI Voice Call"]["config"]])

	def test_the_capability_is_resolved_AT_REQUEST_TIME_never_at_import(self):
		"""Resolving a capability imports adapter modules, and this app keeps adapters lazy on purpose —
		`webhooks.registry.CHANNELS` holds module PATHS. Filtering while `NODE_TYPES` was being built made
		importing the workflow engine import Bolna and everything under it, so one bad adapter import
		would have taken down every workflow. The static declaration therefore stays UNFILTERED and the
		endpoint does the asking."""
		from tatva_connect.workflow_engine import registry as wf_registry

		static = [f["name"] for f in wf_registry.NODE_TYPES["AI Voice Call"]["config"]]
		self.assertIn("bypass_guardrails", static, "the import-time declaration asks no adapter anything")
		self.assertEqual(wf_registry.NODE_TYPES["AI Voice Call"]["channel"], "voice",
		                 "and it carries the channel so the endpoint can resolve without a second map")

	def test_every_capability_gated_field_can_actually_be_resolved(self):
		"""`offered_fields` shows a gated field when it cannot tell which channel to ask — permissive, so a
		declaration slip degrades to a visible control rather than a broken canvas. This is the lock that
		makes the permissiveness safe: a param may only name a capability that EXISTS in the vocabulary,
		on a verb that names the channel to ask. Both halves, or the filter quietly never runs."""
		from tatva_connect.channels import contract

		for verb, declared in actions.VERBS.items():
			for param in declared.get("params") or []:
				capability = param.get("capability")
				if not capability:
					continue
				self.assertIn(capability, contract.CAPABILITIES,
				              f"{verb}.{param['name']} names a capability no adapter can ever declare")
				self.assertTrue(declared.get("outcomes_channel"),
				                f"{verb} gates {param['name']} on a capability but names no channel to ask")

	def test_no_second_switch_governs_it(self):
		"""It was briefly an automation switch. A provider request field is not a guardrail of ours, and
		the tick it silently voided was the `broken_dependency` state with none of the reporting."""
		keys = {a.key for a in auto_registry.AUTOMATIONS}
		self.assertNotIn("AI Voice::Channel::bypass-guardrails", keys)
		self.assertIn("AI Voice::Channel::calls", keys, "placing a call at all IS the guardrail")

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
			                 bypass_guardrails=True)

		self.assertIs(post.call_args.kwargs["json"]["bypass_call_guardrails"], True)

	def test_our_word_goes_in_and_the_vendors_word_goes_out(self):
		"""The boundary the channel contract exists to draw: `bypass_guardrails` is the one author-facing
		word, `bypass_call_guardrails` is Bolna's spelling, and the translation happens in the adapter."""
		self.assertIn("bypass_guardrails", bolna.place_call.__code__.co_varnames)
		with patch.object(bolna.requests, "post", return_value=_Response()) as post:
			bolna.place_call(_CONNECTION, "+919876543210", "agent-1", None, "journey::node",
			                 bypass_guardrails=True)

		self.assertNotIn("bypass_guardrails", post.call_args.kwargs["json"],
		                 "our word must not leak onto the wire")


class TestTheTickActuallyReachesTheProvider(FrappeTestCase):
	"""THE DEFECT THAT MADE ALL OF THE ABOVE DECORATION: the field was declared, the switch was declared,
	and NOTHING carried the value. `_action_place_voice_call` did not pass it, `send_voice` had no such
	parameter, and `_deliver_voice` called the adapter without it — so the adapter's `False` default won
	every time and an author who ticked the box got no bypass whether the switch was on or off.

	Each hop is asserted separately, because the value was lost at a different one each time.
	"""

	def test_the_handler_hands_the_authors_tick_to_the_send_path(self):
		from tatva_connect.automation import actions as verbs

		action = frappe._dict({
			"contact_number": "mobile_no", "connection": "acct", "agent_id": "agent-1",
			"from_override": None, "agent_values": [], "bypass_guardrails": 1,
		})
		with patch.object(verbs.sends, "send_voice", return_value=("placed", None)) as send, \
		     patch.object(verbs, "resolve_target", return_value=(None, "LEAD-1")):
			verbs._action_place_voice_call(action, "LEAD-1", {}, None, None)

		self.assertIs(send.call_args.kwargs["bypass_guardrails"], True)

	def test_an_untouched_field_hands_over_False_rather_than_None(self):
		"""`None` and `False` behave alike here, but the adapter's contract is a bool and a `None` would
		reach the wire check as an untyped absence."""
		from tatva_connect.automation import actions as verbs

		action = frappe._dict({
			"contact_number": "mobile_no", "connection": "acct", "agent_id": "agent-1",
			"from_override": None, "agent_values": [],
		})
		with patch.object(verbs.sends, "send_voice", return_value=("placed", None)) as send, \
		     patch.object(verbs, "resolve_target", return_value=(None, "LEAD-1")):
			verbs._action_place_voice_call(action, "LEAD-1", {}, None, None)

		self.assertIs(send.call_args.kwargs["bypass_guardrails"], False)

	def test_the_deferred_job_hands_it_to_the_adapter(self):
		"""The last hop, and the one that was silently dropping it: the job ran, the call was placed, and
		the instruction the author gave was simply not in the request."""
		from tatva_connect.automation import sends

		with patch.object(bolna, "place_call", return_value={}) as place, \
		     patch.object(sends, "_write_voice_call_log"), \
		     patch("tatva_connect.voice.api.connection_for", return_value=_CONNECTION):
			sends._deliver_voice("acct", "+919876543210", "agent-1", None, "LEAD-1",
			                     correlation="journey::node", bypass_guardrails=True)

		self.assertIs(place.call_args.kwargs["bypass_guardrails"], True)


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
