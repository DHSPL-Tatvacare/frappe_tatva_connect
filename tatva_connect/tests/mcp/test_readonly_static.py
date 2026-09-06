# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Read-only, structurally — the invariant no reviewer should have to re-check by eye.

The MCP package must contain no write call of any kind. `frappe.log_error` is on the banned list for
the same reason as `save`: it writes an Error Log row on the master, from inside a request that has
been switched to the read replica. Diagnostics in this package go to `frappe.logger`, which is a file.

The scan walks the ABSTRACT SYNTAX TREE, not the text, so the package can go on explaining in its own
docstrings exactly which calls it refuses to make without tripping its own guard.
"""
import ast
import io
import os
import unittest

import tatva_connect.mcp as package

# Every one of these either writes a row or opens a write path. None belongs in a read-only server.
BANNED_CALLS = {
	"log_error", "save", "insert", "delete_doc", "set_value", "set_single_value",
	"commit", "rename_doc", "submit", "cancel", "db_insert", "db_update", "bulk_update",
}
# A permission bypass or a guest door would each undo the whole design in one word.
BANNED_NAMES = {"ignore_permissions", "allow_guest"}


def _modules():
	folder = os.path.dirname(package.__file__)
	for name in sorted(os.listdir(folder)):
		if name.endswith(".py"):
			yield name, ast.parse(io.open(os.path.join(folder, name), encoding="utf-8").read())


def _called_name(node):
	"""The bare name a Call resolves to — `frappe.db.set_value(...)` reads as `set_value`."""
	func = node.func
	if isinstance(func, ast.Attribute):
		return func.attr
	if isinstance(func, ast.Name):
		return func.id
	return ""


class TestMCPIsReadOnly(unittest.TestCase):
	def test_no_module_calls_anything_that_writes(self):
		for filename, tree in _modules():
			for node in ast.walk(tree):
				if isinstance(node, ast.Call):
					name = _called_name(node)
					self.assertNotIn(name, BANNED_CALLS,
					                 f"{filename} calls {name}() — the MCP package must never write.")

	def test_no_module_bypasses_permissions_or_opens_a_guest_door(self):
		for filename, tree in _modules():
			for node in ast.walk(tree):
				if isinstance(node, ast.keyword) and node.arg in BANNED_NAMES:
					self.fail(f"{filename} passes {node.arg} — permissions and the login gate are the design.")

	def test_only_the_one_entry_point_is_reachable_over_http(self):
		"""The replica switch lives on `endpoint`. A second whitelisted function in this package would
		be a door around it — reads on the master, no flag, no throttle, no challenge."""
		doors = []
		for filename, tree in _modules():
			for node in ast.walk(tree):
				if not isinstance(node, ast.FunctionDef):
					continue
				for decorator in node.decorator_list:
					name = _called_name(decorator) if isinstance(decorator, ast.Call) else getattr(decorator, "attr", "")
					if name == "whitelist":
						doors.append(f"{filename}:{node.name}")
		self.assertEqual(doors, ["server.py:endpoint"], f"whitelisted functions in the package: {doors}")

	def test_the_endpoint_is_switched_to_the_replica(self):
		from tatva_connect.mcp import server

		source = io.open(server.__file__.replace(".pyc", ".py"), encoding="utf-8").read()
		tree = ast.parse(source)
		endpoint = next(n for n in ast.walk(tree)
		                if isinstance(n, ast.FunctionDef) and n.name == "endpoint")
		decorators = {_called_name(d) if isinstance(d, ast.Call) else getattr(d, "attr", "")
		              for d in endpoint.decorator_list}
		self.assertIn("read_only", decorators, "the one entry point must carry @frappe.read_only()")
		self.assertIn("whitelist", decorators)
