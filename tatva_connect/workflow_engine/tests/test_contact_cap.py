# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""W12 — the ceiling on how often ONE NUMBER is contacted, whichever workflow is doing the contacting.

Every workflow is individually reasonable and the patient is on six of them. The cap is the only place
that asks the question none of them can: how many times has this person already been contacted?

IT COUNTS A PERSON, NOT A CHANNEL. WhatsApp and voice count together against one number, which only
works because the step log stores the CANONICAL form of the address — `test_who_we_reached` is the lock
on that, and `test_whatsapp_and_voice_count_against_the_same_person` is what it buys.

THE REFUSAL IS AN ORDINARY `failed`. It rides the path a withdrawn consent and an unusable number
already take, so a capped send is something the author's own branch already handles: the send stops, the
journey does not, and no new mechanism appears in anybody's graph.

DORMANT AT REST. The settings ship OFF and every test that arms them registers the restore with
`addCleanup` BEFORE writing, so a failure between the two still leaves the bench off.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \\
        --module tatva_connect.workflow_engine.tests.test_contact_cap
"""
from unittest.mock import patch

import frappe
from frappe.model import no_value_fields
from frappe.tests.utils import FrappeTestCase

from tatva_connect.automation import contact_cap, sends
from tatva_connect.workflow_engine.tests import fixtures as fx

SETTINGS_DT = contact_cap.SETTINGS_DT
STEP_LOG_DT = fx.STEP_LOG_DT
_NUMBER = "+919876543210"
_OTHER = "+919876543211"
_WF = "ZZ Contact Cap"


def _reset_settings():
	"""Put the Single back to the row that SHIPPED — every value field, not just the switch.

	Never "whatever it was": restoring the previous value is what propagates a poisoned baseline. And never
	a list typed here either — `frappe.new_doc` applies the doctype's own defaults and casts them, so the
	JSON stays the one statement of what shipped. Restoring only `enabled` is what left this bench carrying
	a maximum of 1 that no operator chose, with a dormant switch hiding it.
	"""
	shipped = frappe.new_doc(SETTINGS_DT)
	settings = frappe.get_doc(SETTINGS_DT)
	for field in shipped.meta.fields:
		if field.fieldtype not in no_value_fields:
			settings.set(field.fieldname, shipped.get(field.fieldname))
	settings.save(ignore_permissions=True)
	frappe.db.commit()
	frappe.clear_cache(doctype=SETTINGS_DT)


class _CapCase(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		fx.purge(_WF)
		cls.addClassCleanup(fx.purge, _WF)
		cls.workflow = fx.make_workflow(_WF, [fx.trigger(to="n1"), fx.node("n1", "Terminal")])

	def setUp(self):
		# Registered BEFORE anything is armed, so an abort mid-setUp still disarms — the shape
		# `fixtures.arm_engine` was rewritten into after a suite left the engine on.
		self.addCleanup(_reset_settings)
		self.addCleanup(self._purge_steps)
		self.lead = fx.make_lead()
		self.addCleanup(self._drop_lead, self.lead.name)
		self.journey = fx.start_journey(self.workflow, self.lead.name, "n1")

	def _drop_lead(self, name):
		if frappe.db.exists("CRM Lead", name):
			frappe.delete_doc("CRM Lead", name, force=True, ignore_permissions=True)
			frappe.db.commit()

	def _purge_steps(self):
		for journey in frappe.get_all(fx.JOURNEY_DT, filters={"workflow": _WF}, pluck="name"):
			frappe.db.delete(STEP_LOG_DT, {"journey": journey})
		frappe.db.delete(fx.JOURNEY_DT, {"workflow": _WF})
		frappe.db.commit()

	def _arm(self, most=2, count=30, unit="Days"):
		settings = frappe.get_doc(SETTINGS_DT)
		settings.update({"enabled": 1, "max_contacts": most, "window_count": count, "window_unit": unit})
		settings.save(ignore_permissions=True)
		frappe.db.commit()
		frappe.clear_cache(doctype=SETTINGS_DT)

	def _already_contacted(self, times, channel=None, contact=_NUMBER, days_ago=0):
		"""Steps the engine really wrote — the cap counts its own audit trail and nothing else."""
		for _ in range(times):
			row = frappe.get_doc({
				"doctype": STEP_LOG_DT, "journey": self.journey.name, "subject_name": self.lead.name,
				"node_id": "n1", "node_type": "Send WhatsApp", "outcome": "sent",
				"channel": channel or sends.WHATSAPP, "contact": contact, "duration_ms": 1,
			}).insert(ignore_permissions=True)
			if days_ago:
				frappe.db.set_value(
					STEP_LOG_DT, row.name, "creation",
					frappe.utils.add_to_date(frappe.utils.now_datetime(), days=-days_ago),
					update_modified=False,
				)
		frappe.db.commit()


class TestTheCap(_CapCase):
	def test_it_ships_off_and_refuses_nothing(self):
		"""Dormant by default. Until an operator turns it on, no send is ever counted or stopped."""
		self._already_contacted(50)

		self.assertIsNone(contact_cap.refusal(sends.WHATSAPP, _NUMBER))

	def test_under_the_limit_the_send_goes(self):
		self._arm(most=2)
		self._already_contacted(1)

		self.assertIsNone(contact_cap.refusal(sends.WHATSAPP, _NUMBER))

	def test_at_the_limit_the_send_is_refused(self):
		self._arm(most=2)
		self._already_contacted(2)

		self.assertTrue(contact_cap.refusal(sends.WHATSAPP, _NUMBER))

	def test_the_refusal_reads_like_a_reason_a_person_would_give(self):
		"""It lands in the step log's `detail` and on the operator's screen. "error" is not a reason."""
		self._arm(most=2, count=30, unit="Days")
		self._already_contacted(2)

		reason = contact_cap.refusal(sends.WHATSAPP, _NUMBER)

		self.assertTrue(reason.startswith("failed:"), "it must ride the existing failed vocabulary")
		self.assertIn("2", reason, "it must say how many")
		self.assertIn("30 days", reason, "it must say over what period")

	def test_whatsapp_and_voice_count_against_the_same_person(self):
		"""THE point of counting a canonical address. A patient called once and messaged once has been
		contacted TWICE, and a cap that counted per channel would let each channel spend the whole
		allowance on its own."""
		self._arm(most=2)
		self._already_contacted(1, channel=sends.WHATSAPP)
		self._already_contacted(1, channel=sends.VOICE)

		self.assertTrue(contact_cap.refusal(sends.VOICE, _NUMBER), "the two channels were counted apart")

	def test_email_is_not_counted(self):
		"""A ceiling enforced on a phone number cannot have an address folded into it and still mean
		anything — and an email step is recorded, so this has to be a counting rule, not a logging one."""
		self._arm(most=2)
		self._already_contacted(5, channel=sends.EMAIL, contact="someone@example.test")

		self.assertIsNone(contact_cap.refusal(sends.EMAIL, "someone@example.test"))
		self.assertIsNone(contact_cap.refusal(sends.WHATSAPP, _NUMBER), "an email spent a number's allowance")

	def test_another_number_has_its_own_allowance(self):
		self._arm(most=2)
		self._already_contacted(2)

		self.assertIsNone(contact_cap.refusal(sends.WHATSAPP, _OTHER))

	def test_the_window_rolls_rather_than_resetting(self):
		"""A calendar month lets a patient be contacted the maximum on its last day and the maximum again
		the next morning. Rolling is what "in a month" means to the person being messaged."""
		self._arm(most=2, count=30, unit="Days")
		self._already_contacted(2, days_ago=31)

		self.assertIsNone(contact_cap.refusal(sends.WHATSAPP, _NUMBER), "steps outside the window still counted")

	def test_a_number_with_no_canonical_identity_is_not_counted(self):
		"""`_canonical_contact` gives a number that is real nowhere a BLANK identity. Blank must never
		collect a whole site's traffic into one bucket and cap everybody at once."""
		self._arm(most=2)
		self._already_contacted(5, contact="")

		self.assertIsNone(contact_cap.refusal(sends.WHATSAPP, ""))

	def test_a_rejected_hand_off_gives_the_slot_back(self):
		"""Counting at the DECISION to send is what stops two in-flight sends both passing. Its cost is
		that a provider outage would eat a patient's window for messages they never received — so a
		hand-off refused outright is un-counted. The step stays readable; only its claim is withdrawn."""
		self._arm(most=2)
		self._already_contacted(2)
		self.assertTrue(contact_cap.refusal(sends.WHATSAPP, _NUMBER), "the fixture must start capped")

		self.assertTrue(contact_cap.void(self.journey.name, "n1"))

		self.assertIsNone(contact_cap.refusal(sends.WHATSAPP, _NUMBER))
		self.assertEqual(
			frappe.db.count(STEP_LOG_DT, {"journey": self.journey.name}), 2,
			"voiding deleted the audit row instead of withdrawing its claim",
		)


