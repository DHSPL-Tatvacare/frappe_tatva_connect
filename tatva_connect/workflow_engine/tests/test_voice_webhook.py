# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""AI VOICE (Bolna) — pass 2: the door the outcome comes back through, and the run it wakes.

THE TEST THAT MATTERS IS THE NEGATIVE ONE. A callback carrying a DIFFERENT run's token must leave this
run parked. Without correlation the wake is keyed on the lead, so any finished call could advance any
journey waiting on any call — silently, and looking exactly like correct behaviour. That is the same
defect the task bridge was built to remove, and voice closes it the same way.

Correlation here is the PROVIDER'S OWN ECHO: `place_call` puts the opaque `run::node` token into
`user_data`, Bolna hands it back on the terminal callback, and `handle` reads it out. No lookup row, so
there is no window in which the callback beats the row that would have matched it.

Nothing in this file dials. The send switch stays OFF, so the voice node parks the run without touching
the adapter, and no `requests` call is made anywhere in the suite.
"""
import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.voice.adapters import bolna
from tatva_connect.webhooks import ingress, registry
from tatva_connect.workflow_engine import interpreter, signals
from tatva_connect.workflow_engine.tests import fixtures as fx

_WORKFLOW = "voice-webhook-probe"
_ACCOUNT = "voice-webhook-account"
_TOKEN = "voice-webhook-secret-token"


def _account(name=_ACCOUNT, enabled=1, token=_TOKEN):
	if frappe.db.exists("CRM AI Voice Account", name):
		frappe.delete_doc("CRM AI Voice Account", name, force=True, ignore_permissions=True)
	return frappe.get_doc({
		"doctype": "CRM AI Voice Account", "account_name": name, "api_key": "sk-test-never-real",  # pragma: allowlist secret
		"enabled": enabled, "webhook_token": token,
	}).insert(ignore_permissions=True)


def _callback(correlation, status="completed", reason=""):
	"""A Bolna terminal callback, shaped as Bolna really sends one: the execution keyed under `id`, the
	echo under `user_data`, and some of the capture nested in `telephony_data`."""
	return {
		"id": "exec-abc123",
		"status": status,
		"status_reason": reason,
		"user_data": {bolna.USER_DATA_CORRELATION_KEY: correlation},
		"transcript": "Hello, this is a reminder about your appointment.",
		"telephony_data": {"duration": 42, "recording_url": "https://example.invalid/rec/1"},
	}


class TestTheDoorIsShutWithoutAToken(FrappeTestCase):
	"""The ingress is the FIRST thing `spine.receive` does, before the kill-switch and before any line of
	the adapter runs. A channel with no `token_field` cannot authenticate anything, which is what the
	voice entry was until this chunk: the endpoint would have 403'd every real callback."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.account = _account()
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		frappe.delete_doc("CRM AI Voice Account", _ACCOUNT, force=True, ignore_permissions=True)
		frappe.db.commit()
		super().tearDownClass()

	def test_the_channel_declares_a_whole_ingress(self):
		cfg = registry.by_channel("voice")
		self.assertEqual(cfg["token_field"], "webhook_token")
		self.assertEqual(cfg["ingress_prefix"], "")
		self.assertTrue(callable(cfg["targets"]), "a channel with no targets can publish no URL to register")

	def test_the_url_carries_the_token_and_no_vendor(self):
		cfg = registry.by_channel("voice")
		url = cfg["targets"]("https://crm.example", self.account, _TOKEN)[0]["url"]
		self.assertEqual(url, f"https://crm.example/webhooks/voice/{_TOKEN}")
		self.assertNotIn("bolna", url, "the vendor is named by the account row, never by the URL")

	def test_the_digest_is_derived_on_save(self):
		"""A Password column cannot be indexed, so authentication is one read against this digest."""
		self.assertEqual(
			frappe.db.get_value("CRM AI Voice Account", _ACCOUNT, "webhook_token_hash"),
			ingress.token_digest(_TOKEN),
		)

	def test_the_right_token_resolves_the_account_and_a_wrong_one_resolves_nothing(self):
		cfg = registry.by_channel("voice")
		self.assertEqual(ingress._account_for_token(cfg, _TOKEN), _ACCOUNT)
		self.assertIsNone(ingress._account_for_token(cfg, "not-the-token"))
		self.assertIsNone(ingress._account_for_token(cfg, ""))

	def test_a_disabled_account_authenticates_nothing(self):
		"""Turning an integration off must STOP its traffic, not merely hide it from a list."""
		frappe.db.set_value("CRM AI Voice Account", _ACCOUNT, "enabled", 0)
		frappe.db.commit()
		try:
			self.assertIsNone(ingress._account_for_token(registry.by_channel("voice"), _TOKEN))
		finally:
			frappe.db.set_value("CRM AI Voice Account", _ACCOUNT, "enabled", 1)
			frappe.db.commit()


