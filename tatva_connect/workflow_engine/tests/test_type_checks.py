# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""W2.2 — EACH TYPE CARRIES ITS OWN CHECK, IN ITS ROW.

W2.1 built the shelf and left it almost empty: 3 of 16 types carried a check, and those three were only
the ones the old if-chain already had. Everything else published green and raised on a live patient
record — the exact failure class `graph.py`'s own docstring says the publish gate exists to close.

  Code + options:JSON   must parse. A malformed request body or match_json used to reach a real send.
  Link + grain_scoped   the value is inside the workflow's grain, asked of `taxonomy/grain.py` — the ONE
                        matcher. A blank axis on a RULE means ANY (B9), so this can never be a tuple
                        comparison; a workflow with no grain declared may name anything.
  Select                options membership, moved OUT of validate_node's generic fallback and INTO the
                        row, so the fallback stops being a second place checks live.

A check takes `(value, field, graph_config)` because a grain question cannot be answered from the field
alone — the workflow's grain lives on the Trigger, which is another node in the same graph.
"""
import unittest

from tatva_connect.workflow_engine import registry

_TRIGGER_AT_GRAIN = {"trigger-1": {"vertical": "TatvaPractice", "group": "India", "program": "FieldSales"}}


def _check(node_type, field_name, value, graph_config=None):
	field = next(f for f in registry.NODE_TYPES[node_type]["config"] if f["name"] == field_name)
	row = registry.FIELD_TYPES[field["type"]]
	if not row["check"]:
		return []
	return list(row["check"](value, field, graph_config or {}))


class TestEveryRowNamesARealCheck(unittest.TestCase):
	"""B12/B3 — the lock does not name the checks, it walks the table and calls what it finds."""

	def test_a_row_may_not_name_a_check_that_does_not_exist(self):
		for name, row in registry.FIELD_TYPES.items():
			with self.subTest(type=name):
				if row["check"] is None:
					continue
				self.assertTrue(callable(row["check"]), f"{name} names a check that is not callable")

	def test_every_check_accepts_the_one_signature(self):
		"""One signature, or the table cannot call them uniformly and a caller starts special-casing."""
		for name, row in registry.FIELD_TYPES.items():
			with self.subTest(type=name):
				if row["check"] is None:
					continue
				self.assertIsInstance(row["check"](None, {"name": "x", "label": "X"}, {}), list)

	def test_the_types_that_must_carry_a_check_do(self):
		"""W2.1 left these empty and that was the whole of W2.2."""
		for name in ("Code", "Link", "Select", "Predicate", "Mapping", "Requirements"):
			with self.subTest(type=name):
				self.assertIsNotNone(registry.FIELD_TYPES[name]["check"], f"{name} carries no check")


class TestCodeMustParse(unittest.TestCase):
	"""THE red: a malformed JSON body published green and raised on a live record."""

	def test_a_malformed_json_body_is_refused(self):
		found = _check("Call API", "request_body", '{"a": ')
		self.assertTrue(found, "a broken JSON body must be refused at publish")
		self.assertIn("JSON", found[0])

	def test_a_valid_json_body_passes(self):
		self.assertEqual(_check("Call API", "request_body", '{"a": "$ctx.crm_lead.first_name"}'), [])

	def test_an_empty_body_is_not_a_parse_error(self):
		"""Blank is the `reqd` rule's business, not the parser's — two rules, two messages."""
		self.assertEqual(_check("Call API", "request_body", ""), [])

	def test_a_code_field_that_is_not_json_is_not_parsed_as_json(self):
		"""`Set Variables.assign` is an expression dict, not JSON. Checking it as JSON refused every
		valid Set Variables — that regression happened once already in W2.1."""
		self.assertEqual(_check("Set Variables", "assign", '{"x": ctx["y"]}'), [])


class TestSelectChecksItsOwnOptions(unittest.TestCase):
	"""Moved out of the generic fallback: two places to look for one rule is the defect class."""

	def test_a_value_outside_the_declared_options_is_refused(self):
		found = _check("Trigger", "event", "Exploded")
		self.assertTrue(found)
		self.assertIn("Exploded", found[0])

	def test_a_declared_option_passes(self):
		self.assertEqual(_check("Trigger", "event", "Created"), [])

	def test_the_generic_fallback_no_longer_checks_options(self):
		"""G5 — when a check moves into a row it is DELETED from where it was."""
		import inspect

		source = inspect.getsource(registry.validate_node)
		self.assertNotIn("is not a valid", source, "the options message still lives in the fallback")


class TestGrainScopedLinkAsksTheOneMatcher(unittest.TestCase):
	"""B9 — one matcher, `taxonomy/grain.py`. Never an exact tuple comparison."""

	def test_a_value_outside_the_workflows_grain_is_refused(self):
		field = {"name": "task_type", "label": "Task Type", "type": "Link",
		         "link": "CRM Task Type", "grain_scoped": True}
		found = registry.FIELD_TYPES["Link"]["check"]("::OtherVertical::x", field, _TRIGGER_AT_GRAIN)
		self.assertTrue(found, "a value from another grain must be refused")

	def test_a_link_that_is_not_grain_scoped_is_never_grain_checked(self):
		"""Most links carry no axes at all — grain-checking them would refuse every valid value."""
		field = {"name": "whatsapp_template", "label": "Template", "type": "Link",
		         "link": "WhatsApp Templates"}
		self.assertEqual(registry.FIELD_TYPES["Link"]["check"]("anything-at-all", field, _TRIGGER_AT_GRAIN), [])

	def test_a_workflow_with_no_grain_declared_accepts_anything(self):
		"""A blank axis on a RULE means ANY. Comparing it as an empty string is the defect that hid 129
		fields from 1,894 leads while every test stayed green."""
		field = {"name": "task_type", "label": "Task Type", "type": "Link",
		         "link": "CRM Task Type", "grain_scoped": True}
		self.assertEqual(registry.FIELD_TYPES["Link"]["check"]("::Anything::x", field, {"trigger-1": {}}), [])
