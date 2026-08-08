# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Every node type in the registry must be real — checked by iterating the registry, not a list.

The registry is the contract: the palette, the inspector, the validator and the canvas all read it. A
type declared here that the interpreter cannot execute, or that declares no outputs, is a promise the
engine does not keep — and the failure is silent, because everything downstream simply believes the
declaration.

So this suite iterates `NODE_TYPES`. **A new node type needs no new test code**: add it in W2 or W4 and
these checks cover it the same day. Hand-writing a test per type is how a declaration and its
implementation drift, which is precisely what the WhatsApp capability test missed — it compared a
hardcoded set to a copy of itself and would have passed had all seven capabilities been fiction. One
of them was.

Static: reads the registry and the interpreter's source. No DB, no site.
"""
import ast
import unittest
from pathlib import Path

import frappe

from tatva_connect.workflow_engine import refs, registry

_INTERPRETER = Path(frappe.get_app_path("tatva_connect")) / "workflow_engine" / "interpreter.py"

# A minimal valid config per type; a type added without one fails test_every_type_has_a_valid_example.
_VALID_EXAMPLE = {
	"Trigger": {"subject_doctype": "CRM Lead", "event": "Created"},
	"Route": {"routes": [{"id": "r1", "label": "r1", "condition": {"type": "rule", "field": "status", "operator": "is", "value": "New"}}]},
	"Sample": {"arms": [{"id": "a1", "label": "Treatment", "percent": 50}]},
	"Set Variables": {"assign": "{'x': 1}"},
	"Wait": {"mode": registry.FOR_DURATION, "duration": "{'minutes': 5}"},
	"Terminal": {},
	# One per effect verb — the example IS the documentation of a usable node of that type.
	"Create Task": {"task_type": "x"},
	"Update Field": {"target_doctype": "CRM Lead", "updates": [{"name": "status", "mode": "Literal", "value": "New"}]},
	"Append Child Row": {"child_table": "x", "set_fields": [{"name": "f", "mode": "Literal", "value": "v"}]},
	"Upsert Child Row": {"child_table": "x", "set_fields": [{"name": "f", "mode": "Literal", "value": "v"}]},
	"Call API": {"webhook_endpoint": "x"},
	"Create Note": {},
	"Send WhatsApp": {"contact_number": "crm_lead.mobile_no", "whatsapp_template": "x"},
	"Send Email": {"email_recipient": "sv.email", "email_template": "x"},
	"AI Voice Call": {"contact_number": "crm_lead.mobile_no", "connection": "x", "agent_id": "a"},
	"Assign to User": {"assign_mode": "Assign", "assignee_mode": "User", "assign_to_user": "x"},
	"Generate Document": {"document_template": "x"},
}


def _interpreter_source():
	return _INTERPRETER.read_text()


class TestRegistryConformance(unittest.TestCase):
	def test_the_registry_is_not_empty(self):
		"""A control: every check below iterates NODE_TYPES, so an empty registry would pass them all."""
		self.assertTrue(registry.NODE_TYPES, "the node-type registry declares nothing")

	def test_every_type_declares_its_outputs(self):
		"""Outputs are how the canvas draws handles and how the validator rejects an edge nobody
		declared. A type declaring neither form is invisible to both."""
		for node_type, declared in registry.NODE_TYPES.items():
			with self.subTest(node_type=node_type):
				self.assertTrue(
					("outputs" in declared) ^ ("outputs_by" in declared),
					f"{node_type} must declare exactly one of outputs / outputs_by",
				)

	def test_a_conditional_declaration_resolves_for_every_value_it_keys_on(self):
		"""`outputs_by` resolves outputs either from a mode-map keyed on a config field (Wait), or from a
		fixed base with rows read from a config field (Route). Either way the field it reads must be one the
		type declares, and every mapped value must resolve to exactly its declared list."""
		for node_type, declared in registry.NODE_TYPES.items():
			rule = declared.get("outputs_by")
			if not rule:
				continue
			with self.subTest(node_type=node_type):
				fields = {f["name"] for f in declared["config"]}
				keyed_on = rule["field"] if "field" in rule else rule["rows_from"]["declares"]
				self.assertIn(keyed_on, fields, f"{node_type} keys its outputs on an undeclared field")
				for value, expected in rule.get("map", {}).items():
					self.assertEqual(
						registry.outputs_for(node_type, {rule["field"]: value}), list(expected), value
					)

	def test_an_unset_conditional_field_yields_only_its_fixed_part(self):
		"""Fail closed: a Wait with no mode chosen offers no edges rather than guess one; a Route with no
		rows yet offers only its reserved base (`otherwise`), never a placeholder leg."""
		for node_type, declared in registry.NODE_TYPES.items():
			rule = declared.get("outputs_by")
			if not rule:
				continue
			with self.subTest(node_type=node_type):
				fixed = list(rule.get("base", [])) if "base" in rule else []
				self.assertEqual(registry.outputs_for(node_type, {}), fixed)

	def test_every_config_field_is_fully_described(self):
		"""The inspector renders straight from these. A field missing its type renders as nothing.

		A4 — `help` is in that list, and it is not decoration. Every field on this canvas was a label and an
		input, so an author was expected to already know the contract of each one: the Wait's delay silently
		wanted a Python dict literal, and Accepts was a blank box with a one-word label. That is one defect
		with one fix — a kwarg in the declaration and an icon in the one renderer — and it is enforced here
		rather than left optional, because half the fields getting it is worse than none: nobody can then
		tell which half is missing from which is genuinely obvious.
		"""
		for node_type, declared in registry.NODE_TYPES.items():
			for field in declared["config"]:
				with self.subTest(node_type=node_type, field=field.get("name")):
					for key in ("name", "label", "type", "help"):
						self.assertTrue(field.get(key), f"{node_type} config field lacks {key}")

	def test_every_type_can_actually_be_executed(self):
		"""The declaration must correspond to an execution branch, or a journey reaches the node and dies on
		'unknown node type'. Exactly the bug that registering Trigger exposed.

		Read from the interpreter's source rather than by running a graph: this stays a static check that
		cannot be satisfied by a handler that happens to be unreachable.
		"""
		from tatva_connect.automation import actions

		source = _interpreter_source()
		tree = ast.parse(source)
		literals = {
			n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)
		}
		for node_type in registry.NODE_TYPES:
			with self.subTest(node_type=node_type):
				# Two ways to be executable, and a type must be one of them. A CONTROL type is named in
				# the interpreter's dispatch. A VERB type is not named anywhere — the interpreter routes
				# it by lane — so what makes it runnable is that the verb declaration gives it a handler.
				named = node_type in literals or f"registry.{node_type.upper()}" in source
				runnable = actions.lane_of(node_type) == "effect" and actions.handler_of(node_type)
				self.assertTrue(
					named or runnable,
					f"{node_type} is declared but nothing can execute it: the interpreter does not name "
					f"it and it is not an effect verb with a handler",
				)

	def test_every_type_has_a_valid_example(self):
		"""Each type needs a config this suite knows is valid — which doubles as the documentation of
		what a usable node of that type looks like."""
		self.assertEqual(
			set(_VALID_EXAMPLE), set(registry.NODE_TYPES),
			"a node type was added to the registry without a valid example here",
		)

	def test_a_valid_example_passes_validation(self):
		for node_type, config in _VALID_EXAMPLE.items():
			with self.subTest(node_type=node_type):
				outputs = registry.outputs_for(node_type, config)
				self.assertEqual(registry.validate_node(node_type, config, outputs), [])

	def test_an_undeclared_output_is_rejected(self):
		"""The other direction. Without this, validation could pass everything."""
		for node_type, config in _VALID_EXAMPLE.items():
			with self.subTest(node_type=node_type):
				problems = registry.validate_node(node_type, config, ["not_a_real_output"])
				self.assertTrue(problems, f"{node_type} accepted an output it never declared")

	def test_an_undeclared_config_key_is_rejected(self):
		for node_type, config in _VALID_EXAMPLE.items():
			with self.subTest(node_type=node_type):
				problems = registry.validate_node(
					node_type, {**config, "not_a_real_setting": 1}, registry.outputs_for(node_type, config)
				)
				self.assertTrue(problems, f"{node_type} accepted a setting it never declared")

	def test_an_unknown_node_type_throws(self):
		with self.assertRaises(frappe.exceptions.ValidationError):
			registry.declaration("Teleport")

	def test_the_endpoint_exposes_every_declared_type(self):
		"""The palette is the registry. A type the endpoint drops is a type an author cannot place."""
		exposed = {entry["type"] for entry in registry.node_types()}
		self.assertEqual(exposed, set(registry.NODE_TYPES))

	# The two kinds whose value says HOW it is filled — rows of them, and one of them; same vocabulary and same lock, so a singular field cannot quietly grow a second spelling of a mode.
	MODE_KINDS = ("value_rows", "value_pair")

	def _value_rows_fields(self):
		return [
			(entry["type"], field)
			for entry in registry.node_types()
			for field in entry["config"]
			if registry.read_kind_of(field) in self.MODE_KINDS
		]

	def test_a_value_rows_field_ships_its_modes_from_the_contract(self):
		"""The mode switch's vocabulary travels with the field, and it is the CONTRACT's own words.

		A `value_rows` row says HOW it is filled, and `contract.resolve_row` decides which way by comparing
		against `refs.FROM_CONTEXT`. If the control offered its own spelling of that word the comparison
		would silently fall through to the literal branch, and a patient would receive the text
		`crm_lead.first_name` where their name belonged — a wrong message, sent, with nothing logged.

		So every word is asserted to BE the declaration's object (`assertIs`), never a string that happens
		to match. Typing `["Literal", "From Context"]` into this test would let both sides drift together
		and prove nothing.

		W8.1 — a field MAY offer its own list (`Update Field.updates` adds Increment, which no template slot
		can take), so this no longer pins one pair. It pins the thing that matters: every offered word is
		one of the four declared OBJECTS, and there is no fifth.
		"""
		found = self._value_rows_fields()
		self.assertTrue(found, "no value_rows field is declared — this lock would pass vacuously")
		declared = (refs.LITERAL, refs.FROM_CONTEXT, refs.EXPRESSION, refs.INCREMENT)
		for node_type, field in found:
			with self.subTest(node_type=node_type, field=field["name"]):
				self.assertTrue(field.get("modes"), "a value_rows field with no modes draws no switch")
				for mode in field["modes"]:
					self.assertTrue(any(mode is word for word in declared),
					                f"{mode!r} is a second spelling of a declared mode, not the declaration")

	def test_a_field_that_declares_no_modes_gets_the_default_pair(self):
		"""The default is what every send verb rides on, and it must stay the pair a template slot can take:
		a slot has no field to add to and nothing to compute against."""
		from tatva_connect.workflow_engine import contract

		for node_type, field in self._value_rows_fields():
			if field.get("modes") is not None and field["name"] in ("updates", "until_time", "set_fields"):
				continue
			with self.subTest(node_type=node_type, field=field["name"]):
				self.assertEqual(field["modes"], [contract.LITERAL, contract.FROM_CONTEXT])

	def test_a_field_that_reads_nothing_ships_no_modes(self):
		"""The negative half. `modes` says 'this control chooses HOW the value is filled'; putting it on a
		field with one way to be filled would have the inspector draw a switch with nothing to switch."""
		for entry in registry.node_types():
			for field in entry["config"]:
				if registry.read_kind_of(field) not in self.MODE_KINDS:
					with self.subTest(node_type=entry["type"], field=field.get("name")):
						self.assertIsNone(field.get("modes"))

	def test_every_effect_verb_is_a_node_type(self):
		"""A node IS a verb. Every verb the engine can actually run must be placeable on the canvas, or it
		is a capability nobody can reach; and every verb node must name a real handler, or a journey reaches it
		and dies. Iterated from the verb declaration, so a verb added there is covered the same day."""
		from tatva_connect.automation import actions

		for verb in actions.verbs_in_lane("effect"):
			with self.subTest(verb=verb):
				self.assertIn(verb, registry.NODE_TYPES, f"{verb} can be run but cannot be placed")
				self.assertTrue(registry.NODE_TYPES[verb].get("is_verb"))
				self.assertIsNotNone(actions.handler_of(verb))

	def test_a_guard_verb_is_never_a_node_type(self):
		"""A guard qualifies a save and is declared on the Trigger as a Requirement. Offering it as a node
		would put it after the save, where raising blocks nothing and only fails the journey."""
		from tatva_connect.automation import actions

		for verb in actions.verbs_in_lane("guard"):
			with self.subTest(verb=verb):
				self.assertNotIn(verb, registry.NODE_TYPES)

