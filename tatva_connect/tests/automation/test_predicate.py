# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The predicate evaluator — the ONE thing that decides whether a rule applies to a subject.

Two properties matter here, and the second is the reason this rewrite happened.

COMPOSITION. all / any / not nest arbitrarily, and the leaf comparison is the same operator brain the
flat criteria list always used. Nothing about how an operator compares changed; what changed is that
conditions can now be combined.

LOUDNESS. A predicate that names a field the subject does not have RAISES. The old evaluator returned
False for anything it could not evaluate, which meant a typo in a fieldname produced a rule that looked
correct in the builder, never fired, and reported nothing — indistinguishable from a rule whose
condition legitimately did not hold. Every test below that asserts a raise is guarding that.

Static: pure evaluation against dicts. No DB, no site.
"""
import unittest

from tatva_connect.automation import rules

# A subject's declared fields. Where this is passed it is also the allowlist of what may be referenced.
_TYPES = {"status": "Data", "score": "Int", "city": "Data"}
_CONTEXT = {"status": "New", "score": 40, "city": "Pune"}


def _rule(field, operator, value=None, **extra):
	return {"type": rules.RULE, "field": field, "operator": operator, "value": value, **extra}


def _match(predicate, context=None, field_types=_TYPES):
	return rules.predicate_match(predicate, context or _CONTEXT, field_types)


class TestPredicate(unittest.TestCase):
	# --- an empty gate is open -------------------------------------------------------------------------

	def test_no_predicate_matches_everything(self):
		for empty in (None, {}, ""):
			with self.subTest(empty=empty):
				self.assertTrue(_match(empty))

	def test_an_empty_group_matches(self):
		"""A half-built group must not read as false — the builder blocks the save, not the evaluator."""
		for kind in (rules.ALL, rules.ANY):
			with self.subTest(kind=kind):
				self.assertTrue(_match({"type": kind, "children": []}))

	# --- the leaf ---------------------------------------------------------------------------------------

	def test_a_single_rule_evaluates_both_ways(self):
		self.assertTrue(_match(_rule("status", "is", "New")))
		self.assertFalse(_match(_rule("status", "is", "Closed")))

	def test_the_leaf_still_uses_the_shared_typed_comparison(self):
		"""The operator brain is untouched — an Int field compares numerically, not as text."""
		self.assertTrue(_match(_rule("score", "greater than", "30")))
		self.assertFalse(_match(_rule("score", "greater than", "50")))

	# --- composition ------------------------------------------------------------------------------------

	def test_all_requires_every_child(self):
		both = [_rule("status", "is", "New"), _rule("city", "is", "Pune")]
		self.assertTrue(_match({"type": rules.ALL, "children": both}))
		one_bad = [_rule("status", "is", "New"), _rule("city", "is", "Mumbai")]
		self.assertFalse(_match({"type": rules.ALL, "children": one_bad}))

	def test_any_requires_one_child(self):
		one_good = [_rule("status", "is", "Closed"), _rule("city", "is", "Pune")]
		self.assertTrue(_match({"type": rules.ANY, "children": one_good}))
		none_good = [_rule("status", "is", "Closed"), _rule("city", "is", "Mumbai")]
		self.assertFalse(_match({"type": rules.ANY, "children": none_good}))

	def test_not_inverts_its_child(self):
		self.assertFalse(_match({"type": rules.NOT, "children": [_rule("status", "is", "New")]}))
		self.assertTrue(_match({"type": rules.NOT, "children": [_rule("status", "is", "Closed")]}))

	def test_groups_nest(self):
		"""status is New AND (city is Mumbai OR score > 30) — the shape a flat list could never express."""
		predicate = {
			"type": rules.ALL,
			"children": [
				_rule("status", "is", "New"),
				{"type": rules.ANY, "children": [
					_rule("city", "is", "Mumbai"),
					_rule("score", "greater than", 30),
				]},
			],
		}
		self.assertTrue(_match(predicate))
		self.assertFalse(_match(predicate, context={**_CONTEXT, "score": 10, "city": "Pune"}))

	# --- loudness: the reason this evaluator exists ------------------------------------------------------

	def test_an_unknown_field_raises_instead_of_returning_false(self):
		"""The headline rule. A typo must not read as 'the condition did not hold'."""
		with self.assertRaises(rules.PredicateError) as caught:
			_match(_rule("staus", "is", "New"))
		self.assertIn("staus", str(caught.exception))

	def test_an_unknown_field_raises_even_nested_under_any(self):
		"""`any` short-circuits on the first true child, so a broken rule could hide behind a true one.
		Evaluation order must not decide whether an authoring fault is reported."""
		predicate = {"type": rules.ANY, "children": [
			_rule("status", "is", "New"),
			_rule("nonexistent", "is", "x"),
		]}
		with self.assertRaises(rules.PredicateError):
			_match(predicate)

	def test_a_field_that_is_declared_but_empty_is_not_an_error(self):
		"""Absent from the SCHEMA is an authoring fault; empty at runtime is an ordinary non-match."""
		self.assertFalse(_match(_rule("city", "is", "Pune"), context={**_CONTEXT, "city": None}))
		self.assertTrue(_match(_rule("city", "is not set"), context={**_CONTEXT, "city": None}))

	def test_an_unknown_operator_raises(self):
		with self.assertRaises(rules.PredicateError):
			_match(_rule("status", "sounds a bit like", "New"))

	def test_a_rule_without_a_field_raises(self):
		with self.assertRaises(rules.PredicateError):
			_match(_rule("", "is", "New"))

	def test_an_unknown_node_type_raises(self):
		with self.assertRaises(rules.PredicateError):
			_match({"type": "maybe", "children": []})

	def test_a_not_with_the_wrong_number_of_children_raises(self):
		for children in ([], [_rule("status", "is", "New"), _rule("city", "is", "Pune")]):
			with self.subTest(count=len(children)):
				with self.assertRaises(rules.PredicateError):
					_match({"type": rules.NOT, "children": children})

	def test_a_non_object_node_raises(self):
		with self.assertRaises(rules.PredicateError):
			_match({"type": rules.ALL, "children": ["status is New"]})

	# --- without a schema, the context is the declaration ------------------------------------------------

	def test_with_no_field_types_the_context_keys_are_the_allowlist(self):
		"""A Branch tests run state, which has no schema. The same loudness rule applies to its keys —
		a Branch reading a key no earlier node ever set is a broken graph, not a false."""
		self.assertTrue(rules.predicate_match(_rule("taken", "is", 0), {"taken": 0}, None))
		with self.assertRaises(rules.PredicateError):
			rules.predicate_match(_rule("never_set", "is", 0), {"taken": 0}, None)