class TestScreeningDecidesBeforeAnythingIsWritten(FrappeTestCase):
	"""The spine asks the adapter ONE question and records the answer on the row, so a declined delivery
	is actionable rather than lost. Both declines here are ordinary traffic, not faults."""

	def test_a_call_still_in_flight_is_declined_with_its_status(self):
		wanted, reason = bolna.screen(_callback("run::n", status="ringing"), None, _ACCOUNT)
		self.assertFalse(wanted)
		self.assertIn("ringing", reason)

	def test_a_call_no_workflow_placed_is_declined(self):
		payload = _callback("run::n")
		payload["user_data"] = {}
		wanted, reason = bolna.screen(payload, None, _ACCOUNT)
		self.assertFalse(wanted)
		self.assertIn("correlation", reason)

	def test_a_terminal_correlated_call_is_wanted(self):
		self.assertEqual(bolna.screen(_callback("run::n"), None, _ACCOUNT), (True, None))

	def test_the_batch_echo_is_read_too(self):
		"""The cohort path (W7.2) lands the same key under `context_details.recipient_data`."""
		payload = _callback("run::n")
		payload["user_data"] = {}
		payload["context_details"] = {"recipient_data": {bolna.USER_DATA_CORRELATION_KEY: "run::n"}}
		self.assertEqual(bolna.engine_token(payload), "run::n")

	def test_the_outcome_map_is_the_pass_one_classifier(self):
		self.assertEqual(bolna.normalize_webhook(_callback("x"))[0], "answered")
		self.assertEqual(bolna.normalize_webhook(_callback("x", status="no-answer"))[0], "no_answer")
		self.assertEqual(bolna.normalize_webhook(_callback("x", status="failed"))[0], "failed")

	def test_a_failed_call_wakes_only_the_coarse_done_event(self):
		"""`voice.failed` is a SYNCHRONOUS output of the node, so no Wait may be authored on it — and a
		call that failed after being placed must still free a run parked on `voice.completed`."""
		self.assertEqual(bolna.waitable_signals("answered"), ["voice.answered", "voice.completed"])
		self.assertEqual(bolna.waitable_signals("no_answer"), ["voice.completed", "voice.no_answer"])
		self.assertEqual(bolna.waitable_signals("failed"), ["voice.completed"])


