# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""THE STEP LOG RECORDS WHAT HAPPENED, NOT THAT THE HANDLER RETURNED.

Seen live on 2026-07-21, one row of `CRM Workflow Step Log`:

    Node Type | Send WhatsApp
    Outcome   | ok
    Detail    | failed: no WhatsApp routing for lead ubc8ge0d8q's grain

`ok` beside `failed:`, in the same row. `interpreter.advance` hardcoded the outcome for every effect
node, so `ok` meant only "the handler returned without raising". The verb's REAL result was computed one
line earlier by `_verb_output` — used to pick the outgoing edge, then thrown away. The field's own
description promised the reader "what happened", and the reader was entitled to believe it.

WHY `ok` COULD BE OVERWRITTEN RATHER THAN JOINED BY A SECOND COLUMN
------------------------------------------------------------------
For an EFFECT node `ok` carries no information at all: an effect node that did not raise always ran, and
only a Wait ever parks. So the word was pure noise where a declared answer already existed. Control nodes
keep their own words — `parked`, `resumed`, `done`, and the terminal `failed` that `_fail` writes — because
for those the control-flow fact IS what happened.

The vocabulary therefore stays closed BY CONSTRUCTION: five control words plus whatever the verbs
declare in `actions.VERBS[*]["outputs"]`. `TestTheVocabularyCannotDriftFromTheFrontend` is what keeps it
closed — it reads the declarations at runtime and fails when a word the engine can now write is unknown
to either frontend map. There are THREE frontend consumers, not one: the history dots, and the live
canvas ring which reads the same value off the realtime `workflow_step` event.

WHAT THIS SUITE REFUSES TO MOCK
-------------------------------
Every runtime test here drives the real `interpreter.advance` and reads its rows back out of the database
through `fx.logs()`. Nothing patches `_step_log`, `_verb_output`, `_edge` or `history._failure` — the
functions under test. The Call API suite next door learned this the hard way: a module that mocked the
exact function that was broken stayed green over a dead code path for months.

Nothing is sent. The headline success case is a DORMANT send, which patches nothing whatsoever; the
headline failure case arms the sends switch IN-PROCESS only and fails before any account is resolved.
"""
import hashlib
import json
import pathlib
import re
import unittest
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.automation import actions, sends
from tatva_connect.tests.authz.grains import assert_masters_exist
from tatva_connect.workflow_engine import history, interpreter
from tatva_connect.workflow_engine.tests import fixtures as fx

_WORKFLOW = "step-log-truth-probe"

_FRONTEND = pathlib.Path(
	"/home/frappe/frappe-bench/apps/crm/frontend/src/tatva/workflows"
)


class _WalkHarness(FrappeTestCase):
	"""One real lead, real graphs, the real interpreter. Step log rows are read back from the database."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		assert_masters_exist()
		# By PREFIX, not by the one name: this suite builds a graph per case, and a class that dies in setUp leaves them behind.
		fx.purge(*frappe.get_all(fx.WORKFLOW_DT, filters={"name": ("like", f"{_WORKFLOW}%")}, pluck="name"))
		fx.arm_engine(True, cls)
		# A distinct number per class: the lead dedup index is (mobile_no, vertical, group), so a shared one collides between classes.
		cls.number = f"+9198000{int(hashlib.md5(cls.__name__.encode()).hexdigest(), 16) % 10**5:05d}"
		for stale in frappe.get_all("CRM Lead", filters={"mobile_no": cls.number}, pluck="name"):
			frappe.delete_doc("CRM Lead", stale, force=True, ignore_permissions=True)
		cls.lead = fx.make_lead()
		cls.lead.mobile_no = cls.number
		cls.lead.save(ignore_permissions=True)  # authz-ok: tier-c — test fixture, through the document API (B11)
		cls._made = []
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		fx.purge(*cls._made)
		frappe.delete_doc("CRM Lead", cls.lead.name, force=True, ignore_permissions=True)
		frappe.db.commit()
		super().tearDownClass()

	def _walk(self, workflow, at="start"):
		run = fx.start_journey(workflow, self.lead.name, at)
		interpreter.advance(frappe.get_doc(fx.JOURNEY_DT, run.name))
		return frappe.get_doc(fx.JOURNEY_DT, run.name)

	def _steps(self, run):
		"""The audit rows as the reader really gets them — straight out of the table."""
		return {row["node_id"]: row for row in fx.logs(run.name)}

	def _make(self, name, nodes):
		"""Every graph this suite builds is remembered, so tearDownClass takes its links down before the
		fixtures they point at."""
		self._made.append(name)
		wf = fx.make_workflow(name, nodes)
		frappe.db.commit()
		return wf

	def _send_graph(self, name):
		return self._make(name, [
			fx.trigger(to="wa"),
			fx.node("wa", "Send WhatsApp", config={"contact_number": "crm_lead.mobile_no", "whatsapp_template": "probe"},
			        edges={sends.SENT: "sent_end", sends.FAILED: "failed_end"}),
			fx.node("sent_end", "Terminal"),
			fx.node("failed_end", "Terminal"),
		])


