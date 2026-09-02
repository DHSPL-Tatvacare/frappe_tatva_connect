# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A stranger who writes to a programme's number becomes a lead — and NOTHING ELSE CHANGES.

`ingest._targets` drops a message whose number matches no lead routing to the receiving account. That
drop is correct and stays correct; enrolment is a second answer offered at the same fork, and the whole
risk of it is that the fork itself moves. So this file is written the other way round from most: the
first and largest class proves the OLD path is untouched, and only then that the new one works.

WHAT IS PROVEN, AND WHY EACH ONE IS HERE
----------------------------------------
DORMANCY, three ways. The switch off, the account unticked, and both off. Each must drop exactly as
today — no lead, no row. Two independent gates, so each is proven to close on its own; a feature that
needed both to be wrong before it misfired would still be one edit away from misfiring.

THE OLD PATH. A sender who IS already a lead never reaches enrolment at all, armed or not. That is the
whole of today's traffic, and it is the assertion that says today's traffic is unaffected.

BACKFILL. A history pull re-reads months of conversations. Every stranger in them would be enrolled as
though they had just written in — hundreds of leads born at once, each firing assignment and whatever a
Created flow does. `apply_historical` sets `in_workflow`; this proves enrolment honours it.

OUTBOUND ECHO. A campaign sent from the provider's own portal echoes back one event per recipient. If
those enrolled, one marketing send would mint a lead per number. Only live INBOUND may enrol.

THE ROUND TRIP. The grain is read back out of the routing rules, so the lead a message creates must
resolve to the very account that message arrived on. Asserted with the real resolver, not by comparing
the tuple we just passed in — that would only prove the test can copy a variable.

Nothing reaches a provider: no transport is touched, media is never fetched (no media on these events),
and every row is rolled back.
"""
import frappe
from frappe.tests.utils import FrappeTestCase
from unittest.mock import patch

from tatva_connect.channels import event as event_mod
from tatva_connect.whatsapp import channel, enrol, ingest, routing

_ACCOUNT = "_TC Enrol Probe Account"
_VERTICAL, _GROUP = "_TC Enrol Line", "_TC Enrol Group"
_STRANGER = "919000000001"
_KNOWN = "919000000002"


def _event(kind="inbound", number=_STRANGER, sender_name="Probe Sender", **over):
	"""One normalized event, built the ONE way an adapter builds it — never a hand-made dict, or the
	test would be asserting against a shape the product does not produce."""
	fields = {
		"channel": "whatsapp", "provider": "WATI", "account": _ACCOUNT, "kind": kind,
		"subject_number": number, "text": "hello", "wamid": f"wamid.{number}.{kind}",
		"provider_message_id": f"pmid.{number}.{kind}", "raw": {"senderName": sender_name},
	}
	if kind != "inbound":
		fields["outcome"] = "sent"
		fields["correlation_id"] = f"corr.{number}"
	fields.update(over)
	return event_mod.build(**fields)


class _EnrolmentCase(FrappeTestCase):
	"""A routed account and its rule, plus one lead that is already known on it."""

	def setUp(self):
		self.addCleanup(frappe.db.rollback)
		# Each grain master autonames from its own `<x>_name`, which is also `reqd`.
		for dt, field, name in (("CRM Vertical", "vertical_name", _VERTICAL), ("CRM Group", "group_name", _GROUP)):
			if not frappe.db.exists(dt, name):
				frappe.get_doc({"doctype": dt, field: name}).insert(
					ignore_permissions=True, ignore_if_duplicate=True
				)
		if not frappe.db.exists("WhatsApp Account", _ACCOUNT):
			frappe.get_doc({
				"doctype": "WhatsApp Account", "account_name": _ACCOUNT, "name": _ACCOUNT,
				"status": "Active", "custom_provider": "WATI",
			}).insert(ignore_permissions=True, ignore_if_duplicate=True)
		self.rule = frappe.get_doc({
			"doctype": "CRM WhatsApp Routing", "vertical": _VERTICAL, "psp_group": _GROUP,
			"whatsapp_account": _ACCOUNT,
		}).insert(ignore_permissions=True, ignore_if_duplicate=True)
		self.known = frappe.get_doc({
			"doctype": "CRM Lead", "first_name": "Known", "mobile_no": "+" + _KNOWN,
			"custom_vertical": _VERTICAL, "custom_group": _GROUP,
		}).insert(ignore_permissions=True)

	def _tick(self, on=True):
		frappe.db.set_value("WhatsApp Account", _ACCOUNT, enrol.ACCOUNT_FLAG, 1 if on else 0)

	def _leads_on(self, number):
		"""Leads on this number IN THIS TEST'S GRAIN.

		Scoped, not global: a bench carries real leads, and one of them already held the first number
		this file picked. `_upsert_one` dedups on (phone, product line, group), so a lead on the same
		number in another grain is a DIFFERENT lead by the product's own rule — counting it would make
		the assertion wrong rather than strict.
		"""
		return frappe.get_all(
			"CRM Lead",
			filters={
				"mobile_no": ["in", ["+" + number, number]],
				"custom_vertical": _VERTICAL,
				"custom_group": _GROUP,
			},
			pluck="name",
		)

	def _targets(self, event, switch=True, **kw):
		"""Arm the enrolment switch and NOTHING ELSE.

		`return_value=True` would arm every switch in the app, including the workflow engine — and a
		lead insert then starts a journey inline (`enqueue ... now=in_test`), which COMMITS, so rows
		escaped the rollback and leaked into the next test. Keying on the switch under test is both the
		fix and the better assertion: it proves enrolment needs no other switch armed.
		"""
		with patch(
			"tatva_connect.automation.is_enabled",
			side_effect=lambda key: switch and key == channel.SWITCH_ENROLMENT,
		):
			return ingest._targets(event, **kw)


class TestTheOldPathIsUntouched(_EnrolmentCase):
	"""The burden of proof: every way this can be off, and the traffic that never reaches it."""

	def test_the_switch_off_drops_the_stranger_exactly_as_today(self):
		self._tick(True)
		self.assertEqual(self._targets(_event(), switch=False, enrol_unknown=True), [])
		self.assertEqual(self._leads_on(_STRANGER), [], "a dormant switch minted a lead")

	def test_the_account_untick_drops_the_stranger_exactly_as_today(self):
		self._tick(False)
		self.assertEqual(self._targets(_event(), enrol_unknown=True), [])
		self.assertEqual(self._leads_on(_STRANGER), [], "an unticked account minted a lead")

	def test_a_caller_that_does_not_ask_to_enrol_drops_the_stranger(self):
		"""The default. Every existing caller of `_targets` passes nothing, and must behave as before."""
		self._tick(True)
		self.assertEqual(self._targets(_event()), [])
		self.assertEqual(self._leads_on(_STRANGER), [])

	def test_a_sender_who_is_already_a_lead_never_reaches_enrolment(self):
		"""Today's whole traffic. Armed or dormant, a known number resolves the way it always did."""
		self._tick(True)
		for switch in (True, False):
			with self.subTest(switch=switch):
				self.assertEqual(
					self._targets(_event(number=_KNOWN), switch=switch, enrol_unknown=True), [self.known.name]
				)
		self.assertEqual(len(self._leads_on(_KNOWN)), 1, "a known sender was enrolled a second time")

	def test_a_backfilled_message_never_enrols(self):
		"""`apply_historical` sets `in_workflow`; a history pull must file old messages, not invent patients."""
		self._tick(True)
		frappe.flags.in_workflow = True
		self.addCleanup(lambda: frappe.flags.pop("in_workflow", None))
		self.assertEqual(self._targets(_event(), enrol_unknown=True), [])
		self.assertEqual(self._leads_on(_STRANGER), [], "a backfill enrolled a stranger")

	def test_an_outbound_echo_never_enrols(self):
		"""A portal campaign echoes one event per recipient — enrolling those is a lead per number sent to."""
		self._tick(True)
		self.assertEqual(ingest._targets(_event(kind="outbound_echo")), [])
		self.assertEqual(self._leads_on(_STRANGER), [])

	def test_an_inactive_account_never_enrols(self):
		"""Its own messages already resolve to nothing, so a lead born here could never be reached again."""
		self._tick(True)
		frappe.db.set_value("WhatsApp Account", _ACCOUNT, "status", "Inactive")
		self.assertEqual(self._targets(_event(), enrol_unknown=True), [])
		self.assertEqual(self._leads_on(_STRANGER), [])


