# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""THE BULK VOCABULARY LOCK — the key a bulk lane reads is declared once and published.

A bulk body's array is named per lane: ids arrive as `names`, new records as the entity's own plural,
edits as `updates`. That was already true of all five resources, but the strings were hand-typed at
eighteen call sites and published nowhere, so the only way a caller could learn the key was to send the
wrong one and read the refusal. This test holds the two halves that fix it:

  * no module hand-types a bulk key — every lane reads through `read_bulk_list`
  * every entity that emits a schema has a declared vocabulary, so `bulk.payload_key` cannot be absent

`bulk_max` already carries the same guarantee for the per-call ceiling: enforcement and discovery read
one source, so the number advertised is the number enforced. This is that rule for the names.
"""
import ast
import unittest
from pathlib import Path

import frappe

from tatva_connect.api._base import bulk_keys

MODULES = ("partner", "partner_activity", "partner_note", "partner_call", "partner_file")
LANES = ("get", "create", "update", "delete")


def _tree(module):
	path = Path(frappe.get_app_path("tatva_connect")) / "api" / f"{module}.py"
	return ast.parse(path.read_text()), path


class TestBulkVocabularyIsDeclaredOnce(unittest.TestCase):
	def test_no_module_hand_types_a_bulk_key(self):
		"""A literal key passed to `_read_required_list` is a second declaration of the vocabulary."""
		offenders = []
		for module in MODULES:
			tree, path = _tree(module)
			for node in ast.walk(tree):
				if not isinstance(node, ast.Call):
					continue
				fn = node.func
				if isinstance(fn, ast.Name) and fn.id == "_read_required_list":
					literal = [a for a in node.args if isinstance(a, ast.Constant)]
					if literal:
						offenders.append(f"{path.name}:{node.lineno} -> {literal[0].value!r}")
		self.assertEqual(
			offenders, [],
			"A bulk key is hand-typed instead of read from bulk_keys(). Call read_bulk_list(entity, lane) "
			f"so the key read is the key published: {offenders}",
		)

	def test_every_lane_read_names_a_declared_entity_and_lane(self):
		"""`read_bulk_list` is only as good as its arguments — a typo must fail here, not on the wire."""
		bad = []
		for module in MODULES:
			tree, path = _tree(module)
			for node in ast.walk(tree):
				if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
					continue
				if node.func.id != "read_bulk_list":
					continue
				args = [a.value for a in node.args if isinstance(a, ast.Constant)]
				if len(args) != 2:
					bad.append(f"{path.name}:{node.lineno} -> non-literal arguments")
					continue
				entity, lane = args
				try:
					keys = bulk_keys(entity)
				except KeyError:
					bad.append(f"{path.name}:{node.lineno} -> unknown entity {entity!r}")
					continue
				if lane not in keys:
					bad.append(f"{path.name}:{node.lineno} -> unknown lane {lane!r}")
		self.assertEqual(bad, [], f"read_bulk_list called with something the vocabulary does not declare: {bad}")

	def test_the_published_key_is_the_key_read(self):
		"""Discovery and ingestion resolve through the SAME map, for every entity and every lane."""
		for entity in ("lead", "activity", "note", "call", "file"):
			keys = bulk_keys(entity)
			self.assertEqual(sorted(keys), sorted(LANES), f"{entity} does not declare every lane")
			self.assertEqual(keys["get"], "names", f"{entity}: an id lane is always `names`")
			self.assertEqual(keys["delete"], "names", f"{entity}: an id lane is always `names`")
			self.assertEqual(keys["update"], "updates", f"{entity}: an edit lane is always `updates`")
			self.assertTrue(keys["create"], f"{entity}: a create lane needs the entity's own plural")

	def test_the_live_contract_is_not_renamed(self):
		"""These names are what integrations already send. Renaming one breaks every caller of that lane,
		so the lock states them literally rather than deriving them."""
		self.assertEqual(bulk_keys("lead")["create"], "leads")
		self.assertEqual(bulk_keys("activity")["create"], "activities")
		self.assertEqual(bulk_keys("note")["create"], "notes")
		self.assertEqual(bulk_keys("call")["create"], "calls")
		self.assertEqual(bulk_keys("file")["create"], "files")
