# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Which provider calls become Call Logs, which lead each lands on and which rep it credits — real Acefone shapes only.

Shapes: the 179-webhook corpus (2026-07), the live click-to-call hangup body (2026-09-01), the dashboard's "Call answered
by Agent" body, and three anonymised call records of 2026-10-15. Only the resulting Call Log row is asserted.

Run:
    bench --site dev.localhost run-tests --app tatva_connect --module tatva_connect.tests.telephony.test_call_attribution
"""
import collections
import random
from unittest.mock import patch

import frappe
from frappe.desk.form import assign_to
from frappe.tests import IntegrationTestCase
from frappe.utils import add_to_date, getdate, now_datetime

from tatva_connect.telephony import api, reconcile, writer
from tatva_connect.telephony.adapters import acefone
from tatva_connect.tests.telephony.fixtures import config

DID = "9000007179"  # the corpus's Dialer number
CORPUS_SEAT = "0602141810347"  # the seat on the corpus's answered calls
OWNER = "_test_tel_owner@example.com"
ASSIGNEE = "_test_tel_assignee@example.com"
STRANGER = "_test_tel_stranger@example.com"
NOBODY = "_test_tel_nobody@example.invalid"  # an agent email that is no CRM user


def _digits():
	return f"9{random.randint(100_000_000, 999_999_999)}"


def _answered_hangup(**over):
	"""The corpus's first answered Dialer hangup as the 2026 tenant sends it (the seat, no email), with the given keys replaced."""
	payload = dict(next(p for p in config.corpus() if str(p.get("call_connected")) == "1"))
	payload["answered_agent"] = [{"name": "Redacted", "number": payload["answered_agent_number"]}]
	payload.update(over)
	return payload


def _click_to_call(digits, seat, **over):
	"""The live click-to-call hangup body (2026-09-01): rep named by seat only, customer prefixed with 91."""
	return {"call_id": frappe.generate_hash(length=16), "direction": "clicktocall", "call_status": "answered",
	        "caller_id_number": DID, "call_to_number": f"91{digits}", "call_connected": "1",
	        "answered_agent": {"id": f"05{seat[2:]}", "name": "Rep-Extension", "number": seat},
	        "answered_agent_number": seat, **over}


def _answered_live(call_id, digits, agent_phone):
	"""The dashboard's "Call answered by Agent" body: the agent is named only by phone; the trailing space is Acefone's."""
	return {"answer_agent_number": agent_phone, "billing_circle": {"circle": "Karnataka", "operator": "Airtel"},
	        "call_id": call_id, "call_status": "Answered", "call_to_number": f"91{DID}", "caller_id_number": digits,
	        "customer_no_with_prefix ": f"+91{digits}", "direction": "inbound",
	        "start_stamp": now_datetime().strftime("%Y-%m-%d %H:%M:%S"), "uuid": frappe.generate_hash(length=13)}


def _record(case, digits):
	"""An anonymised 2026-10-15 record, placed on our number and the given customer."""
	return {**config.record(case), "did_number": f"+91{DID}", "client_number": f"+91{digits}"}


def _push(payload, event):
	return acefone.process(payload, event=event, account=config.ACCOUNT)


def _pull(row):
	reconcile._reconcile_one(row, config.ACCOUNT, False, collections.defaultdict(int))
	return writer.row_for_key(row["call_id"])


def _row(name):
	return frappe.get_doc("CRM Call Log", name) if name else None


def _logged():
	return frappe.get_all("CRM Call Log", filters={"custom_telephony_account": config.ACCOUNT}, fields=["name", "status", "to"])