class TestTheEchoWakesTheRunThatPlacedTheCall(FrappeTestCase):
	"""End to end on the real engine: a run walks to the voice node, parks on the Wait that follows it,
	and the provider's callback advances THAT run — and only that one.

	The send switch is OFF throughout, so the node parks the run without touching the adapter. Which is
	the point: the wake is proved on a graph where no call was ever placed."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		fx.purge(_WORKFLOW)
		fx.arm_engine(True, cls)
		cls.account = _account()
		cls.lead = fx.make_lead()
		# A number in the declared +E.164 shape, so the node takes `placed` and the run reaches the Wait.
		# It is never dialled — the send switch is off, so the node parks without touching the adapter.
		cls.lead.db_set("mobile_no", "+919999999999")
		cls.workflow = fx.make_workflow(_WORKFLOW, [
			fx.trigger(to="call"),
			fx.node("call", "AI Voice Call", config={
				"contact_number": "crm_lead.mobile_no", "connection": _ACCOUNT, "agent_id": "agent-1",
			}, edges={"placed": "w1", "failed": "end"}),
			fx.node("w1", "Wait", config={
				"mode": "Until Event", "source_node": "call", "event_name": "voice.answered",
				"accepts": '{"outcome": "voice_outcome"}',
			}, edges={"event": "end"}),
			fx.node("end", "Terminal"),
		])
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		fx.purge(_WORKFLOW)
		frappe.db.delete(fx.EVENT_DT, {"subject_name": cls.lead.name})
		frappe.delete_doc("CRM Lead", cls.lead.name, force=True, ignore_permissions=True)
		frappe.delete_doc("CRM AI Voice Account", _ACCOUNT, force=True, ignore_permissions=True)
		frappe.db.commit()
		super().tearDownClass()

	def setUp(self):
		self._reset()

	def tearDown(self):
		self._reset()

	def _reset(self):
		for run in frappe.get_all(fx.RUN_DT, filters={"workflow": _WORKFLOW}, pluck="name"):
			frappe.db.delete(fx.STEP_LOG_DT, {"workflow_run": run})
		frappe.db.delete(fx.RUN_DT, {"workflow": _WORKFLOW})
		frappe.db.delete(fx.EVENT_DT, {"subject_name": self.lead.name})
		frappe.db.commit()

	def _park(self):
		"""Walk a run to the Wait after the voice node. Returns (run_name, its correlation token)."""
		run = fx.start_run(self.workflow, self.lead.name, "start")
		interpreter.advance(frappe.get_doc(fx.RUN_DT, run.name))
		frappe.db.commit()
		return run.name, f"{run.name}::call"

	def _row(self, run_name):
		return frappe.db.get_value(
			fx.RUN_DT, run_name, ["status", "current_node", "awaiting_signal", "awaiting_correlation"],
			as_dict=True,
		)

	def test_the_run_parks_on_the_token_the_voice_node_minted(self):
		run_name, token = self._park()
		row = self._row(run_name)
		self.assertEqual(row.status, "Parked")
		self.assertEqual(row.awaiting_signal, "voice.answered")
		self.assertEqual(row.awaiting_correlation, token)

	def test_the_callback_delivers_the_correlated_outcome(self):
		_run_name, token = self._park()
		bolna.handle(_callback(token), None, _ACCOUNT)
		frappe.db.commit()

		events = frappe.get_all(
			fx.EVENT_DT, filters={"subject_name": self.lead.name, "correlation": token},
			fields=["event_name", "correlation"], order_by="event_name asc",
		)
		self.assertEqual(
			[e.event_name for e in events], ["voice.answered", "voice.completed"],
			"an answered call reports its own outcome AND the coarse done event",
		)

	def test_the_delivered_outcome_resumes_the_run(self):
		run_name, token = self._park()
		bolna.handle(_callback(token), None, _ACCOUNT)
		signals.resume_for_signal("CRM Lead", self.lead.name, "voice.answered", correlation=token)
		frappe.db.commit()

		row = self._row(run_name)
		self.assertNotEqual(row.status, "Parked", "the callback must free the run it correlates to")
		self.assertEqual(row.current_node, "end")

	def test_what_the_call_captured_reaches_run_state(self):
		"""The Wait's `accepts` map merges the signal payload into state, which is what makes an outcome
		routable downstream rather than merely an edge."""
		run_name, token = self._park()
		bolna.handle(_callback(token), None, _ACCOUNT)
		signals.resume_for_signal("CRM Lead", self.lead.name, "voice.answered", correlation=token)
		frappe.db.commit()

		# Namespaced under the WAIT that accepted it — a Wait writes as itself, so downstream reads
		# `w1.voice_outcome` and two Waits on one graph can never collide on a bare name.
		state = frappe.parse_json(frappe.db.get_value(fx.RUN_DT, run_name, "state_json")) or {}
		self.assertEqual(state.get("w1", {}).get("voice_outcome"), "answered", state)

	def test_another_runs_callback_leaves_this_run_parked(self):
		"""THE test. Same lead, same account, same terminal status — a different token, so nothing moves.

		Correlating on the lead would advance whichever run answered first, which is exactly the defect
		this exists to prevent when one lead is in two journeys.
		"""
		run_name, _token = self._park()
		other_run, other_token = self._park()
		self.assertNotEqual(run_name, other_run)

		bolna.handle(_callback(other_token), None, _ACCOUNT)
		signals.resume_for_signal("CRM Lead", self.lead.name, "voice.answered", correlation=other_token)
		frappe.db.commit()

		self.assertEqual(self._row(run_name).status, "Parked", "the OTHER call must not move this run")
		self.assertEqual(self._row(other_run).current_node, "end")

	def test_a_callback_for_no_known_run_wakes_nothing(self):
		run_name, _token = self._park()
		bolna.handle(_callback("NOT-A-RUN::call"), None, _ACCOUNT)
		frappe.db.commit()

		self.assertEqual(self._row(run_name).status, "Parked")
		self.assertFalse(
			frappe.get_all(fx.EVENT_DT, filters={"correlation": "NOT-A-RUN::call"}),
			"a callback whose run cannot be found must deliver no signal",
		)

	def test_a_redelivered_callback_is_recognised_and_not_replayed(self):
		"""Providers re-send. The spine collapses byte-identical copies; this catches a copy that differs
		in a field the wake does not read, and must not wake the run a second time."""
		_run_name, token = self._park()
		self.assertFalse(bolna.already_processed(_callback(token), None, _ACCOUNT))
		bolna.handle(_callback(token), None, _ACCOUNT)
		frappe.db.commit()
		self.assertTrue(bolna.already_processed(_callback(token), None, _ACCOUNT))