class TestASendRecordsWhetherItReachedThePatient(_WalkHarness):
	"""THE HEADLINE, both directions, both through the unmocked interpreter."""

	def test_a_failed_send_records_failed_not_ok(self):
		"""THE red, and it is the live row from 2026-07-21 reproduced exactly: sends armed, no routing
		rule for this lead's grain. `_step_log` is NOT patched — the row asserted on is the row a support
		engineer would open."""
		workflow = self._send_graph(f"{_WORKFLOW}-fail")

		with patch.object(sends, "sends_enabled", return_value=True):
			run = self._walk(workflow)

		step = self._steps(run)["wa"]
		self.assertEqual(step["outcome"], "failed", "the audit said `ok` over a send that never happened")
		self.assertIn("no WhatsApp routing", step["detail"], "the reason must survive beside the outcome")

	def test_a_send_that_did_happen_records_sent(self):
		"""The other direction, and the one that proves this is not a blanket rewrite to `failed`.

		A DORMANT send is the purest available success path: it patches NOTHING, reaches no provider, and
		still leaves by the `sent` edge. The switch ships OFF, so this is the bench's real behaviour.
		"""
		workflow = self._send_graph(f"{_WORKFLOW}-dormant")

		run = self._walk(workflow)

		step = self._steps(run)["wa"]
		self.assertEqual(step["outcome"], "sent")
		self.assertNotEqual(step["outcome"], "ok", "an effect node's outcome must be its declared output")
		self.assertIn(sends.DORMANT_MARKER, step["detail"])

	def test_the_control_flow_is_unchanged_by_the_audit_fix(self):
		"""The guard that matters most: this chunk changes what is WRITTEN DOWN, never where the journey goes.
		Both runs must still leave the send by the edge their output names."""
		failing = self._send_graph(f"{_WORKFLOW}-fail-route")
		dormant = self._send_graph(f"{_WORKFLOW}-sent-route")

		with patch.object(sends, "sends_enabled", return_value=True):
			failed_run = self._walk(failing)
		sent_run = self._walk(dormant)

		self.assertEqual(failed_run.current_node, "failed_end")
		self.assertEqual(failed_run.status, "Done", "a routed failure is not a dead run")
		self.assertEqual(sent_run.current_node, "sent_end")