class TestCallAttribution(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		config.ensure_account(rep_emails=(OWNER, ASSIGNEE, STRANGER))
		config.ensure_account(account=config.OTHER_ACCOUNT)
		config.ensure_account(account=config.SIBLING_ACCOUNT, token=f"{config.ACCOUNT}-token")
		cls.addClassCleanup(config.set_rules, config.current_rules())

	def setUp(self):
		config.clear_dids()
		config.clear_seats()
		config.clear_seats(config.SIBLING_ACCOUNT)
		config.map_did(DID)
		config.credit_related_only(0)

	def tearDown(self):
		config.credit_related_only(0)
		config.clear_dids()
		config.clear_seats()
		config.clear_seats(config.SIBLING_ACCOUNT)

	def _lead(self, owner=None, vertical=None):
		digits = _digits()
		lead = config.lead(digits, owner=owner, vertical=vertical)
		self.addCleanup(frappe.delete_doc, "CRM Lead", lead.name, force=True, ignore_permissions=True)
		return digits, lead

	def _corpus_on_did(self):
		return [p for p in config.corpus() if DID in str(p.get("call_to_number"))]

	# -- Which calls are ours -------------------------------------------------------------------------------------

	def test_no_capture_rule_logs_nothing(self):
		config.set_rules([])
		for payload in config.corpus():
			_push(payload, "inbound_complete")
		self.assertEqual(_logged(), [])

	def test_only_calls_on_our_mapped_number_are_kept(self):
		config.set_rules([config.rule("Inbound", "Any")])
		for payload in config.corpus():
			_push(payload, "inbound_complete")
		rows = _logged()
		self.assertEqual(len(rows), len({p["call_id"] for p in self._corpus_on_did()}))
		self.assertEqual({r.to for r in rows}, {DID})

	def test_a_disabled_number_logs_nothing(self):
		config.set_rules([config.rule("Inbound", "Any")])
		config.clear_dids()
		config.map_did(DID, enabled=0)
		for payload in self._corpus_on_did():
			_push(payload, "inbound_complete")
		self.assertEqual(_logged(), [])

	def test_a_number_belonging_to_another_tenant_is_refused(self):
		config.set_rules([config.rule("Inbound", "Any")])
		self.assertIsNone(acefone.process(_answered_hangup(), event="inbound_complete", account=config.OTHER_ACCOUNT))

	def test_one_tenant_split_across_accounts_keeps_the_call_and_credits_by_the_numbers_own_account(self):
		"""The delivery carries a sibling's token for a number mapped under ACCOUNT: kept, and credited only by a seat on ACCOUNT."""
		config.set_rules([config.rule("Inbound", "Any")])
		config.set_seat(OWNER, CORPUS_SEAT, account=config.SIBLING_ACCOUNT)
		row = _row(acefone.process(_answered_hangup(), event="inbound_complete", account=config.SIBLING_ACCOUNT))
		self.assertEqual((row.status, row.receiver), ("Completed", None))  # a seat on the sibling is never used
		config.set_seat(OWNER, CORPUS_SEAT)
		call = _answered_hangup(call_id=frappe.generate_hash(length=16))
		row = _row(acefone.process(call, event="inbound_complete", account=config.SIBLING_ACCOUNT))
		self.assertEqual((row.status, row.receiver, row.custom_telephony_account), ("Completed", OWNER, config.ACCOUNT))

	def test_every_logged_call_carries_a_status_the_call_log_declares(self):
		config.set_rules([config.rule("Inbound", "Any")])
		for payload in self._corpus_on_did():
			_push(payload, "inbound_complete")
		declared = set(frappe.get_meta("CRM Call Log").get_field("status").options.split("\n"))
		rows = _logged()
		self.assertLessEqual({r.status for r in rows}, declared)
		answered = {p["call_id"] for p in self._corpus_on_did() if str(p.get("call_connected")) == "1"}
		self.assertEqual(sum(r.status == "Completed" for r in rows), len(answered))

	# -- Which lead -----------------------------------------------------------------------------------------------

	def test_a_call_lands_on_the_lead_with_that_phone_in_the_numbers_grain(self):
		config.set_rules([config.rule("Inbound", "Any")])
		digits, lead = self._lead()
		row = _row(_push(_answered_hangup(caller_id_number=f"+91{digits}", customer_number_with_prefix=f"+91{digits}"), "inbound_complete"))
		self.assertEqual((row.type, row.reference_docname), ("Incoming", lead.name))

	def test_the_same_phone_in_another_grain_is_not_linked(self):
		config.set_rules([config.rule("Inbound", "Any")])
		digits, _lead = self._lead(vertical="_TestTelOtherVertical")
		row = _row(_push(_answered_hangup(caller_id_number=f"+91{digits}", customer_number_with_prefix=f"+91{digits}"), "inbound_complete"))
		self.assertTrue(row)
		self.assertFalse(row.reference_docname)

	def test_a_click_to_call_is_logged_outgoing_from_our_number_to_the_lead(self):
		config.set_rules([config.rule("Outbound", "Dialer")])
		config.set_seat(OWNER, "0600000000901")
		digits, lead = self._lead()
		row = _row(_push(_click_to_call(digits, "0600000000901"), "outbound_complete"))
		self.assertEqual((row.type, row.get("from"), row.to, row.reference_docname, row.caller),
		                 ("Outgoing", DID, digits, lead.name, OWNER))

	# -- Which rep ------------------------------------------------------------------------------------------------

	def test_a_missed_call_is_logged_with_no_rep(self):
		config.set_rules([config.rule("Inbound", "Any")])
		missed = next(p for p in self._corpus_on_did() if str(p.get("call_connected")) != "1")
		row = _row(_push(missed, "inbound_complete"))
		self.assertEqual((row.status, row.receiver), ("No Answer", None))

	def test_an_agent_the_crm_does_not_know_is_logged_with_no_rep(self):
		config.set_rules([config.rule("Inbound", "Any")])
		row = _row(_push(_answered_hangup(answered_agent=[{"email": NOBODY, "name": "Redacted", "number": "Extension-0"}],
		                                  answered_agent_number=""), "inbound_complete"))
		self.assertEqual((row.status, row.receiver), ("Completed", None))

	def test_a_handed_on_seat_credits_whoever_held_it_when_the_call_started(self):
		config.set_rules([config.rule("Inbound", "Any")])
		config.set_seat(OWNER, CORPUS_SEAT)
		config.hand_over(CORPUS_SEAT, OWNER, ASSIGNEE, now_datetime())
		before = _push(_answered_hangup(), "inbound_complete")  # started 2026-07-11, before the handover
		_push(_answered_hangup(), "inbound_complete")
		after = _push(_answered_hangup(call_id=frappe.generate_hash(length=16),
		                               start_stamp=add_to_date(now_datetime(), minutes=1).strftime("%m/%d/%Y, %I:%M:%S %p")),
		              "inbound_complete")
		self.assertEqual((_row(before).receiver, _row(after).receiver), (OWNER, ASSIGNEE))

	def test_the_follow_me_phone_names_the_rep_on_a_missed_click_to_call_with_no_seat(self):
		config.set_rules([config.rule("Outbound", "Dialer")])
		digits, lead = self._lead()
		record = _record("missed_click_to_call_no_seat", digits)
		self.assertIsNone(_row(_pull(record)).caller)
		config.set_seat(OWNER, "0600000000901", follow_me_number=record["agent_number"])
		row = _row(_pull(record))  # the next pull fills the blank rep
		self.assertEqual((row.type, row.reference_docname, row.caller), ("Outgoing", lead.name, OWNER))

	def test_a_transferred_call_ends_with_the_rep_who_handled_it(self):
		"""Live: the answered body credits the first pickup by phone; the hangup names the last seat; a re-sent hangup changes nothing."""
		config.set_rules([config.rule("Inbound", "Any")])
		config.set_seat(ASSIGNEE, "0600000000903", follow_me_number="+919000200010")
		config.set_seat(OWNER, "0600000000904", follow_me_number="+919000200007")
		digits, _lead = self._lead()
		call_id = frappe.generate_hash(length=16)
		name = _push(_answered_live(call_id, digits, "+919000200010"), "inbound_answered")
		self.assertEqual((_row(name).status, _row(name).receiver), ("In Progress", ASSIGNEE))
		hangup = _answered_hangup(call_id=call_id, caller_id_number=f"+91{digits}", customer_number_with_prefix=f"+91{digits}",
		                          answered_agent=[{"name": "Redacted", "number": "Extension-0600000000904"}],
		                          answered_agent_number="Extension-0600000000904")
		_push(hangup, "inbound_complete")
		_push(hangup, "inbound_complete")
		self.assertEqual(frappe.db.count("CRM Call Log", {writer.CALL_KEY_FIELD: call_id}), 1)
		self.assertEqual((_row(name).status, _row(name).receiver), ("Completed", OWNER))

	def test_the_pull_credits_the_last_rep_of_a_transferred_record(self):
		config.set_rules([config.rule("Inbound", "Any")])
		config.set_seat(ASSIGNEE, "0600000000903")
		config.set_seat(OWNER, "0600000000904")
		digits, lead = self._lead()
		row = _row(_pull(_record("inbound_transferred", digits)))
		self.assertEqual((row.status, row.reference_docname, row.receiver), ("Completed", lead.name, OWNER))

	def test_the_pull_and_the_push_of_one_call_write_one_row(self):
		config.set_rules([config.rule("Outbound", "Dialer")])
		config.set_seat(OWNER, "0600000000901")
		digits, lead = self._lead()
		record = _record("answered_click_to_call_with_seat", digits)
		pulled = _pull(record)
		pushed = _push(_click_to_call(digits, "0600000000901", call_id=record["call_id"]), "outbound_complete")
		self.assertEqual(pulled, pushed)
		row = _row(pushed)
		self.assertEqual((row.status, row.reference_docname, row.caller), ("Completed", lead.name, OWNER))

	# -- The disposition the agent saves ------------------------------------------------------------------------

	def test_a_disposition_fills_its_call_and_a_resent_hangup_keeps_it(self):
		config.set_rules([config.rule("Outbound", "Dialer")])
		config.set_seat(OWNER, "0600000000901")
		digits, _lead = self._lead()
		hangup = _click_to_call(digits, "0600000000901", outbound_sec="31", reason_key="Call Disconnected By Caller")
		name = _push(hangup, "outbound_complete")
		_push({"call_id": hangup["call_id"], "disposition": "Call Back", "sub_disposition": "Evening"}, acefone.DISPOSITION_EVENT)
		_push(hangup, "outbound_complete")
		row = _row(name)
		self.assertEqual(
			(row.status, row.duration, row.caller, row.custom_hangup_reason, row.custom_disposition, row.custom_sub_disposition),
			("Completed", 31, OWNER, "Call Disconnected By Caller", "Call Back", "Evening"))

	def test_fetch_dispositions_fills_only_a_logged_call_and_creates_none(self):
		config.set_rules([config.rule("Outbound", "Dialer")])
		digits, _lead = self._lead()
		dialled = _record("dialer_with_disposition", digits)
		logged = _pull({**dialled, "dialer_call_details": {}})
		stranger = {**dialled, "call_id": frappe.generate_hash(length=16)}
		with patch.object(api, "make_get_request", return_value={"results": [dialled, stranger]}):
			counts = reconcile._fill_dispositions(config.ACCOUNT, getdate(dialled["date"]))
		row = _row(logged)
		self.assertEqual(
			(counts, row.custom_disposition, row.custom_sub_disposition, writer.row_for_key(stranger["call_id"])),
			((1, 1, []), "Not Interested", "Already Consulting an Expert", None))

	# -- Credit only the lead's own reps (CRM Telephony Settings) ------------------------------------------------

	def test_the_switch_keeps_another_reps_outgoing_call_on_the_lead_with_no_rep(self):
		config.set_rules([config.rule("Outbound", "Dialer")])
		seats = {OWNER: "0600000000901", ASSIGNEE: "0600000000902", STRANGER: "0600000000903"}
		for rep, seat in seats.items():
			config.set_seat(rep, seat)
		digits, lead = self._lead(owner=OWNER)
		assign_to.add({"doctype": "CRM Lead", "name": lead.name, "assign_to": [ASSIGNEE]})

		def credited(rep):
			row = _row(_push(_click_to_call(digits, seats[rep]), "outbound_complete"))
			self.assertEqual(row.reference_docname, lead.name)
			return row.caller

		self.assertEqual(credited(STRANGER), STRANGER)  # off: as it always was
		config.credit_related_only(1)
		self.assertEqual([credited(r) for r in (STRANGER, OWNER, ASSIGNEE)], [None, OWNER, ASSIGNEE])