class TestTheStrangerIsEnrolled(_EnrolmentCase):
	"""Armed and ticked, the drop becomes a lead — at the grain the rules already declare."""

	def setUp(self):
		super().setUp()
		self._tick(True)

	def test_the_stranger_becomes_a_lead_the_message_can_land_on(self):
		targets = self._targets(_event(), enrol_unknown=True)
		self.assertEqual(len(targets), 1)
		lead = frappe.get_doc("CRM Lead", targets[0])
		self.assertEqual(lead.mobile_no, "+" + _STRANGER)
		self.assertEqual(lead.source, enrol.SOURCE)
		self.assertEqual(lead.first_name, "Probe Sender", "the sender's own profile name was dropped")

	def test_the_new_lead_routes_back_to_the_account_it_wrote_to(self):
		"""The round trip, asked of the REAL resolver — the point of reading the grain off the rules."""
		lead = frappe.get_doc("CRM Lead", self._targets(_event(), enrol_unknown=True)[0])
		self.assertEqual(routing.resolve_account_for_lead(lead), _ACCOUNT)

	def test_a_second_message_joins_the_same_lead_instead_of_making_another(self):
		"""The number is the identity, and `_upsert_one` owns that — this proves we did not bypass it."""
		first = self._targets(_event(), enrol_unknown=True)
		second = self._targets(_event(), enrol_unknown=True)
		self.assertEqual(first, second)
		self.assertEqual(len(self._leads_on(_STRANGER)), 1)

	def test_a_sender_with_no_profile_name_is_still_enrolled(self):
		"""A nameless sender is still a person; the brain fills its own placeholder rather than refusing."""
		self.assertEqual(len(self._targets(_event(sender_name=""), enrol_unknown=True)), 1)

	def test_an_account_that_declares_no_single_grain_enrols_nobody(self):
		"""No rule, or two equally-broad ones: inventing a grain is the guess reading the rules avoids."""
		with patch.object(routing, "grain_for_account", return_value=None):
			self.assertEqual(self._targets(_event(), enrol_unknown=True), [])
		self.assertEqual(self._leads_on(_STRANGER), [])
