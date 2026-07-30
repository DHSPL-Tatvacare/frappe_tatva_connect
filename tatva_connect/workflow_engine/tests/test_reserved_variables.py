# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""An engine-reserved name is refused on EVERY field that writes journey state, not only on a Mapping.

`RESERVED_VARIABLES` says these names are "refused at author time rather than debugged at 3am". They were
not: `validate_node` ran the check only for `type == "Mapping"`, and the two fields that actually write
state — `Set Variables.assign` and `Wait.accepts` — are `Code`. So `{"_emitted": "x"}` published green,
`state.update` replaced the correlation map the wake reads, and the journey parked with nothing able to reach
it. For `accepts` the value comes off an external signal payload, so anyone who can edit the subject could
route it onto `_emitted`.

Iterated over the registry's own `writes` declarations, so a third field that declares `writes` is covered
the day it is added rather than the day someone remembers to list it here.

Static: registry + AST/JSON parsing only. No DB.
"""
import json
import unittest

from tatva_connect.workflow_engine import registry

# One config per (node type, writing field) that puts NAME into the keys that field writes. Keyed by the
# declared `writes` kind, so the shape is stated once per kind rather than once per node type. `assign` is
# a Python expression (read by AST); `accepts` is JSON (read by the interpreter's own parser).
_BY_KIND = {
	"expression_dict": lambda name: f"{{{name!r}: 1}}",
	"payload_map": lambda name: json.dumps({"some.path": name}),
}


def _writing_fields():
	"""Every (node type, field) that declares `writes` — the registry's own list, never a copy."""
	return [
		(node_type, field)
		for node_type, declared in registry.NODE_TYPES.items()
		for field in declared["config"]
		if field.get("writes")
	]


def _config_writing(node_type, field, name):
	"""A minimal config for this node type in which `field` writes the key `name`."""
	from tatva_connect.workflow_engine.tests.test_registry_conformance import _VALID_EXAMPLE

	config = dict(_VALID_EXAMPLE[node_type])
	if node_type == "Wait":  # `accepts` only applies in the event-waiting modes
		config = {"mode": registry.UNTIL_EVENT, "event_name": "probe.done"}
	config[field["name"]] = _BY_KIND[field["writes"]](name)
	return config


class TestReservedVariables(unittest.TestCase):
	def test_there_are_fields_that_write_state(self):
		"""A control: every check below iterates the declarations, so an empty list would pass them all."""
		self.assertTrue(_writing_fields(), "no field declares `writes` — this suite would prove nothing")

	def test_a_reserved_name_is_refused_on_every_writing_field(self):
		for node_type, field in _writing_fields():
			for name in registry.RESERVED_VARIABLES:
				with self.subTest(node_type=node_type, field=field["name"], name=name):
					config = _config_writing(node_type, field, name)
					problems = registry.validate_node(node_type, config, registry.outputs_for(node_type, config))
					self.assertTrue(
						[p for p in problems if p["field"] == field["name"]],
						f"{node_type}.{field['name']} accepted the reserved name {name}",
					)

	def test_any_underscore_name_is_refused(self):
		"""The engine's bookkeeping shares the state dict, so the whole underscore namespace is its own —
		the same rule the Mapping check already applied."""
		for node_type, field in _writing_fields():
			with self.subTest(node_type=node_type, field=field["name"]):
				config = _config_writing(node_type, field, "_anything")
				problems = registry.validate_node(node_type, config, registry.outputs_for(node_type, config))
				self.assertTrue([p for p in problems if p["field"] == field["name"]])

	def test_an_ordinary_name_still_publishes(self):
		"""The other direction. A gate that refused everything would pass the checks above."""
		for node_type, field in _writing_fields():
			with self.subTest(node_type=node_type, field=field["name"]):
				config = _config_writing(node_type, field, "outcome")
				self.assertEqual(
					registry.validate_node(node_type, config, registry.outputs_for(node_type, config)), []
				)

	def test_keys_that_cannot_be_enumerated_are_not_refused(self):
		"""A computed `assign` key cannot be read without evaluating the author's expression, so it is not
		checked — stated openly rather than guessed at. Guessing "reserved" would block correct workflows."""
		config = {"assign": "dict(other)"}
		self.assertEqual(registry.validate_node("Set Variables", config, ["next"]), [])

	def test_a_mapping_capture_is_still_refused(self):
		"""The behaviour that already worked must keep working — Call API's `capture` rows."""
		config = {"webhook_endpoint": "x", "capture": [{"path": "a", "variable": "_emitted"}]}
		problems = registry.validate_node("Call API", config, registry.outputs_for("Call API", config))
		self.assertTrue([p for p in problems if p["field"] == "capture"])
