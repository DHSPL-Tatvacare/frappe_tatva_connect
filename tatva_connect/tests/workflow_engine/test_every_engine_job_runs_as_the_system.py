# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The engine runs as one of two identities: inside a request it is the person, untouched; every job it queues starts as the system."""

import ast
import importlib
import inspect
import pathlib

import frappe
from frappe.tests import IntegrationTestCase

import tatva_connect
from tatva_connect.tests.workflow_engine.fixtures import set_switch
from tatva_connect.workflow_engine import ENGINE_SWITCH, drain

_APP = pathlib.Path(tatva_connect.__file__).parent
# Where the engine queues its jobs: the engine, its verbs, its API, the pool's routing and the workflow's own controller.
_ENGINE = [
	*(_APP / "workflow_engine").rglob("*.py"),
	*(_APP / "automation").rglob("*.py"),
	*(_APP / "workflows").rglob("*.py"),
	_APP / "lead" / "routing.py",
	_APP / "tatva_connect" / "doctype" / "crm_workflow" / "crm_workflow.py",
]
_QUEUERS = {"enqueue", "schedule_on_lane"}
_HELPER = "as_system"


def _sources():
	return [(path, ast.parse(path.read_text())) for path in _ENGINE if "tests" not in path.parts]


def _callee(call):
	return call.func.attr if isinstance(call.func, ast.Attribute) else getattr(call.func, "id", None)


def _queued(tree):
	"""Every dotted method a module queues, with its module-level string constants resolved."""
	constants = {
		target.id: node.value.value
		for node in tree.body if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant)
		for target in node.targets if isinstance(target, ast.Name)
	}
	for call in ast.walk(tree):
		if not (isinstance(call, ast.Call) and _callee(call) in _QUEUERS):
			continue
		args = [kw.value for kw in call.keywords if kw.arg == "method"] or call.args[: 1 if _callee(call) == "enqueue" else 2][-1:]
		for arg in args:
			value = arg.value if isinstance(arg, ast.Constant) else constants.get(getattr(arg, "id", None))
			if isinstance(value, str) and value.startswith("tatva_connect."):
				yield value


def _starts_as_system(dotted):
	module, name = dotted.rsplit(".", 1)
	body = ast.parse(inspect.getsource(getattr(importlib.import_module(module), name))).body[0].body
	first = body[1] if isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) else body[0]
	return isinstance(first, ast.Expr) and isinstance(first.value, ast.Call) and _callee(first.value) == _HELPER


class TestEveryEngineJobRunsAsTheSystem(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		# Registered before super(): it runs after the class rollback, which fires no hook to drop the cached switch row.
		cls.addClassCleanup(frappe.clear_document_cache, "CRM Tatva Automation")
		super().setUpClass()
		# Off, so the pass returns before it books or commits anything.
		set_switch(ENGINE_SWITCH, 0)

	def test_every_job_the_engine_queues_starts_as_the_system(self):
		jobs = {dotted for _, tree in _sources() for dotted in _queued(tree)}
		self.assertGreaterEqual(len(jobs), 8, f"the scan found too few queued jobs to trust: {sorted(jobs)}")
		self.assertEqual(sorted(j for j in jobs if not _starts_as_system(j)), [], "a queued job does not start with as_system()")

	def test_nothing_else_in_the_engine_switches_user(self):
		switches = [
			f"{path.relative_to(_APP)}:{call.lineno}"
			for path, tree in _sources()
			for fn in ast.walk(tree) if isinstance(fn, ast.FunctionDef) and fn.name != _HELPER
			for call in ast.walk(fn) if isinstance(call, ast.Call) and _callee(call) == "set_user"
		]
		self.assertEqual(switches, [], "only as_system() may switch user: a switch inside a request logs the person out")

	def test_a_pass_queued_by_a_user_with_no_roles_runs_as_the_system(self):
		user = frappe.get_doc({"doctype": "User", "email": f"no-roles-{frappe.generate_hash(length=6)}@example.invalid", "first_name": "No roles", "send_welcome_email": 0}).insert(ignore_permissions=True)  # authz-ok: tier-c — test fixture
		self.addCleanup(frappe.set_user, "Administrator")
		frappe.set_user(user.name)
		drain.run()
		self.assertEqual(frappe.session.user, "Administrator")