class TestItIsTheDeclarationNotASendWhatsAppSpecialCase(_WalkHarness):
	"""A second verb, with a different output vocabulary, proves the rule is `_verb_output` and not a
	string pasted into the send path."""

	_ENDPOINT = "Step Log Truth Probe Endpoint"

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		if not frappe.db.exists("Webhook", cls._ENDPOINT):
			frappe.get_doc({
				"doctype": "Webhook", "name": cls._ENDPOINT, "webhook_doctype": "CRM Lead",
				"request_url": "https://example.invalid/probe", "request_method": "POST",
				# Disabled on purpose: the Call API node reads this row for its URL and never checks the flag, while an ENABLED row would fire Frappe's own webhook on every CRM Lead save in the suite.
				"webhook_docevent": "on_update", "enabled": 0,
			}).insert(ignore_permissions=True)  # authz-ok: tier-c — test fixture, no user input
			frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		# The graphs LINK to this endpoint, so they come down first — super() purges them.
		super().tearDownClass()
		frappe.delete_doc("Webhook", cls._ENDPOINT, force=True, ignore_permissions=True)
		frappe.db.commit()

	def _call_graph(self, name):
		return self._make(name, [
			fx.trigger(to="call"),
			fx.node("call", "Call API", config={
				"webhook_endpoint": self._ENDPOINT,
				"success_when": {"type": "rule", "field": "call.status", "operator": "is", "value": 200},
			}, edges={"succeeded": "won", "failed": "lost"}),
			fx.node("won", "Terminal"),
			fx.node("lost", "Terminal"),
		])

	def _drive(self, name, status):
		"""Only the HTTP call is stubbed — the seam BELOW the interpreter. Capture, routing and the step
		log all run for real."""
		workflow = self._call_graph(name)
		response = {"status": status, "ok": status == 200, "body": {}, "error": None}
		with patch("tatva_connect.automation.actions._call_endpoint", return_value=response):
			run = self._walk(workflow)
		return self._steps(run)["call"]

	def test_a_call_that_succeeded_records_succeeded(self):
		self.assertEqual(self._drive(f"{_WORKFLOW}-call-ok", 200)["outcome"], "succeeded")

	def test_a_call_that_failed_records_failed(self):
		self.assertEqual(self._drive(f"{_WORKFLOW}-call-bad", 500)["outcome"], "failed")


class TestTheControlWordsAreUntouched(_WalkHarness):
	"""`parked`, `resumed` and `done` describe control flow, which is what really happened at those nodes.
	Overwriting them with a verb output would have been the same defect facing the other way."""

	def test_a_wait_still_records_parked(self):
		workflow = self._make(f"{_WORKFLOW}-park", [
			fx.trigger(to="w1"),
			fx.node("w1", "Wait", config={"mode": "Until Event", "event_name": "probe.never"},
			        edges={"event": "end"}),
			fx.node("end", "Terminal"),
		])

		run = self._walk(workflow)

		self.assertEqual(run.status, "Parked")
		self.assertEqual(self._steps(run)["w1"]["outcome"], "parked")

	def test_a_terminal_still_records_done(self):
		workflow = self._make(f"{_WORKFLOW}-done", [
			fx.trigger(to="end"),
			fx.node("end", "Terminal"),
		])

		run = self._walk(workflow)

		self.assertEqual(self._steps(run)["end"]["outcome"], "done")


class TestAFailedRunStillReportsItsReason(_WalkHarness):
	"""`history._failure` reads `outcome == "failed"`, and this chunk adds a SECOND kind of row that
	matches it. The terminal row must still win, or a support engineer opening a dead run gets told about
	a send that was handled instead of the fault that killed it."""

	def test_a_run_the_engine_could_not_continue_reports_the_terminal_reason(self):
		"""Driven by positioning a journey at a node the frozen graph does not contain — a real `_Permanent`,
		through the real `_fail`, with nothing patched."""
		workflow = self._send_graph(f"{_WORKFLOW}-dead")

		run = self._walk(workflow, at="ghost")

		self.assertEqual(run.status, "Failed")
		failure = history._failure(run)
		self.assertIsNotNone(failure, "a failed run must say why")
		self.assertIn("ghost", failure["detail"])

	def test_a_run_that_handled_its_own_failure_reports_no_failure(self):
		"""The guard on the guard. A journey whose send failed and whose graph carried on is DONE — it did
		what the author built it to do — and `_failure` must keep returning None for it even though its
		step log now genuinely contains a `failed` row."""
		workflow = self._send_graph(f"{_WORKFLOW}-handled")

		with patch.object(sends, "sends_enabled", return_value=True):
			run = self._walk(workflow)

		self.assertEqual(run.status, "Done")
		self.assertEqual(self._steps(run)["wa"]["outcome"], "failed", "the step really did fail")
		self.assertIsNone(history._failure(run), "a completed run must not be reported as a failed one")


