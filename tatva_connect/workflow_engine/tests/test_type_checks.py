# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""EACH TYPE CARRIES ITS OWN CHECK, IN ITS ROW, AND `validate_node` ONLY WALKS THE TABLE.

W2.1 built the shelf. This fills it and removes the last rule that was still spelled inline.

THE SIGNATURE, decided first because everything else depends on it: a FIELD_TYPES check takes
`(value, field, config, graph_context)`. `value` and `field` are what a per-field rule needs; `config` is
this node's OWN settings, because a field's validity can depend on a SIBLING field (a `Field` is only
judgeable against the `Target` beside it); `graph_context` is the resolved graph, because it can also
depend on facts outside the node. Node-level and graph-level are kept apart rather than merged into one
bag, so a check says which it is reaching for. READ_KINDS/WRITE_KINDS checks keep `(value)` — they parse a
value and genuinely need nothing else, and widening them would be ceremony.

A CHECK MAY NOT INVENT. `graph_context` is optional. A check that cannot answer without it returns NO
problem rather than guessing one: a false problem blocks a publish the author has no way to fix, which is
strictly worse than a rule that waits until runtime.

THE STRUCTURAL CLAIM, and the only one that matters: a node type or config field declared tomorrow is
validated the day it is declared, with no new validator code. `TestANewTypeValidatesWithNoNewCode`
proves it by declaring one at test time.
"""
import ast
import inspect
import pathlib
import re
import unittest
from unittest.mock import patch

from tatva_connect.workflow_engine import registry

_TRIGGER = "Trigger"


def _problems(node_type, config, outputs=None, context=None):
	"""THE REAL ENTRY POINT. A test that calls a private check proves the function and nothing about
	validation; every assertion here goes through the validator the publish gate actually calls."""
	return registry.validate_node(
		node_type, config, outputs or [], mode=registry.PUBLISH, graph_context=context
	)


def _messages(*args, **kwargs):
	return " | ".join(p["message"] for p in _problems(*args, **kwargs))


class TestTheSignatureIsOneShape(unittest.TestCase):
	"""Sixteen rows called through one call site; a row with a different arity is a crash at publish."""

	def test_every_row_check_accepts_value_field_config_and_context(self):
		for name, row in registry.FIELD_TYPES.items():
			if not row["check"]:
				continue
			with self.subTest(type=name):
				signature = inspect.signature(row["check"])
				self.assertEqual(
					len(signature.parameters), 4,
					f"{name}'s check takes {list(signature.parameters)} — the table calls it with four",
				)

	def test_every_row_check_is_callable_with_nothing_configured(self):
		"""An unset field must not raise: an author saves a half-built node constantly."""
		for name, row in registry.FIELD_TYPES.items():
			if not row["check"]:
				continue
			with self.subTest(type=name):
				self.assertIsInstance(list(row["check"](None, {"name": "x", "label": "X"}, {}, None)), list)

	def test_the_kind_checks_stay_single_argument(self):
		"""READ/WRITE kinds parse a value. Widening them to match would be ceremony, so the difference is
		asserted rather than left to drift."""
		for table in (registry.READ_KINDS, registry.WRITE_KINDS):
			for name, row in table.items():
				if not row["check"]:
					continue
				with self.subTest(kind=name):
					self.assertEqual(len(inspect.signature(row["check"]).parameters), 1)


class TestSelectChecksItsOwnOptions(unittest.TestCase):
	"""MOVED from `validate_node`'s body onto the `Select` row. Two places to look for one rule is the
	defect class this whole effort exists to remove."""

	def test_a_value_outside_the_declared_options_is_refused(self):
		found = _messages(_TRIGGER, {"subject_doctype": "CRM Lead", "event": "Exploded"})
		self.assertIn("Exploded", found)

	def test_a_declared_option_passes(self):
		found = _messages(_TRIGGER, {"subject_doctype": "CRM Lead", "event": "Created"})
		self.assertNotIn("Created is not", found)

	def test_the_rule_left_the_validator_body(self):
		"""G5 — when a check moves onto a row it is DELETED from where it was, not left as a twin."""
		source = inspect.getsource(registry.validate_node)
		self.assertNotIn("is not a valid", source, "the options message still lives in the validator body")
		self.assertNotIn('field.get("options")', source, "the validator still reads options itself")

	def test_the_check_lives_on_the_row(self):
		self.assertIsNotNone(registry.FIELD_TYPES["Select"]["check"])


class TestACheckNeverSkipsAnother(unittest.TestCase):
	"""The landmine W2.1 left: `validate_node` did `continue` after a row check, so a type carrying BOTH a
	row check and a read kind would silently skip the read check. Nothing declared that combination yet,
	which is exactly why it would have been found the hard way."""

	def test_a_row_check_and_a_read_kind_both_run(self):
		fake = dict(registry.FIELD_TYPES["Predicate"])
		fake["reads"] = "expression"
		with patch.dict(registry.FIELD_TYPES, {"Predicate": fake}):
			found = _messages("Trigger", {"subject_doctype": "CRM Lead", "event": "Created", "predicate": "for x in y: pass"})
		self.assertIn("expression", found.lower(), "the read-kind check was skipped by the row check")


class TestANewTypeValidatesWithNoNewCode(unittest.TestCase):
	"""THE CHUNK'S WHOLE CLAIM, proven structurally rather than argued.

	A type declared at test time — a row in the table and a field on a real node — is validated by the
	validator that already exists. If this needs a line of validator code to pass, the seam does not hold
	and the six node types W7 adds will each arrive with hand-written validation again.
	"""

	def _declare(self, check):
		row = {"control": "data", "check": check, "primitive": True, "reads": None,
		       "scalar": True, "summary": None}
		field = {"name": "zz_probe", "label": "Probe", "type": "ZZ Probe"}
		declared = dict(registry.NODE_TYPES["Terminal"])
		declared["config"] = [*declared["config"], field]
		return row, declared

	# The check a new type ships with - DECLARATION, not validator code: nothing in validate_node knows this type exists.
	@staticmethod
	def _refuse_shouting(value, field, config, context):
		return [f"{field['label']} must not shout"] if value and value.isupper() else []

	def test_a_type_declared_today_refuses_a_bad_value_today(self):
		row, declared = self._declare(self._refuse_shouting)
		with patch.dict(registry.FIELD_TYPES, {"ZZ Probe": row}), \
		     patch.dict(registry.NODE_TYPES, {"Terminal": declared}):
			found = _messages("Terminal", {"zz_probe": "LOUD"})
		self.assertIn("must not shout", found)

	def test_the_same_type_accepts_a_good_value(self):
		row, declared = self._declare(self._refuse_shouting)
		with patch.dict(registry.FIELD_TYPES, {"ZZ Probe": row}), \
		     patch.dict(registry.NODE_TYPES, {"Terminal": declared}):
			found = _messages("Terminal", {"zz_probe": "quiet"})
		self.assertNotIn("must not shout", found)

	def test_the_problem_is_anchored_on_the_field_so_the_canvas_can_mark_it(self):
		"""Author error is DATA. A message with no field name can only be toasted."""
		row, declared = self._declare(self._refuse_shouting)
		with patch.dict(registry.FIELD_TYPES, {"ZZ Probe": row}), \
		     patch.dict(registry.NODE_TYPES, {"Terminal": declared}):
			found = [p for p in _problems("Terminal", {"zz_probe": "LOUD"}) if "shout" in p["message"]]
		self.assertEqual(found[0]["field"], "zz_probe")

	def test_a_type_needing_no_check_is_still_a_legal_row(self):
		row, declared = self._declare(None)
		with patch.dict(registry.FIELD_TYPES, {"ZZ Probe": row}), \
		     patch.dict(registry.NODE_TYPES, {"Terminal": declared}):
			self.assertNotIn("zz_probe", _messages("Terminal", {"zz_probe": "anything"}))


class TestNoTypeSwitchReturnsToTheValidator(unittest.TestCase):
	"""B12 — forbids the SHAPE, and the scan surface is asserted rather than assumed. A lock pointed at
	the wrong file reads as coverage and is worse than none; that has happened on this surface."""

	_SWITCH = re.compile(r"""\[.type.\]\s*==\s*['"]|\.get\(.type.\)\s*==""")

	def _validation_surface(self):
		"""THE SCAN SURFACE, built rather than assumed: the validator plus every check the table names.
		Deliberately NOT the whole module - `_scope_kind`/`_scoped` switch on `type == "Link"` to decide
		which CONTROL is grain-narrowed, which is a different question from validation and is not this
		chunk's to move. A lock that failed on them would force an unrelated refactor to go green."""
		sources = {"validate_node": inspect.getsource(registry.validate_node)}
		for name, row in registry.FIELD_TYPES.items():
			if row["check"]:
				sources[f"{name} check"] = inspect.getsource(row["check"])
		return sources

	def test_the_scan_surface_really_holds_the_validator_and_the_checks(self):
		"""The lock's own aim, asserted first: a lock pointed at the wrong source reads as coverage."""
		surface = self._validation_surface()
		self.assertIn("def validate_node", surface["validate_node"])
		self.assertIn("Select check", surface, "the table's checks are not being scanned")

	def test_no_switch_on_a_field_type_string_in_the_validation_path(self):
		offenders = [
			f"{where}:{n}"
			for where, source in self._validation_surface().items()
			for n, line in enumerate(source.splitlines(), 1)
			if self._SWITCH.search(line) and not line.strip().startswith("#")
		]
		self.assertEqual(offenders, [], f"a per-type switch is back at: {offenders}")

	def test_the_validator_dispatches_through_the_table_and_not_a_ladder(self):
		"""Walked with AST: the validator body may contain no `if` whose test compares to a type name."""
		tree = ast.parse(inspect.getsource(registry.validate_node).lstrip())
		names = {name for name in registry.FIELD_TYPES}
		offenders = [
			node.lineno for node in ast.walk(tree)
			if isinstance(node, ast.Compare)
			for operand in [*node.comparators, node.left]
			if isinstance(operand, ast.Constant) and operand.value in names
		]
		self.assertEqual(offenders, [], f"validate_node compares against a type name at lines {offenders}")


class TestEveryRowIsDeliberate(unittest.TestCase):
	"""A row with no check must be a decision, not an oversight."""

	_MUST_CHECK = ("Predicate", "Mapping", "Requirements", "Select")

	def test_the_types_that_can_be_wrong_on_their_own_carry_a_check(self):
		for name in self._MUST_CHECK:
			with self.subTest(type=name):
				self.assertIsNotNone(registry.FIELD_TYPES[name]["check"], f"{name} carries no check")

	def test_code_carries_no_row_check_because_its_parse_belongs_to_its_kind(self):
		"""`Code` spans three semantics — an expression dict, a ctx JSON body and a payload map. A JSON
		check on the row would refuse every valid `Set Variables.assign`, which regressed once already."""
		self.assertIsNone(registry.FIELD_TYPES["Code"]["check"])
		self.assertIsNotNone(registry.READ_KINDS["ctx_json"]["check"])
		self.assertIsNotNone(registry.READ_KINDS["expression"]["check"])
		self.assertIsNotNone(registry.WRITE_KINDS["payload_map"]["check"])

	def test_variable_carries_no_row_check_because_the_graph_answers_it(self):
		""""Does this reference resolve upstream" needs the whole graph and this node's position in it.
		`graph._reference_problems` already answers it from `upstream.available_map`. A row check would be
		a second implementation of one rule."""
		self.assertIsNone(registry.FIELD_TYPES["Variable"]["check"])
		from tatva_connect.workflow_engine import graph

		self.assertIn("available_map", inspect.getsource(graph._reference_problems))
