# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""An Automation Manager lists and opens only the workflows for the business lines they hold.
Real users, Assignment Rules and workflows, answered by Frappe's own get_list and has_permission; nothing patched."""

import ast
import pathlib

import frappe
from frappe.tests import IntegrationTestCase

from tatva_connect.tests.authz.grains import GRAINS, assert_masters_exist
from tatva_connect.workflow_engine.tests import fixtures

WORKFLOW_DT = "CRM Workflow"
SWITCH = "Workflow::CRM Workflow::visibility"

_TP_FIELD = GRAINS[2]  # Tatvapractice · India · Field-Sales
_TP_INSIDE = GRAINS[3]  # Tatvapractice · India · Inside-Sales
_GF_INSIDE = GRAINS[4]  # Goodflip · India · Inside-Sales: same group and program names as _TP_INSIDE

FIELD_MANAGER = "wfgrain.field@example.test"
INSIDE_MANAGER = "wfgrain.inside@example.test"
NO_LINE_MANAGER = "wfgrain.noline@example.test"

# The line each probe workflow declares on its Trigger, as (vertical, group, program); blank means any.
_PROBES = {
	"tp-field-sales": (_TP_FIELD["vertical"], _TP_FIELD["group"], _TP_FIELD["program"]),
	"goodflip-inside-sales": (_GF_INSIDE["vertical"], _GF_INSIDE["group"], _GF_INSIDE["program"]),
	"tatvapractice-any": (_TP_FIELD["vertical"], "", ""),
	"any-vertical-india-inside-sales": ("", _TP_INSIDE["group"], _TP_INSIDE["program"]),
	"site-wide": ("", "", ""),
}

# Exactly what each manager may see; every other probe must be hidden from them.
_EXPECTED = {
	FIELD_MANAGER: {"tp-field-sales", "tatvapractice-any", "site-wide"},
	INSIDE_MANAGER: {"tatvapractice-any", "any-vertical-india-inside-sales", "site-wide"},
	NO_LINE_MANAGER: {"site-wide"},
}


def _automation_manager(email, grain=None):
	"""An Automation Manager whose line comes from an Assignment Rule, the way production grants it."""
	frappe.get_doc({
		"doctype": "User", "email": email, "first_name": email.split("@")[0], "send_welcome_email": 0,
		"roles": [{"role": "Automation Manager"}],
	}).insert(ignore_permissions=True)
	if grain:
		frappe.get_doc({
			"doctype": "Assignment Rule", "name": f"wfgrain-{grain['key']}", "document_type": "CRM Lead",
			"assign_condition": "1", "rule": "Round Robin", "priority": 0, "disabled": 0,
			"grain_vertical": grain["vertical"], "grain_group": grain["group"], "grain_program": grain["program"],
			"users": [{"user": email}],
			"assignment_days": [{"day": day} for day in ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday")],
		}).insert(ignore_permissions=True)


def _workflow(key, vertical, group, program):
	"""A real Draft workflow whose Trigger declares this line; the header copies are derived on save."""
	config = {"subject_doctype": "CRM Lead", "event": "Created"}
	config.update({axis: value for axis, value in (("vertical", vertical), ("group", group), ("program", program)) if value})
	nodes = [fixtures.node("start", "Trigger", config=config, edges={"next": "end"}), fixtures.node("end", "Terminal")]
	return fixtures.make_workflow(f"wfgrain-{key}", nodes, lifecycle_state="Draft").name


def _set_switch(enabled):
	"""Arm or disarm the visibility switch through its own row; a save clears the cached value too."""
	row = frappe.get_doc("CRM Tatva Automation", SWITCH)
	row.enabled = enabled
	row.save(ignore_permissions=True)


class _World(IntegrationTestCase):
	"""Three Automation Managers and five workflows, built once per class and rolled back after it."""

	switch_on = True

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		assert_masters_exist()
		cls._switch_was = frappe.db.get_value("CRM Tatva Automation", SWITCH, "enabled")
		_set_switch(1 if cls.switch_on else 0)
		_automation_manager(FIELD_MANAGER, _TP_FIELD)
		_automation_manager(INSIDE_MANAGER, _TP_INSIDE)
		_automation_manager(NO_LINE_MANAGER)
		cls.names = {key: _workflow(key, *line) for key, line in _PROBES.items()}

	@classmethod
	def tearDownClass(cls):
		_set_switch(cls._switch_was)
		super().tearDownClass()

	def listed(self, user):
		"""The probe keys `user` gets from the Workflows list, through Frappe's own `get_list`."""
		key_of = {name: key for key, name in self.names.items()}
		with self.set_user(user):
			rows = frappe.get_list(WORKFLOW_DT, filters={"name": ["in", list(key_of)]}, pluck="name")
		return {key_of[name] for name in rows}


class TestAnAutomationManagerSeesOnlyTheLinesTheyHold(_World):
	def test_each_manager_lists_exactly_their_lines(self):
		for user, expected in _EXPECTED.items():
			with self.subTest(user=user):
				self.assertEqual(self.listed(user), expected)

	def test_opening_a_workflow_gives_the_same_answer_as_the_list(self):
		for user, expected in _EXPECTED.items():
			for key, name in self.names.items():
				with self.subTest(user=user, workflow=key):
					doc = frappe.get_doc(WORKFLOW_DT, name)
					self.assertEqual(frappe.has_permission(WORKFLOW_DT, "read", doc, user=user), key in expected)

	def test_a_system_manager_lists_every_line(self):
		self.assertEqual(self.listed("Administrator"), set(_PROBES))


class TestSwitchOffIsStockBehaviour(_World):
	"""The switch ships off: every Automation Manager then reads every workflow, as the DocPerm alone allows."""

	switch_on = False

	def test_every_manager_lists_every_workflow(self):
		for user in _EXPECTED:
			with self.subTest(user=user):
				self.assertEqual(self.listed(user), set(_PROBES))


class TestTheEngineIgnoresTheReadersScope(IntegrationTestCase):
	"""Scoping the list must not change which leads a workflow acts on: a background job has no user."""

	def test_no_engine_path_reads_workflows_through_get_list(self):
		root = pathlib.Path(frappe.get_app_path("tatva_connect")) / "workflow_engine"
		offenders = []
		for path in sorted(root.rglob("*.py")):
			if "/tests/" in str(path):
				continue
			for node in ast.walk(ast.parse(path.read_text())):
				if not isinstance(node, ast.Call):
					continue
				name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
				first = node.args[0] if node.args else None
				if name == "get_list" and isinstance(first, ast.Constant) and first.value == WORKFLOW_DT:
					offenders.append(f"{path.name}:{node.lineno}")
		self.assertEqual(offenders, [], f"the engine would obey a user's scope in a job that has no user: {offenders}")
