# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""How the catch-up poll finds the call a parked journey is waiting on.

THE POLL HAD NO TESTS AT ALL until 2026-08-05, and it is the only thing that rescues a journey whose
outcome webhook never arrived — so an untested lookup meant an untested rescue.

It used to read a SECOND step-log row that the send path wrote purely for it, carrying `execution_id=<id>
account=<name>` as a parsed string. That row was a duplicate of a step the journey had already logged, it
had to stay in step with a format two modules agreed on by hand, and its channel/contact were blank on a
table W12 added those columns to. It is gone. Both facts are now read from what already holds them:

  the execution id   IS the `CRM Call Log` row, found by the engine token stamped on it
  the account        is on the journey's FROZEN node config — the configuration the call really used

Run:
    bench --site dev.localhost run-tests --app tatva_connect \\
        --module tatva_connect.workflow_engine.tests.test_voice_reconcile_lookup
"""
import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.automation import origin
from tatva_connect.voice import reconcile
from tatva_connect.workflow_engine.tests import fixtures as fx

_WF = "ZZ Voice Reconcile Lookup"
_ACCOUNT = "ZZ Bolna Lookup Account"


class VoiceReconcileLookupCase(FrappeTestCase):
	"""One journey parked behind one voice node, and the call row that node produced."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.addClassCleanup(fx.purge, _WF)
		cls.lead = fx.make_lead()
		cls.workflow = fx.make_workflow(_WF, [
			fx.trigger(to="voice-1"),
			fx.node("voice-1", "AI Voice Call", config={"connection": _ACCOUNT, "agent_id": "zz-agent"}),
		], entry="start")

	def setUp(self):
		self.journey = fx.start_journey(self.workflow, self.lead.name, "voice-1").name
		self.correlation = f"{self.journey}::voice-1"

	def call_row(self, correlation=None):
		"""A call row exactly as `_write_voice_call_log` leaves one: named by the execution id, stamped.

		A unique id per case and an explicit delete, because `start_journey` COMMITS — so the suite's own
		rows outlive the rollback that would otherwise take them, and a fixed id would collide.
		"""
		execution_id = f"zz-execution-{frappe.generate_hash(length=10)}"
		frappe.get_doc({
			"doctype": "CRM Call Log", "id": execution_id, "type": "Outgoing", "status": "Initiated",
			"telephony_medium": "AI Voice", "to": "+919876543210", "from": "+918035303509", "duration": 0,
		}).insert(ignore_permissions=True)
		self.addCleanup(frappe.delete_doc, "CRM Call Log", execution_id, force=True, ignore_permissions=True)
		if correlation:
			frappe.db.set_value("CRM Call Log", execution_id,
			                    origin.AUTOMATION_STAMP["CRM Call Log"], correlation, update_modified=False)
		frappe.db.commit()
		return execution_id


class TestThePollFindsTheCallByTheJourneysOwnToken(VoiceReconcileLookupCase):

	def test_the_execution_id_and_the_account_are_both_found(self):
		placed = self.call_row(self.correlation)
		execution_id, account = reconcile._placement_of(self.journey, self.correlation)
		self.assertEqual(execution_id, placed, "the call row's name IS the provider's execution id")
		self.assertEqual(account, _ACCOUNT, "the account comes off the frozen node the call was placed on")

	def test_a_call_stamped_for_ANOTHER_journey_is_not_ours(self):
		"""The whole point of the token: one lead may have several calls, and a poll must take its own."""
		self.call_row("some-other-journey::voice-1")
		self.assertEqual(reconcile._placement_of(self.journey, self.correlation), (None, None))

	def test_an_unstamped_call_is_invisible_to_the_poll(self):
		"""A rep's own click-to-call lands in the same table and must never answer a journey's question."""
		self.call_row(correlation=None)
		self.assertEqual(reconcile._placement_of(self.journey, self.correlation), (None, None))

	def test_no_call_row_yet_means_nothing_to_poll_rather_than_an_error(self):
		self.assertEqual(reconcile._placement_of(self.journey, self.correlation), (None, None))


class TestTheAccountComesFromTheFrozenVersion(VoiceReconcileLookupCase):
	"""Authoritative because it is what the call was PLACED on — a later edit cannot move it."""

	def test_editing_the_live_workflow_does_not_change_what_an_in_flight_poll_reads(self):
		self.call_row(self.correlation)
		node = frappe.db.get_value("CRM Workflow Node",
		                           {"workflow": self.workflow.name, "node_id": "voice-1"}, "name")
		frappe.db.set_value("CRM Workflow Node", node, "config_json",
		                    frappe.as_json({"connection": "ZZ Some Other Account", "agent_id": "zz-agent"}))
		_execution_id, account = reconcile._placement_of(self.journey, self.correlation)
		self.assertEqual(account, _ACCOUNT, "the journey polls the account its own frozen version names")

	def test_a_token_naming_no_node_yields_no_account_and_so_cannot_be_polled(self):
		"""The pair stays TRUTHFUL — the call row was found, the account could not be. `poll_one` refuses
		on `execution_id and account`, so a half-answer stops the poll rather than inventing an outcome."""
		malformed = "just-a-journey-with-no-node-part"
		placed = self.call_row(malformed)
		execution_id, account = reconcile._placement_of(self.journey, malformed)
		self.assertEqual(execution_id, placed, "the stamp matched, so the call really was found")
		self.assertIsNone(account, "no node part means no frozen node to read the account off")
