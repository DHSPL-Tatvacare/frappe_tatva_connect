# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Locks read from the source (A.20): the MCP package never writes, never bypasses permissions, never locks a row,
and has one door. The syntax tree is walked, not the text, so docstrings may name what the code refuses to call."""
import ast
import os
import unittest

import tatva_connect.mcp as package

# Each writes a row or opens a write path; `log_error` too, as it writes on the master inside a replica-switched request.
BANNED_CALLS = {"log_error", "save", "insert", "delete_doc", "set_value", "set_single_value", "commit", "rename_doc",
                "submit", "cancel", "db_insert", "db_update", "bulk_update", "sql"}
BANNED_KEYWORDS = {"ignore_permissions", "allow_guest", "for_update"}


def _modules():
	folder = os.path.dirname(package.__file__)
	for name in sorted(os.listdir(folder)):
		if name.endswith(".py"):
			yield name, ast.parse(open(os.path.join(folder, name), encoding="utf-8").read())


def _called(node):
	func = node.func
	return func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")


def _decorators(node):
	return {_called(d) if isinstance(d, ast.Call) else getattr(d, "attr", "") for d in node.decorator_list}


class TestMCPLocks(unittest.TestCase):
	def test_nothing_writes_bypasses_permissions_or_locks_a_row(self):
		for filename, tree in _modules():
			for node in ast.walk(tree):
				if isinstance(node, ast.Call):
					self.assertNotIn(_called(node), BANNED_CALLS, f"{filename} calls {_called(node)}()")
				if isinstance(node, ast.keyword):
					self.assertNotIn(node.arg, BANNED_KEYWORDS, f"{filename} passes {node.arg}")

	def test_rows_are_read_only_through_the_permission_checked_calls(self):
		calls = {_called(node) for node in ast.walk(dict(_modules())["records.py"]) if isinstance(node, ast.Call)}
		self.assertNotIn("get_all", calls, "records.py must read rows through get_list / get_doc only")

	def test_the_one_door_is_switched_to_the_replica(self):
		doors = {f"{filename}:{node.name}": _decorators(node) for filename, tree in _modules()
		         for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and "whitelist" in _decorators(node)}
		self.assertEqual(list(doors), ["server.py:endpoint"])
		self.assertIn("read_only", doors["server.py:endpoint"])
