# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""What a node DECLARES it writes must be what it really writes.

`emits` (and a config field's `writes`) is not documentation. `upstream.available_map` turns those
declarations into "what may this node read", and `graph._reference_problems` REFUSES to publish a graph
that reads something nothing upstream produces. The gate is therefore only as true as the declarations
it trusts, and a false declaration makes it worse than no gate at all:

  * declared but not written — the gate CERTIFIES a graph that fails on a live record. A Branch on `ok`
    publishes green, then `rules._rule_match` raises and the run is marked Failed; a `Variable` field
    resolves to None and the node silently takes its empty path with nothing logged.
  * written but not declared — the gate REJECTS a correct graph. A false rejection is worse still: it
    teaches authors that the check is wrong and must be worked around.

So every declaration is DRIVEN here and compared with the state the run really carries afterwards.
Engine bookkeeping (`registry.RESERVED_VARIABLES`) is excluded from the comparison rather than tolerated
as an extra — an author may not take those names, so they are not part of anybody's contract.

**A new verb needs no new test code.** The suite iterates `actions.VERBS` and `registry.NODE_TYPES`: a
verb added without a driver fails `test_every_effect_verb_is_driven_or_declared_undriven`, and a verb
that declares emits may not be listed UNDRIVEN at all. Silent omission is what let three false
declarations ship, and it is exactly what this shape forbids.

The only thing stubbed is the outbound boundary — Call API's HTTP call. Everything the engine does with
the answer runs for real, through `interpreter._run_verb`, which is the same entry `advance` uses.
"""
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.automation import actions
from tatva_connect.tests.authz.grains import assert_masters_exist
from tatva_connect.workflow_engine import interpreter, refs, registry, upstream
from tatva_connect.workflow_engine.tests import fixtures as fx

_CALL_ENDPOINT = "tatva_connect.automation.actions._call_endpoint"
_ENDPOINT = "Emits Conformance Endpoint"

# An effect verb this suite cannot drive, and why. A verb listed here is NOT exempt from the contract:
# it may not declare `emits` (see test_an_undriven_verb_may_not_declare_emits), so the declaration it
# does not have cannot be false. Every entry needs a written reason.
UNDRIVEN = {
	"Create Task": "needs a CRM Task Type master on the lead's grain — operator data, not test data",
	"Update Field": "needs an enabled CRM Automation Field allowlist row, which is operator config",
	"Append Child Row": "needs an enabled child-table allowlist row, which is operator config",
	"Upsert Child Row": "needs an enabled child-table allowlist row, which is operator config",
}


def _reserved(keys):
	"""Engine bookkeeping, dropped from both sides of the comparison.

	Dropped by SOURCE now that every value is namespaced: the engine keeps `_engine.token`,
	`_engine.emitted` and the rest under its own source, so one membership test covers all of them and a
	new piece of bookkeeping needs no change here."""
	return {k for k in keys if (refs.parse(k) or ("", ""))[0] != refs.ENGINE}


def _declared_downstream(node_type, config):
	"""What the PUBLISH GATE believes this node contributes — asked the way the gate asks it.

	Read off `available_map` for a node wired to a Terminal rather than off `emits` directly: the gate is
	the consumer that matters, and a declaration the resolver drops on the way (a verb's `emits_from`
	rows, a field's `writes`) would otherwise look correct here and still reject a correct graph.
	"""
	graph = [
		{
			"node_id": "w", "node_type": node_type, "config_json": frappe.as_json(config),
			"edges": [{"from_output": out, "to_node": "end"}
			          for out in registry.outputs_for(node_type, config)],
		},
		{"node_id": "end", "node_type": "Terminal", "config_json": "{}", "edges": []},
	]
	available, _opaque = upstream.available_map(graph)
	return _reserved(available.get("end") or set())


class TestEmitsConformance(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		assert_masters_exist()
		cls.lead = fx.make_lead()
		frappe.db.set_value("CRM Lead", cls.lead.name, "mobile_no", "9800000001")
		_ensure_endpoint()
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		frappe.delete_doc("CRM Lead", cls.lead.name, force=True, ignore_permissions=True)
		if frappe.db.exists("Webhook", _ENDPOINT):
			frappe.delete_doc("Webhook", _ENDPOINT, force=True, ignore_permissions=True)
		frappe.db.commit()

	# --- the driver -------------------------------------------------------------------------------------

	def _drive_verb(self, verb, config, seed=None):
		"""Run one verb the way the interpreter runs it, and return the state keys it wrote.

		`interpreter._run_verb` rather than the handler directly: that is the engine's own entry to a
		verb, so the savepoint, the `action_type` stamp and the correlation token are all real here.
		"""
		# A `refs.Values`, because that is what the interpreter really hands a verb: `_run_verb` scopes the
		# handler to the node's own writer view, which is how a bare `patient_id` lands at `w.patient_id`.
		state = refs.Values(buckets=seed or {})
		node = frappe._dict({
			"node_id": "w", "node_type": verb,
			"config_json": frappe.as_json(config), "edges": [],
		})
		interpreter._run_verb(node, self.lead.name, self.lead, state, fx.AXES, run_name="conformance")
		before = {refs.of_node(source, k) for source, bucket in (seed or {}).items() for k in bucket}
		return _reserved(state.keys()) - _reserved(before)

	def _assert_conformant(self, verb, config, seed=None, note=""):
		written = self._drive_verb(verb, config, seed)
		declared = _declared_downstream(verb, config)
		self.assertEqual(
			written, declared,
			f"{verb} {note}: declares {sorted(declared)} but writes {sorted(written)} — "
			"the publish gate trusts the declaration, so a difference either certifies a broken "
			"graph or rejects a correct one",
		)

	# --- coverage: nothing may be silently omitted -------------------------------------------------------

	def test_every_effect_verb_is_driven_or_declared_undriven(self):
		"""The check that keeps this suite honest as verbs are added. A new verb is covered or named."""
		for verb in actions.verbs_in_lane("effect"):
			with self.subTest(verb=verb):
				self.assertTrue(
					verb in _DRIVERS or verb in UNDRIVEN,
					f"{verb} is neither driven here nor listed in UNDRIVEN with a reason",
				)

	def test_an_undriven_verb_may_not_declare_emits(self):
		"""A declaration that is never driven is a declaration nothing checks — which is the defect."""
		for verb, reason in UNDRIVEN.items():
			with self.subTest(verb=verb):
				self.assertTrue(reason, f"{verb} is UNDRIVEN with no reason given")
				self.assertFalse(
					actions.emits_of(verb),
					f"{verb} declares emits, so it must be driven here — not listed UNDRIVEN",
				)

	def test_a_guard_verb_declares_no_emits(self):
		"""A guard runs inside `validate`, where there is no run state at all. Anything it declared as
		emitted would be offered downstream by a resolver that could never see it written."""
		for verb in actions.verbs_in_lane("guard"):
			with self.subTest(verb=verb):
				self.assertFalse(actions.emits_of(verb), f"{verb} is a guard and cannot write run state")

	def test_every_node_type_declaring_writes_is_driven(self):
		"""The same rule for the core node types, whose writes are declared on a config FIELD."""
		for node_type in registry.NODE_TYPES:
			for field in registry.config_fields(node_type):
				if not field.get("writes"):
					continue
				with self.subTest(node_type=node_type, field=field["name"]):
					self.assertIn(
						node_type, _NODE_DRIVERS,
						f"{node_type}.{field['name']} declares writes but nothing here drives it",
					)

	# --- the verbs --------------------------------------------------------------------------------------

	def test_call_api_writes_the_response_shape_it_declares(self):
		"""`status`, `ok` and `error` are declared "always written" and a Branch downstream is offered
		them. They were never written into state at all, so that Branch published green and raised on the
		first live record."""
		response = {"status": 201, "ok": True, "body": {"data": {"id": "P-1"}}, "error": None}
		config = {
			"webhook_endpoint": _ENDPOINT,
			"capture": [{"path": "body.data.id", "variable": "patient_id"}],
		}
		with patch(_CALL_ENDPOINT, return_value=response):
			self._assert_conformant("Call API", config, note="on a successful call")

	def test_call_api_writes_the_same_keys_when_the_call_could_not_be_made(self):
		"""A transport failure is DATA here, so it must leave the same variables behind — a downstream
		node reading `error` cannot be offered a key that exists only on the happy path."""
		response = {"status": 0, "ok": False, "body": None, "error": "Connection refused"}
		with patch(_CALL_ENDPOINT, return_value=response):
			self._assert_conformant("Call API", {"webhook_endpoint": _ENDPOINT}, note="on a transport failure")

	def test_call_api_writes_the_declared_types(self):
		"""`ok` is declared a Check and `status` an Int. The predicate evaluator resolves operators by
		TYPE, so a boolean where a Check is declared compares as the wrong thing."""
		response = {"status": 201, "ok": True, "body": {}, "error": None}
		state = refs.Values()
		with patch(_CALL_ENDPOINT, return_value=response):
			node = frappe._dict({
				"node_id": "w", "node_type": "Call API",
				"config_json": frappe.as_json({"webhook_endpoint": _ENDPOINT}), "edges": [],
			})
			interpreter._run_verb(node, self.lead.name, self.lead, state, fx.AXES)
		self.assertEqual(state["w.ok"], 1, "a Check is 1/0, never True/False")
		self.assertEqual(state["w.status"], 201)
		self.assertIsInstance(state["w.error"], str)

	def test_assign_to_user_writes_who_now_holds_the_lead(self):
		"""`assigned_to` is declared and was never written: a downstream node reading it got None and
		took its empty path with nothing in the log to say why."""
		self._assert_conformant("Assign to User", {
			"assign_mode": "Assign", "assignee_mode": "User", "assign_to_user": "Administrator",
		}, note="when someone was assigned")

	def test_assign_to_user_writes_the_same_key_when_nobody_resolved(self):
		"""The `nobody` leg still ran the node. A key that appears only sometimes cannot be offered
		downstream at all — so it is written empty, exactly as a Call API capture that resolved to
		nothing is."""
		self._assert_conformant("Assign to User", {
			"assign_mode": "Assign", "assignee_mode": "User",
		}, note="when nobody resolved")

	def test_create_note_writes_nothing_and_declares_nothing(self):
		"""The other direction: a verb that writes an UNDECLARED key would make the gate reject a
		correct graph. Every non-emitting verb is held to that here."""
		self._assert_conformant("Create Note", {"comment_mode": "Literal", "comment_text": "probe"})

	def test_send_whatsapp_writes_nothing_and_declares_nothing(self):
		"""Driven with sends dormant, which is the shipped default — nothing leaves the site."""
		self._assert_conformant("Send WhatsApp", {"whatsapp_template": "probe"})

	def test_send_email_writes_nothing_and_declares_nothing(self):
		self._assert_conformant("Send Email", {
			"email_recipient": "sv.email", "email_template": "probe",
		})

	# --- the core node types ----------------------------------------------------------------------------

	def test_set_variables_writes_the_keys_its_expression_names(self):
		config = {"assign": '{"stage": "Qualified", "score": 7}'}
		state = refs.Values()
		interpreter._next_control(_node_doc("Set Variables", config), state)
		self.assertEqual(_reserved(state.keys()), _declared_downstream("Set Variables", config))

	def test_a_wait_declares_the_state_keys_its_accepts_map_writes(self):
		"""A Wait writes arbitrary state keys through `accepts` and declared none of them, so the gate
		REJECTED a correct graph: a node reading a value the event carried in was told nothing upstream
		produces it."""
		config = {
			"mode": registry.UNTIL_EVENT, "event_name": "task.completed", "source_node": "t",
			"accepts": '{"data.diagnosis": "diagnosis", "results.0.label": "label"}',
		}
		merged = interpreter._map_payload(
			config["accepts"], {"data": {"diagnosis": "T2DM"}, "results": [{"label": "high"}]}
		)
		# `_map_payload` yields the author's bare state keys; the interpreter merges them through the Wait's
		# own writer view, so what a downstream node really reads is `<node_id>.<key>`.
		written = _reserved({refs.of_node("w", key) for key in merged})
		self.assertEqual(written, _declared_downstream("Wait", config))


def _node_doc(node_type, config):
	return frappe._dict({
		"node_id": "w", "node_type": node_type,
		"config_json": frappe.as_json(config), "edges": [],
	})


# Which verbs this suite drives, by the test that drives them. Read by the coverage check above, so a
# verb cannot be covered by accident and cannot be dropped by accident.
_DRIVERS = {
	"Call API": "test_call_api_writes_the_response_shape_it_declares",
	"Assign to User": "test_assign_to_user_writes_who_now_holds_the_lead",
	"Create Note": "test_create_note_writes_nothing_and_declares_nothing",
	"Send WhatsApp": "test_send_whatsapp_writes_nothing_and_declares_nothing",
	"Send Email": "test_send_email_writes_nothing_and_declares_nothing",
}

_NODE_DRIVERS = {
	"Set Variables": "test_set_variables_writes_the_keys_its_expression_names",
	"Wait": "test_a_wait_declares_the_state_keys_its_accepts_map_writes",
}


def _ensure_endpoint():
	"""The curated Webhook a Call API node PICKS. The node checks it exists before it calls anything, so
	the verb cannot be driven without one — the URL is never reached (the boundary is stubbed)."""
	if frappe.db.exists("Webhook", _ENDPOINT):
		return
	frappe.get_doc({
		"doctype": "Webhook", "name": _ENDPOINT, "webhook_doctype": "CRM Lead",
		"request_url": "https://example.invalid/probe", "request_method": "POST",
		"webhook_docevent": "on_update",
	}).insert(ignore_permissions=True)  # authz-ok: tier-c — test fixture, no user input
