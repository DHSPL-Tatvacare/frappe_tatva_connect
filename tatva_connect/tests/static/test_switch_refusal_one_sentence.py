# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""An action refused because its engine switch is off says so in ONE sentence, from ONE function.

`automation.require(key, feature)` owns the title and the sentence. Four surfaces once wrote their own,
and one named the wrong place to fix it. This lock fails the build if any code checks a switch with
`is_enabled(key)` and then throws in that branch itself, instead of asking `require`.

Pure stdlib AST — runs standalone (tcsec, no frappe) and inside `bench run-tests`.
"""
import ast
import os
import unittest

from tatva_connect.tests.static._lock_helpers import app_root

_ACCESSOR_OWNERS = {"automation", "settings"}  # how `automation.settings.is_enabled` is reached in this app
_HOME = (os.path.join("automation", "settings.py"), "require")  # the one function allowed to write the refusal


def _is_switch_check(node, wrappers=frozenset()):
	"""`is_enabled(key)` on the switch accessor, bare or through `automation` / `settings`, or a call to a module
	function that only returns one (WhatsApp's and telephony's own `is_enabled()`)."""
	if not isinstance(node, ast.Call):
		return False
	func = node.func
	if isinstance(func, ast.Name) and func.id in wrappers:
		return True
	if not node.args:
		return False
	if isinstance(func, ast.Name):
		return func.id == "is_enabled"
	return (isinstance(func, ast.Attribute) and func.attr == "is_enabled"
	        and isinstance(func.value, ast.Name) and func.value.id in _ACCESSOR_OWNERS)


def _wrappers(tree):
	"""Module functions whose last statement returns a switch check: calling one IS reading the switch."""
	return frozenset(f.name for f in tree.body if isinstance(f, ast.FunctionDef) and f.body
	                 and isinstance(f.body[-1], ast.Return) and _is_switch_check(f.body[-1].value))


def _throws(stmts):
	return any(isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "throw"
	           for s in stmts for n in ast.walk(s))


def offenders(source, path="<planted>"):
	"""Every `if not <switch check>: ... throw` in `source`, as path:line."""
	found = []
	tree = ast.parse(source)
	home = {id(n) for f in ast.walk(tree) if isinstance(f, ast.FunctionDef) and (path, f.name) == _HOME
	        for n in ast.walk(f)}
	wrappers = _wrappers(tree)
	for node in ast.walk(tree):
		if id(node) in home:
			continue
		if (isinstance(node, ast.If) and isinstance(node.test, ast.UnaryOp) and isinstance(node.test.op, ast.Not)
				and _is_switch_check(node.test.operand, wrappers) and _throws(node.body)):
			found.append(f"{path}:{node.lineno}")
	return found


class TestSwitchRefusalOneSentence(unittest.TestCase):
	def test_the_scanner_catches_a_hand_written_refusal_and_passes_require(self):
		planted = (
			"def a():\n\tif not automation.is_enabled('X::Y::z'):\n\t\tfrappe.throw('off')\n"
			"def b():\n\tif not is_enabled(KEY):\n\t\tfrappe.throw('off', title='Off')\n"
			"def is_on():\n\treturn automation.is_enabled('X::Y::z')\n"
			"def c():\n\tif not is_on():\n\t\tfrappe.throw('off through a wrapper')\n"
		)
		self.assertEqual(len(offenders(planted)), 3, "the scanner missed a planted hand-written refusal")
		clean = (
			"def d():\n\tautomation.require('X::Y::z', 'Thing')\n"
			"def e():\n\tif not blob_store.is_enabled():\n\t\tfrappe.throw('a settings form, not a switch')\n"
		)
		self.assertEqual(offenders(clean), [])

	def test_no_code_writes_its_own_switched_off_refusal(self):
		root = app_root(__file__)
		found = []
		for dirpath, dirnames, filenames in os.walk(root):
			dirnames[:] = [d for d in dirnames if d not in ("tests", "node_modules", "__pycache__", "public")]
			for name in filenames:
				if name.endswith(".py"):
					path = os.path.join(dirpath, name)
					with open(path, encoding="utf-8") as f:
						found += offenders(f.read(), os.path.relpath(path, root))
		self.assertEqual(found, [], "refuse a switched-off feature with automation.require(key, feature)")