class TestTheSendVerbRefusesThroughItsOwnFailedEdge(_CapCase):
	"""The sends gate is PATCHED on, never armed in the DB — no live switch is left behind, and nothing
	reaches a provider because the verb only ever returns a deferred thunk that nothing here calls."""

	def _send(self):
		with patch("tatva_connect.automation.sends.sends_enabled", return_value=True):
			return sends.send_whatsapp(
				self.lead.name, "crm_lead.mobile_no", "any-template",
				context={"crm_lead.mobile_no": _NUMBER},
			)

	def test_a_capped_send_returns_failed_and_does_not_raise(self):
		"""The journey KEEPS GOING. A raise would mark it Failed and take the author's branch away."""
		self._arm(most=1)
		self._already_contacted(1)

		output, detail = self._send()

		self.assertEqual(output, sends.FAILED)
		self.assertIn("limit", detail)

	def test_with_the_cap_off_the_send_is_refused_by_something_else_or_not_at_all(self):
		"""The control. This bench has no WhatsApp routing for the probe lead, so the call still ends in
		`failed` — the assertion is that it is not the CAP's failure. Asserting `sent` here would pass on
		a dormant bench for the wrong reason, which is what the first cut of this test did."""
		self._already_contacted(50)

		_output, detail = self._send()

		self.assertNotIn("limit", detail or "", "the cap refused a send while switched off")

	def test_a_dormant_bench_keeps_its_sent_edge_even_when_capped(self):
		"""A suppressed send contacts nobody, so the cap must not turn it into `failed` — that would send
		every journey on every dormant bench down its "could not reach the patient" branch, which is the
		shape change `sends`' own header refuses to let a switch make."""
		self._arm(most=1)
		self._already_contacted(1)

		output, marker = sends.send_whatsapp(
			self.lead.name, "crm_lead.mobile_no", "any-template",
			context={"crm_lead.mobile_no": _NUMBER},
		)

		self.assertEqual(output, sends.SENT)
		self.assertEqual(marker, sends.DORMANT_MARKER)


class TestTheOperatorCannotSetANumberThatMeansNothing(FrappeTestCase):
	"""1..20 is the band the product owner set. Refused, never clamped — a value quietly rewritten is an
	operator who believes they set something they did not."""

	def setUp(self):
		self.addCleanup(_reset_settings)

	def _save(self, most):
		settings = frappe.get_doc(SETTINGS_DT)
		settings.max_contacts = most
		settings.save(ignore_permissions=True)

	def test_zero_is_refused_and_the_message_names_the_range(self):
		with self.assertRaises(frappe.ValidationError) as caught:
			self._save(0)
		self.assertIn("1", str(caught.exception))
		self.assertIn("20", str(caught.exception))

	def test_twenty_one_is_refused(self):
		with self.assertRaises(frappe.ValidationError):
			self._save(21)

	def test_the_ends_of_the_range_are_accepted(self):
		for most in (1, 20):
			with self.subTest(most=most):
				self._save(most)
				self.assertEqual(frappe.get_doc(SETTINGS_DT).max_contacts, most)
