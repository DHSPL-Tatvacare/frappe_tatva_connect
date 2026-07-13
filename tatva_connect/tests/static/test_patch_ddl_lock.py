# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The DDL lock: a patch may not change a table's schema behind frappe's cache.

Frappe caches a table's column list (`table_columns::tab...`, database.py:1334) and a raw ALTER never
invalidates it — so the next `has_column()` in the same migrate reads a pre-DDL lie. That is exactly how
the UAT deploy died: a patch dropped a column its predecessor had already dropped (1091). Every schema
change goes through `patches/_schema.py`, which busts the key frappe itself busts (model/meta.py:976).

The rule is written at the top of patches.txt, where the next person writing a patch will read it. This
test is what stops them ignoring it. Pure stdlib AST — runs in tcsec and in bench run-tests.
"""
import ast
import os
import unittest

_FORBIDDEN = {"sql_ddl", "rename_column", "rename_field"}
_DOOR = "_schema.py"


def _patches_dir():
	d = os.path.dirname(os.path.abspath(__file__))
	while d != os.path.dirname(d):
		if os.path.exists(os.path.join(d, "hooks.py")):
			return os.path.join(d, "patches")
		d = os.path.dirname(d)
	raise RuntimeError("app root not found")


def _offenders():
	found = []
	root = _patches_dir()
	for name in sorted(os.listdir(root)):
		if not name.endswith(".py") or name == _DOOR:
			continue
		path = os.path.join(root, name)
		tree = ast.parse(open(path).read(), filename=path)
		for node in ast.walk(tree):
			if not isinstance(node, ast.Call):
				continue
			fn = node.func
			if not isinstance(fn, ast.Attribute):
				# a bare rename_field(...) imported straight from frappe still ALTERs the table
				if getattr(fn, "id", None) in _FORBIDDEN:
					found.append(f"{name}:{node.lineno} calls {fn.id}()")
				continue
			through_the_door = isinstance(fn.value, ast.Name) and fn.value.id == "_schema"
			if fn.attr in _FORBIDDEN and not through_the_door:
				found.append(f"{name}:{node.lineno} calls {fn.attr}()")
	return found


class TestPatchDDLLock(unittest.TestCase):
	def test_no_patch_changes_schema_outside_the_one_door(self):
		offenders = _offenders()
		self.assertEqual(
			offenders, [],
			"a patch changes a table's schema directly. Frappe's cached column list will not know, and the "
			"next has_column() will read a lie — this is the (1091) that killed a UAT deploy. Route it "
			"through patches/_schema.py (see the rules at the top of patches.txt):\n  "
			+ "\n  ".join(offenders),
		)