class TestTheVocabularyCannotDriftFromTheFrontend(unittest.TestCase):
	"""B7/B12. The engine can now write any word a verb declares. Three frontend consumers colour that
	word, and a word none of them knows renders as a grey dot with no meaning.

	The lock reads the DECLARATIONS at runtime rather than a copied list, so a verb added tomorrow is
	covered tomorrow — and it matches the rendered maps by parsing their keys, so renaming a map is a
	visible failure rather than a silent pass.
	"""

	# The control-flow words the interpreter writes directly, which no verb declares.
	_CONTROL = frozenset({"ok", "parked", "resumed", "done", "failed"})

	def _declared_outputs(self):
		words = set()
		for spec in actions.VERBS.values():
			words.update(spec.get("outputs") or [])
		return words

	def _map_keys(self, filename, mapname):
		source = (_FRONTEND / filename).read_text()
		match = re.search(rf"const {mapname} = {{(.*?)}}", source, re.S)
		self.assertIsNotNone(match, f"{mapname} is not in {filename} — the lock is matching a name that moved")
		return set(re.findall(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*:", match.group(1), re.M))

	def test_every_word_the_engine_can_write_is_known_to_the_history_dots(self):
		unknown = self._declared_outputs() - self._map_keys("WorkflowHistory.vue", "OUTCOME_DOT")
		self.assertEqual(unknown, set(), f"OUTCOME_DOT does not know {sorted(unknown)} — those steps render grey")

	def test_every_word_the_engine_can_write_is_known_to_the_live_canvas_ring(self):
		"""The consumers nobody remembers: the canvas ring AND its dot both read the SAME value off the
		realtime `workflow_step` event, in a different component from the history list."""
		for mapname in ("LIVE_RING", "LIVE_DOT"):
			with self.subTest(map=mapname):
				unknown = self._declared_outputs() - self._map_keys("WorkflowNode.vue", mapname)
				self.assertEqual(unknown, set(), f"{mapname} does not know {sorted(unknown)} — the canvas goes blank")

	def test_the_control_words_are_still_rendered_too(self):
		"""The other half: fixing the verb words must not drop the control words the engine also writes."""
		dots = self._map_keys("WorkflowHistory.vue", "OUTCOME_DOT")
		for word in ("parked", "resumed", "done", "failed"):
			self.assertIn(word, dots, f"{word} is written by the interpreter and must still be coloured")


class TestTheFieldDescriptionNoLongerPromisesFiveWords(unittest.TestCase):
	"""The description is the reader's contract with the field, and it is what made `ok` believable."""

	_JSON = pathlib.Path(__file__).resolve().parents[2] / "tatva_connect" / "doctype" / "crm_workflow_step_log" / "crm_workflow_step_log.json"

	def test_the_description_tells_the_reader_a_verb_output_can_appear_here(self):
		"""Tied to the DECLARATIONS, not to a sentence: the description must name at least one word the
		verbs really declare, so it cannot go stale by saying `ok, parked, resumed, done, or failed` and
		leaving the reader to trust it — which is exactly what made the `ok` row believable."""
		fields = {f["fieldname"]: f for f in json.loads(self._JSON.read_text())["fields"]}
		description = (fields["outcome"].get("description") or "").lower()
		declared = set()
		for spec in actions.VERBS.values():
			declared.update(spec.get("outputs") or [])
		named = {word for word in declared if word in description}
		self.assertTrue(
			named, f"the description names none of the verb outputs {sorted(declared)} the engine can write here"
		)
		self.assertIn("declared", description, "it must say the value can be a verb's declared output")
